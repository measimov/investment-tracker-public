"""File boundary and source/index consistency regressions; no application imports."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from tools.architecture import server


def test_analyzer_dispatch_preserves_full_frontend_target_allowlist(tmp_path, monkeypatch):
    files = ["backend/main.py", "frontend/App.vue", "frontend/styles.css", "frontend/package.json"]

    def python_analyze(root, paths):
        assert root == tmp_path
        assert paths == ["backend/main.py"]
        return {"symbols": [], "dependencies": [], "errors": [], "tools": {"python": "1"}}

    def node_run(command, **kwargs):
        assert command[0] == "node"
        assert json.loads(kwargs["input"]) == {"root": str(tmp_path), "files": files}
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "symbols": [],
                    "dependencies": [],
                    "errors": [],
                    "tools": {"frontend": "2"},
                }
            )
        )

    monkeypatch.setattr(
        server.importlib, "import_module", lambda name: SimpleNamespace(analyze=python_analyze)
    )
    monkeypatch.setattr(server.subprocess, "run", node_run)
    assert server._analyze_snapshot(tmp_path, files)["tools"] == {"python": "1", "frontend": "2"}


def test_analyzer_failures_remain_visible(tmp_path, monkeypatch):
    def python_fail(*args):
        raise RuntimeError("analyzer failed")

    def node_fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("node", 120)

    monkeypatch.setattr(
        server.importlib, "import_module", lambda name: SimpleNamespace(analyze=python_fail)
    )
    monkeypatch.setattr(server.subprocess, "run", node_fail)
    result = server._analyze_snapshot(tmp_path, ["sample.py", "sample.ts"])
    assert len(result["errors"]) == 2
    assert "Python 分析器未完成" in result["errors"][0]["message"]
    assert "前端分析器未完成" in result["errors"][1]["message"]
    assert result["symbols"] == []


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    def analyze(root, files):
        assert root != tmp_path
        assert all((root / path).is_file() for path in files)
        return {"symbols": [], "dependencies": [], "errors": [], "tools": {"stub": "1"}}

    monkeypatch.setattr(server, "_analyze_snapshot", analyze)
    return tmp_path


def write(root: Path, path: str, content: str = "value = 1\n") -> Path:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)
    return file


def test_index_source_and_reindex(source_tree):
    source = write(source_tree, "backend/sample.py", "name = '中文'\n")
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    index = client.get("/api/index").json()
    assert index["files"] == [
        {
            "path": "backend/sample.py",
            "language": "python",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    assert index["tools"] == {"stub": "1"}
    assert index["scanned_at"].endswith("+00:00")
    params = {"path": "backend/sample.py", "sha256": index["files"][0]["sha256"]}
    response = client.get("/api/source", params=params)
    assert response.json()["content"] == "name = '中文'\n"
    source.write_text("name = 'updated'\n")
    write(source_tree, "frontend/new.ts")
    assert client.get("/api/source", params=params).status_code == 409
    assert (
        client.get(
            "/api/source", params={"path": "frontend/new.ts", "sha256": "0" * 64}
        ).status_code
        == 404
    )
    updated = client.post("/api/reindex").json()
    assert len(updated["files"]) == 2
    assert updated["files"][0]["sha256"] != index["files"][0]["sha256"]
    params["sha256"] = updated["files"][0]["sha256"]
    assert client.get("/api/source", params=params).status_code == 200


def test_old_page_cannot_read_new_source_after_another_page_reindexes(source_tree):
    source = write(source_tree, "sample.py", "def version_a(): pass\n")
    app = server.create_app(source_tree)
    page_a = TestClient(app, base_url="http://localhost")
    page_b = TestClient(app, base_url="http://localhost")
    old_sha = page_a.get("/api/index").json()["files"][0]["sha256"]
    source.write_text("def version_b(): pass\n")
    new_sha = page_b.post("/api/reindex").json()["files"][0]["sha256"]
    stale = page_a.get("/api/source", params={"path": "sample.py", "sha256": old_sha})
    assert stale.status_code == 409
    assert "version_b" not in stale.text
    current = page_b.get("/api/source", params={"path": "sample.py", "sha256": new_sha.upper()})
    assert current.status_code == 200
    assert current.json()["content"] == "def version_b(): pass\n"


@pytest.mark.parametrize("sha256", [None, "", "f" * 63, "f" * 65, "g" * 64])
def test_source_requires_a_valid_page_fingerprint(source_tree, sha256):
    write(source_tree, "sample.py")
    params = {"path": "sample.py"}
    if sha256 is not None:
        params["sha256"] = sha256
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert client.get("/api/source", params=params).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/etc/passwd",
        "backend/../secret.py",
        "backend//sample.py",
        "backend/./sample.py",
        "C:/secret.py",
        "backend\\sample.py",
        "bad\x00.py",
    ],
)
def test_rejects_noncanonical_paths(source_tree, path):
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert client.get("/api/source", params={"path": path, "sha256": "0" * 64}).status_code == 422


def test_excludes_sensitive_generated_binary_and_large_files(source_tree):
    excluded = [
        ".env",
        "backend/.env.local",
        "secrets/config.json",
        "credentials.json",
        "xueqiu-cookies.json",
        "data/ledger.json",
        "backup/account.json",
        "node_modules/package/index.js",
        "frontend/dist/index.html",
        ".venv-architecture/site.py",
        "ops/llm-eval/inputs/input.json",
        "ops/llm-eval/results/result.json",
        "ops/llm-eval/keys/key.txt",
    ]
    for path in excluded:
        write(source_tree, path, "sensitive")
    write(source_tree, "binary.py").write_bytes(b"hello\0world")
    write(source_tree, "non-utf8.txt").write_bytes(b"\xff\xfe")
    write(source_tree, "oversized.py", "x" * (server.MAX_FILE_BYTES + 1))
    write(source_tree, "ops/llm-eval/run.py")
    write(source_tree, ".github/workflows/ci.yml", "name: CI\n")
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    index = client.get("/api/index").json()
    assert {item["path"] for item in index["files"]} == {
        "ops/llm-eval/run.py",
        ".github/workflows/ci.yml",
    }
    assert {item["path"] for item in index["errors"]} == {
        "binary.py",
        "non-utf8.txt",
        "oversized.py",
    }
    for path in excluded:
        assert (
            client.get("/api/source", params={"path": path, "sha256": "0" * 64}).status_code == 404
        )


def test_git_ignored_files_and_report_directory_are_not_indexed(source_tree):
    subprocess.run(["git", "init", "--quiet", str(source_tree)], check=True)
    write(source_tree, ".gitignore", "private-local.json\n")
    write(source_tree, "private-local.json", "sensitive")
    write(source_tree, "custom-output/report.json", "sensitive report")
    write(source_tree, "uncommitted.py")
    client = TestClient(
        server.create_app(source_tree, source_tree / "custom-output"), base_url="http://localhost"
    )
    paths = {item["path"] for item in client.get("/api/index").json()["files"]}
    assert paths == {".gitignore", "uncommitted.py"}


def test_symlink_files_and_directories_cannot_escape(source_tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = write(outside, "private.py", "sensitive = True\n")
    (source_tree / "alias.py").symlink_to(secret)
    (source_tree / "linked").symlink_to(outside, target_is_directory=True)
    local = write(source_tree, "local.py")
    (source_tree / "local-alias.py").symlink_to(local)
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert [item["path"] for item in client.get("/api/index").json()["files"]] == ["local.py"]
    for path in ["alias.py", "linked/private.py", "local-alias.py"]:
        assert (
            client.get("/api/source", params={"path": path, "sha256": "0" * 64}).status_code == 404
        )


@pytest.mark.parametrize("replacement", ["delete", "file_symlink", "directory_symlink"])
def test_indexed_file_cannot_be_replaced_to_bypass_boundary(
    source_tree, tmp_path_factory, replacement
):
    source = write(source_tree, "backend/sample.py")
    outside = tmp_path_factory.mktemp("replacement")
    secret = write(outside, "sample.py", "sensitive = True\n")
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    index = client.get("/api/index").json()
    source.unlink()
    if replacement == "file_symlink":
        source.symlink_to(secret)
    elif replacement == "directory_symlink":
        source.parent.rmdir()
        source.parent.symlink_to(outside, target_is_directory=True)
    response = client.get(
        "/api/source", params={"path": "backend/sample.py", "sha256": index["files"][0]["sha256"]}
    )
    assert response.status_code == 409
    assert "sensitive" not in response.text


def test_analysis_uses_snapshot_even_if_original_changes(source_tree, monkeypatch):
    original = write(source_tree, "sample.py", "value = 1\n")

    def analyze(snapshot, files):
        original.write_text("value = 2\n")
        assert (snapshot / files[0]).read_text() == "value = 1\n"
        return {"symbols": [], "dependencies": [], "errors": [], "tools": {}}

    monkeypatch.setattr(server, "_analyze_snapshot", analyze)
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    index = client.get("/api/index").json()
    assert index["files"][0]["sha256"] == hashlib.sha256(b"value = 1\n").hexdigest()
    assert (
        client.get(
            "/api/source", params={"path": "sample.py", "sha256": index["files"][0]["sha256"]}
        ).status_code
        == 409
    )


def test_failed_rescan_preserves_previous_index(source_tree, monkeypatch):
    write(source_tree, "sample.py")
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    index = client.get("/api/index").json()

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(server, "_build_index", fail)
    assert client.post("/api/reindex").status_code == 503
    assert client.get("/api/index").json() == index


def test_static_mount_exists_only_for_built_ui(source_tree, tmp_path_factory):
    assert (
        TestClient(server.create_app(source_tree), base_url="http://localhost").get("/").status_code
        == 404
    )
    write(source_tree, "frontend/architecture-dist/index.html", "<h1>架构</h1>")
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert client.get("/").text == "<h1>架构</h1>"
    assert client.get("/api/index").status_code == 200
    outside = tmp_path_factory.mktemp("static-secret")
    secret = write(outside, "secret.txt", "sensitive")
    (source_tree / "frontend/architecture-dist/leak.txt").symlink_to(secret)
    assert client.get("/leak.txt").status_code == 404


def test_symlink_static_directory_is_not_mounted(source_tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("static-outside")
    write(outside, "index.html", "sensitive")
    (source_tree / "frontend").mkdir()
    (source_tree / "frontend/architecture-dist").symlink_to(outside, target_is_directory=True)
    assert (
        TestClient(server.create_app(source_tree), base_url="http://localhost").get("/").status_code
        == 404
    )


@pytest.mark.parametrize("replacement", ["build_directory", "frontend_directory", "asset_file"])
def test_static_snapshot_survives_workspace_symlink_replacement(
    source_tree, tmp_path_factory, replacement
):
    write(source_tree, "frontend/architecture-dist/index.html", "<h1>original UI</h1>")
    asset = write(source_tree, "frontend/architecture-dist/assets/app.js", "original script")
    binary = write(source_tree, "frontend/architecture-dist/icon.png")
    binary.write_bytes(b"\x89PNG\x00\xff")
    outside = tmp_path_factory.mktemp("replacement-assets")
    for prefix in ["", "architecture-dist/"]:
        write(outside, prefix + "index.html", "sensitive outside file")
        write(outside, prefix + "secret.txt", "sensitive outside file")
        write(outside, prefix + "assets/app.js", "sensitive outside script")
    app = server.create_app(source_tree)
    snapshot = Path(app.state.static_snapshot.name)
    with TestClient(app, base_url="http://localhost") as client:
        if replacement == "build_directory":
            original = source_tree / "frontend/architecture-dist"
            original.rename(source_tree / "saved-build")
            original.symlink_to(outside, target_is_directory=True)
        elif replacement == "frontend_directory":
            original = source_tree / "frontend"
            original.rename(source_tree / "saved-frontend")
            original.symlink_to(outside, target_is_directory=True)
        else:
            asset.unlink()
            asset.symlink_to(outside / "assets/app.js")
        assert client.get("/").text == "<h1>original UI</h1>"
        assert client.get("/assets/app.js").text == "original script"
        assert client.get("/icon.png").content == b"\x89PNG\x00\xff"
        assert client.get("/secret.txt").status_code == 404
        assert snapshot.is_dir()
    assert not snapshot.exists()


def test_static_snapshot_does_not_copy_existing_symlink_assets(source_tree, tmp_path_factory):
    write(source_tree, "frontend/architecture-dist/index.html", "original UI")
    outside = tmp_path_factory.mktemp("existing-assets")
    secret = write(outside, "secret.js", "sensitive outside file")
    (source_tree / "frontend/architecture-dist/secret.js").symlink_to(secret)
    (source_tree / "frontend/architecture-dist/external").symlink_to(
        outside, target_is_directory=True
    )
    with TestClient(server.create_app(source_tree), base_url="http://localhost") as client:
        assert client.get("/").text == "original UI"
        assert client.get("/secret.js").status_code == 404
        assert client.get("/external/secret.js").status_code == 404


def test_static_snapshot_rejects_excessive_total_size(source_tree, monkeypatch):
    write(source_tree, "frontend/architecture-dist/index.html", "123456")
    write(source_tree, "frontend/architecture-dist/app.js", "123456")
    monkeypatch.setattr(server, "MAX_STATIC_TOTAL_BYTES", 10)
    with pytest.raises(ValueError, match="静态构建产物超过快照容量限制"):
        server.create_app(source_tree)


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "localhost.evil.example",
        "127.0.0.1.evil.example",
        "<app-host>.evil.example",
        "8.8.8.8",
        "203.0.113.1",
        "0.0.0.0",
        "239.0.0.1",
        "[2001:4860:4860::8888]",
    ],
)
def test_nonlocal_host_is_rejected(source_tree, host):
    write(source_tree, "sample.py")
    client = TestClient(
        server.create_app(source_tree), base_url="http://localhost", headers={"Host": host}
    )
    assert client.get("/api/index").status_code == 400
    assert (
        client.get("/api/source", params={"path": "sample.py", "sha256": "0" * 64}).status_code
        == 400
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost.evil.example",
        "null",
        "file://localhost",
        "http://user@localhost",
        "http://[broken",
        "http://localhost?",
        "http://localhost#",
        "http://localhost:99999",
        "http://localhost/path",
    ],
)
def test_nonlocal_origin_cannot_request_rescan(source_tree, origin):
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert client.post("/api/reindex", headers={"Origin": origin}).status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://127.0.1.1:8765",
        "http://<app-host>:8765",
        "http://172.16.1.20:8765",
        "http://192.168.1.20:8765",
        "http://169.254.1.20:8765",
        "http://[::1]:8765",
        "http://[fd12::20]:8765",
        "http://[fe80::20]:8765",
    ],
)
def test_local_and_lan_pages_source_and_same_origin_rescan(source_tree, origin):
    source = write(source_tree, "sample.py")
    write(source_tree, "frontend/architecture-dist/index.html", "<h1>architecture</h1>")
    # This Starlette test transport cannot parse IPv6 netlocs; send the actual
    # authority as Host so the app still validates the browser's IPv6 origin.
    with TestClient(
        server.create_app(source_tree),
        base_url=f"{urlsplit(origin).scheme}://localhost",
        headers={"Host": urlsplit(origin).netloc},
    ) as client:
        assert client.get("/").text == "<h1>architecture</h1>"
        index = client.get("/api/index").json()
        assert (
            client.get(
                "/api/source", params={"path": "sample.py", "sha256": index["files"][0]["sha256"]}
            ).status_code
            == 200
        )
        source.write_text("value = 2\n")
        result = client.post("/api/reindex", headers={"Origin": origin})
        assert result.status_code == 200
        assert result.json()["files"][0]["sha256"] != index["files"][0]["sha256"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://10.0.0.40:8765",
        "http://<app-host>:8766",
        "https://<app-host>:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "https://evil.example",
        "http://<app-host>.evil.example:8765",
    ],
)
def test_rescan_rejects_different_host_port_scheme_even_within_lan(source_tree, origin):
    client = TestClient(server.create_app(source_tree), base_url="http://<app-host>:8765")
    assert client.post("/api/reindex", headers={"Origin": origin}).status_code == 403
    assert client.app.state.index is None


@pytest.mark.parametrize(
    "host", ["user@localhost", "localhost?", "localhost#", "localhost:99999", "[broken"]
)
def test_malformed_host_is_rejected_before_url_processing(source_tree, host):
    client = TestClient(server.create_app(source_tree), base_url="http://localhost")
    assert client.get("/api/index", headers={"Host": host}).status_code == 400
    assert client.app.state.index is None


def test_duplicate_host_and_origin_headers_are_rejected(source_tree):
    client = TestClient(server.create_app(source_tree), base_url="http://<app-host>:8765")
    assert (
        client.get(
            "/api/index", headers=[("Host", "<app-host>:8765"), ("Host", "evil.example")]
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/reindex",
            headers=[("Origin", "http://<app-host>:8765"), ("Origin", "http://evil.example")],
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "arguments,host", [([], "0.0.0.0"), (["--host", "127.0.0.1"], "127.0.0.1")]
)
def test_cli_default_lan_and_explicit_loopback_without_starting_server(
    source_tree, monkeypatch, arguments, host
):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append(options))
    monkeypatch.setattr(sys, "argv", ["architecture", "--root", str(source_tree), *arguments])
    server.main()
    assert calls == [{"host": host, "port": 8765}]
