# Public Release Notes

This repository is published as a clean snapshot without the private repository's
Git history.

Current snapshot source: private `main` at
`b0dca1c` (2026-09-30).

Before syncing a new public release:

1. Export a fresh tracked-file snapshot from the private repository.
2. Remove local deployment values, personal paths, private IPs, and real
   credentials.
3. Keep `.env` files, database files, broker statements, certificates, logs, and
   backups out of the public repository.
4. Run a secret scan over the snapshot before committing.

The public example user is `demo`. Real deployments should set their own
passwords in `.env`.

Public-only adaptations:

- Rename the private initial user and password setting to `demo` /
  `DEMO_INITIAL_PASSWORD`.
- Keep this file in the public repository even though it is not part of the
  private snapshot.
- Exclude private operator guidance (`CLAUDE.md`) and `ops/`.
- Replace real-ledger reconciliation figures in `METRICS_AUDIT.md` with neutral
  isolated-test wording.
- Keep `TUSHARE_TOKEN` optional (empty default) in `.env.example`,
  `docker-compose.yml`, and deployment docs. Compose passes it as a bare key
  (`- TUSHARE_TOKEN`) so the only default lives in `config.py`; the private
  repository requires it with `${TUSHARE_TOKEN:?...}`.
- Test report fixtures contain excerpts and metadata from public regulatory
  filings; they do not contain broker statements or user portfolio data.
- Treat the squashed `20260728_0001_initial_schema.py` as a fresh pre-v1.0
  baseline; it is not an in-place upgrade from the first public snapshot.
- For the current snapshot, the private repository depended on the private
  `xueqiu-market` package (雪球 quotes / A-share fundamentals / opinion matching).
  This public snapshot drops that requirement and the CI/Docker credential
  plumbing for it; the
  `xueqiu_source` wrapper degrades explicitly (`XueqiuUnavailable`) when the
  package is not installed, and every dependent feature reports the data
  source as unavailable instead of failing silently.
- HKEX daily quotation and 披露易 annual/interim report fixtures are excerpts
  of public exchange data.
- The built-in Xueqiu collector migration (`20260927_0024`) ships with an empty
  `SEED_AUTHORS`; the public snapshot carries no personal follow list. Collector
  tests seed their own synthetic authors, and fixtures derived from public
  Xueqiu pages use synthetic IDs/names for followed authors and cubes
  (`1000000001`, `1000000002`, `某作者`, `ZH000001`, cube number `000001`);
  signer goldens were
  regenerated for those URLs with the original signing script.
- The `xueqiu-collector` compose service builds from the same backend image
  without BuildKit secrets.

## Next sync: optional Xueqiu client

When syncing a source revision with `backend/requirements-xueqiu.txt` and
`WITH_XUEQIU`, use its optional-client path instead of replaying the historical
dependency removal above:

- Keep both requirements files and the conditional Docker installation branch.
  Core installation uses `requirements.txt`; only the optional file references
  the private client. Dependency URLs must not contain credentials.
- Set `WITH_XUEQIU: "0"` for both backend and collector builds in the public
  Compose file. Keep build credentials and Compose secret bindings out of the
  public deployment; no private repository access is needed for core builds.
- Keep the upstream CI check that the client is absent and the tests for that
  configuration. Do not add credential setup or manually remove a dependency
  from the core requirements file.
- Preserve explicit unavailability for client-dependent data sources. Existing
  historical opinions remain readable; capability checks and administrator
  setup access must not be replaced with unconditional hiding.

Validate the resulting sanitized snapshot with an actual no-client installation,
core Docker build, and its normal backend/frontend checks before publishing.
This section records the next-sync procedure; it does not change the current
snapshot source above or publish a new application snapshot.
