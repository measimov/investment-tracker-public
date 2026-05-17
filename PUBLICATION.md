# Public Release Notes

This repository is published as a clean snapshot without the private repository's
Git history.

Before syncing a new public release:

1. Export a fresh tracked-file snapshot from the private repository.
2. Remove local deployment values, personal paths, private IPs, and real
   credentials.
3. Keep `.env` files, database files, broker statements, certificates, logs, and
   backups out of the public repository.
4. Run a secret scan over the snapshot before committing.

The public example user is `demo`. Real deployments should set their own
passwords in `.env`.
