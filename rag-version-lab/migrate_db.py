"""Offline schema migration / rollback. Stop API and Worker before running."""
import argparse
import sqlite3
from pathlib import Path
import version_store as store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services-stopped', action='store_true', required=True)
    parser.add_argument('--restore', type=Path, help='Restore a saved SQLite backup; also deploy old code before restarting')
    args = parser.parse_args()
    if args.restore:
        if args.restore.resolve() == store.DB.resolve():
            parser.error('backup and active database must differ')
        with sqlite3.connect(args.restore.resolve().as_uri() + '?mode=ro', uri=True) as source:
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                parser.error('backup integrity check failed')
            with store.connect() as dest:
                # Preserve current state before overwriting, so new data is recoverable.
                before = store.DB.with_name(store.DB.name + '.before-rollback-' + str(__import__('time').time_ns()) + '.bak')
                with sqlite3.connect(before) as saved:
                    dest.backup(saved)
                before.chmod(0o600)
                source.backup(dest)
        print('Restored backup. Deploy matching old code before restarting services.')
    else:
        store.init()
        print('Schema ready. Existing database backup: version.db.pre-restore-*.bak')


if __name__ == '__main__':
    main()
