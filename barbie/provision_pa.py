"""Run on PA after deploying the mirror cache-only fix. Never prints secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mirror-root', type=Path, default=Path('/home/badangel/mw-mirror'))
    p.add_argument('--private-dir', type=Path, default=Path('/home/badangel/.barbie'))
    args = p.parse_args()
    args.private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = args.private_dir / 'pa.private.json'
    if config_path.exists():
        raise SystemExit('Already provisioned. Existing credentials were preserved.')
    sys.path.insert(0, str(args.mirror_root))
    from mw import db
    config = db.load_credentials()
    config.pop('_source', None)
    worker, manager, mirror = (secrets.token_urlsafe(32) for _ in range(3))
    key = 'barbie-' + secrets.token_hex(6)
    # Save recoverable secrets before the insert; no credential is printed.
    value = dict(mysql=config, worker_token=worker, manager_token=manager)
    with config_path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle)
    config_path.chmod(0o600)
    for name, data in [('worker-credentials.json', dict(worker_token=worker, mirror_token=key + '.' + mirror)),
                       ('manager-credentials.json', dict(manager_token=manager))]:
        path = args.private_dir / name
        with path.open('x', encoding='utf-8') as handle:
            json.dump(data, handle)
        path.chmod(0o600)
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute('INSERT INTO client(client_key,secret_sha256,client_name,daily_quota,may_donate) '
                    'VALUES(%s,%s,%s,0,0)',
                    (key, hashlib.sha256(mirror.encode()).digest(), 'Barbie replay worker'))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    from .pa_app import create_app
    create_app(config_path)
    print('Provisioned MySQL mailbox and quota-0, non-donating mirror client.')
    print('Private configuration saved under ' + str(args.private_dir))


if __name__ == '__main__':
    main()
