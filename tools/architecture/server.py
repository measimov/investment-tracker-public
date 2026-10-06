"""Read-only architecture queries over an explicitly filtered source snapshot."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

MAX_FILE_BYTES = 1024 * 1024
MAX_STATIC_FILE_BYTES = 4 * 1024 * 1024
MAX_STATIC_TOTAL_BYTES = 16 * 1024 * 1024
MAX_STATIC_FILES = 256
LAN_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)
EXCLUDED_DIRS = {
    "node_modules",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "architecture-dist",
    "data",
    "datasets",
    "backups",
    "backup",
    "secrets",
    "credentials",
    "keys",
    "inputs",
    "results",
    "reports",
    "artifacts",
    "raw",
    "coverage",
    "htmlcov",
    "test-results",
    "playwright-report",
    "blob-report",
    "demo-output",
}
LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".vue": "vue",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".html": "html",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "text",
    ".txt": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
}
SPECIAL_FILES = {
    "Makefile": "makefile",
    "Dockerfile": "dockerfile",
    "LICENSE": "text",
    ".gitignore": "text",
    ".dockerignore": "text",
    ".editorconfig": "ini",
    ".prettierignore": "text",
    ".prettierrc": "json",
}
SENSITIVE_NAME = re.compile(
    r"(^|[-_.])(secrets?|credentials?|cookies?|tokens?|passwords?|private[-_]?key)([-_.]|$)",
    re.IGNORECASE,
)
CODE_LANGUAGES = {"python", "typescript", "javascript", "vue", "shell"}


def _valid_path(path: str) -> bool:
    return (
        bool(path)
        and not any(c in path for c in "\\:\x00")
        and not any(
            part in {"", ".", ".."} or any(ord(c) < 32 for c in part) for part in path.split("/")
        )
    )


def _allowed_directory(name: str) -> bool:
    return name.lower() not in EXCLUDED_DIRS and (not name.startswith(".") or name == ".github")


def _language(path: str) -> str | None:
    if not _valid_path(path):
        return None
    parts = path.split("/")
    if any(not _allowed_directory(part) for part in parts[:-1]):
        return None
    name = parts[-1]
    if name.lower().startswith(".env"):
        return None
    if name.startswith(".") and name not in SPECIAL_FILES and not name.startswith(".prettierrc."):
        return None
    language = SPECIAL_FILES.get(name) or LANGUAGES.get(Path(name).suffix.lower())
    if name.startswith("Dockerfile."):
        language = "dockerfile"
    if language not in CODE_LANGUAGES and SENSITIVE_NAME.search(name):
        return None
    return language


def _read_file(root: Path, path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Open every component without following symlinks, including during replacement races."""
    if not _valid_path(path):
        raise ValueError("文件路径无效")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("不是普通文件")
            if before.st_size > max_bytes:
                raise ValueError(f"文件超过允许大小（{max_bytes} 字节）")
            content = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
            if len(content) > max_bytes:
                raise ValueError(f"文件超过允许大小（{max_bytes} 字节）")
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("读取期间文件发生变化，请重新扫描")
            return content
    finally:
        os.close(directory_fd)


def _read_source(root: Path, path: str) -> bytes:
    content = _read_file(root, path)
    if any(byte < 32 and byte not in (9, 10, 13) for byte in content):
        raise ValueError("文件不是可安全展示的文本，未索引")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("文件不是 UTF-8 文本，未索引") from exc
    return content


