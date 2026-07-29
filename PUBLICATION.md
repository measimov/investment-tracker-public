# Public Release Notes

This repository is published as a clean snapshot without the private repository's
Git history.

Current snapshot source: private `main` at
`53dcdb71b02c8e82c123f2df62b47f0259856751` (2026-07-29).

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
- Exclude private operator guidance (`CLAUDE.md`), production rebuild plans and
  verification notes, and `ops/rebuild/`.
- Treat the squashed `20260728_0001_initial_schema.py` as a fresh pre-v1.0
  baseline; it is not an in-place upgrade from the first public snapshot.
