# Public Release Notes

This repository is published as a clean snapshot without the private repository's
Git history.

Current snapshot source: private `main` at
`bd82291` (2026-09-15), plus the optional-dependency change from private
PR #204 (`xueqiu_source` degrades explicitly when `xueqiu-market` is absent).

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
  `docker-compose.yml`, and deployment docs.
- Test report fixtures contain excerpts and metadata from public regulatory
  filings; they do not contain broker statements or user portfolio data.
- Treat the squashed `20260728_0001_initial_schema.py` as a fresh pre-v1.0
  baseline; it is not an in-place upgrade from the first public snapshot.
- The private repository depends on the private `xueqiu-market` package
  (雪球 quotes / A-share fundamentals / opinion matching). The public snapshot
  drops that requirement and the CI/Docker credential plumbing for it; the
  `xueqiu_source` wrapper degrades explicitly (`XueqiuUnavailable`) when the
  package is not installed, and every dependent feature reports the data
  source as unavailable instead of failing silently.
- HKEX daily quotation and 披露易 annual/interim report fixtures are excerpts
  of public exchange data.