def _snapshot_static(root: Path) -> tempfile.TemporaryDirectory | None:
    static_dir = root / "frontend" / "architecture-dist"
    if not static_dir.is_dir() or (root / "frontend").is_symlink() or static_dir.is_symlink():
        return None
    snapshot = tempfile.TemporaryDirectory(prefix="architecture-ui-")
    total_bytes, count = 0, 0
    try:
        for directory, directories, files in os.walk(static_dir, followlinks=False):
            base = Path(directory)
            directories[:] = [
                name
                for name in directories
                if not name.startswith(".") and not (base / name).is_symlink()
            ]
            for name in files:
                original = base / name
                if name.startswith(".") or original.is_symlink():
                    continue
                content = _read_file(
                    root,
                    original.relative_to(root).as_posix(),
                    MAX_STATIC_FILE_BYTES,
                )
                total_bytes += len(content)
                count += 1
                if total_bytes > MAX_STATIC_TOTAL_BYTES or count > MAX_STATIC_FILES:
                    raise ValueError("静态构建产物超过快照容量限制，请检查构建目录")
                destination = Path(snapshot.name) / original.relative_to(static_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        return snapshot
    except BaseException:
        snapshot.cleanup()
        raise


def _candidate_paths(root: Path, report_dir: Path | None) -> list[str]:
    if (root / ".git").exists():
        # Respect project ignore rules while including uncommitted source files.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        candidates = result.stdout.decode("utf-8").split("\0")
    else:
        candidates = []
        for directory, directories, files in os.walk(root, followlinks=False):
            base = Path(directory)
            directories[:] = [
                name
                for name in directories
                if _allowed_directory(name)
                and not (base / name).is_symlink()
                and (report_dir is None or not (base / name).is_relative_to(report_dir))
            ]
            candidates.extend((base / name).relative_to(root).as_posix() for name in files)
    return sorted(
        {
            path
            for path in candidates
            if _language(path) is not None
            and (report_dir is None or not (root / path).is_relative_to(report_dir))
        }
    )


def _analyze_snapshot(root: Path, files: list[str]) -> dict:
    result = {
        "symbols": [],
        "dependencies": [],
        "errors": [],
        "tools": {},
        "cycles": [],
        "cycles_truncated": False,
    }

    def merge(analysis: dict) -> None:
        for field in ("symbols", "dependencies", "errors", "cycles"):
            result[field].extend(analysis.get(field, []))
        result["tools"].update(analysis.get("tools", {}))
        result["cycles_truncated"] |= bool(analysis.get("cycles_truncated"))

    python_files = [path for path in files if _language(path) == "python"]
    if python_files:
        try:
            analyzer = importlib.import_module("tools.architecture.python_index")
            merge(analyzer.analyze(root, python_files))
        except Exception as exc:
            result["errors"].append(
                {
                    "path": None,
                    "message": f"Python 分析器未完成（{type(exc).__name__}）",
                }
            )
    frontend_files = [
        path for path in files if _language(path) in {"typescript", "javascript", "vue"}
    ]
    if frontend_files:
        try:
            completed = subprocess.run(
                ["node", str(Path(__file__).with_name("frontend_index.mjs"))],
                input=json.dumps({"root": str(root), "files": files}),
                text=True,
                capture_output=True,
                check=True,
                timeout=120,
            )
            merge(json.loads(completed.stdout))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
            result["errors"].append(
                {
                    "path": None,
                    "message": f"前端分析器未完成（{type(exc).__name__}）",
                }
            )
    return result


def _build_index(root: Path, report_dir: Path | None) -> dict:
    files, errors = [], []
    with tempfile.TemporaryDirectory(prefix="architecture-source-") as temporary:
        snapshot = Path(temporary)
        for path in _candidate_paths(root, report_dir):
            try:
                content = _read_source(root, path)
            except (OSError, ValueError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "文件已不可安全读取，未索引"
                errors.append({"path": path, "message": message})
                continue
            destination = snapshot / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            files.append(
                {
                    "path": path,
                    "language": _language(path),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        result = _analyze_snapshot(snapshot, [item["path"] for item in files])
    result["errors"].extend(errors)
    return {"files": files, **result, "scanned_at": datetime.now(timezone.utc).isoformat()}


def _local_origin(value: str) -> tuple[str, str, int] | None:
    """Accept explicit loopback/LAN addresses, without DNS lookups or domain wildcards."""
    if any(character.isspace() or ord(character) < 32 for character in value) or any(
        character in value for character in "\\?#"
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            return None
        host = parsed.hostname
        if host != "localhost":
            address = ipaddress.ip_address(host)
            if not address.is_loopback and not any(address in network for network in LAN_NETWORKS):
                return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme, host, port
    except ValueError:
        return None


def create_app(root: Path | str, report_dir: Path | str | None = None) -> FastAPI:
    from tools.architecture.reports import ReportStore

    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("项目根路径必须是目录")
    reports = Path(report_dir).absolute() if report_dir is not None else None
    static_snapshot = _snapshot_static(root)
    report_store = ReportStore(root, reports)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if static_snapshot is not None:
                static_snapshot.cleanup()

    app = FastAPI(title="项目架构浏览", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.static_snapshot = static_snapshot

    @app.middleware("http")
    async def local_host(request: Request, call_next):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or _local_origin(f"{request.scope['scheme']}://{hosts[0]}") is None:
            return JSONResponse(
                {"detail": "只允许 localhost、回环或局域网 IP 地址访问"}, status_code=400
            )
        return await call_next(request)

    app.state.index = None
    lock = threading.RLock()

    def get_index(rebuild: bool = False) -> dict:
        with lock:
            if rebuild or app.state.index is None:
                try:
                    index = _build_index(root, reports)
                except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
                    raise HTTPException(503, "源码扫描未完成，请检查项目目录后重试") from exc
                app.state.index = index
            return app.state.index

    @app.get("/api/index")
    def index() -> dict:
        return get_index()

    @app.get("/api/reports")
    def report_list() -> dict:
        return report_store.list()

    @app.get("/api/report")
    def report(id: str = Query(min_length=1, max_length=257)) -> dict:
        return report_store.report(id)

    @app.get("/api/compare")
    def comparison(
        before: str = Query(min_length=1, max_length=257),
        after: str = Query(min_length=1, max_length=257),
    ) -> dict:
        return report_store.compare(before, after)

    @app.post("/api/reindex")
    def reindex(request: Request) -> dict:
        origin = request.headers.get("origin")
        if origin is not None:
            requested = _local_origin(origin)
            current = _local_origin(f"{request.scope['scheme']}://{request.headers['host']}")
            if (
                requested is None
                or requested != current
                or len(request.headers.getlist("origin")) != 1
            ):
                raise HTTPException(403, "只允许同一来源的本机或局域网页面发起重新扫描")
        return get_index(rebuild=True)

    @app.get("/api/source")
    def source(
        path: str = Query(min_length=1, max_length=4096),
        sha256: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"),
    ) -> dict:
        if not _valid_path(path):
            raise HTTPException(422, "文件路径必须是项目内的相对路径")
        with lock:
            item = next((item for item in get_index()["files"] if item["path"] == path), None)
            if item is None:
                raise HTTPException(404, "文件不在允许读取的索引中")
            if sha256.lower() != item["sha256"]:
                raise HTTPException(409, "页面索引已过期，请刷新索引后重试")
            try:
                content = _read_source(root, path)
            except (OSError, ValueError) as exc:
                raise HTTPException(409, "源码已变化或无法安全读取，请重新扫描") from exc
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise HTTPException(409, "源码已变化，请重新扫描")
            return {**item, "content": content.decode("utf-8")}

    if static_snapshot is not None:
        app.mount(
            "/",
            StaticFiles(directory=static_snapshot.name, html=True, follow_symlink=False),
            name="ui",
        )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动独立项目架构浏览服务")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；仅本机访问可设为 127.0.0.1")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(args.root, args.report_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
