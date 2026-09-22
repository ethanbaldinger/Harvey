"""Harvey Sync CLI: Pulls completed Harbie lookups from PythonAnywhere into

local all_your_base database.

Usage:
  python -m harbie.sync_from_pa [--limit 5000] [--tunnel-port 13306]
"""
from __future__ import annotations

import argparse
import logging
import sys

import mysql.connector

from .harvey_sync import HarveySync
from .queue_store import QueueStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("harbie.sync_cli")


def main():
    parser = argparse.ArgumentParser(description="Sync completed lookups from PA harbie_queue to local all_your_base")
    parser.add_argument("--limit", type=int, default=10000, help="Max completed rows to sync per pass")
    parser.add_argument("--pa-host", type=str, default="127.0.0.1")
    parser.add_argument("--pa-port", type=int, default=13306)
    parser.add_argument("--pa-user", type=str, default="badangel")
    parser.add_argument("--pa-password", type=str, default="SandyMagdalena")
    parser.add_argument("--local-host", type=str, default="localhost")
    parser.add_argument("--local-user", type=str, default="ethan")
    parser.add_argument("--local-password", type=str, default="ethanpw")
    parser.add_argument("--local-db", type=str, default="all_your_base")
    args = parser.parse_args()

    # 1. Connect to Remote PA MySQL
    LOG.info("Connecting to PythonAnywhere MySQL via %s:%d...", args.pa_host, args.pa_port)
    db_name = "badangel$mw"
    def get_pa_conn():
        return mysql.connector.connect(
            host=args.pa_host,
            port=args.pa_port,
            user=args.pa_user,
            password=args.pa_password,
            database=db_name,
            connection_timeout=10,
        )

    # 2. Connect to Local MySQL
    LOG.info("Connecting to local MySQL database '%s'...", args.local_db)
    def get_local_conn():
        return mysql.connector.connect(
            host=args.local_host,
            user=args.local_user,
            password=args.local_password,
            database=args.local_db,
        )

    try:
        remote_store = QueueStore(get_pa_conn, is_mysql=True)
    except Exception as exc:
        LOG.error("Failed to connect to PA MySQL: %s", exc)
        LOG.error("Ensure SSH tunnel to PA is active on port %d!", args.pa_port)
        sys.exit(1)

    # 3. Execute Sync
    sync = HarveySync(remote_store, local_conn_factory=get_local_conn, is_mysql=True)
    LOG.info("Reconciling completed lookups into %s...", args.local_db)
    result = sync.sync_completed(limit=args.limit)

    # 4. Display Results
    print("\n" + "=" * 60)
    print(" HARBIE -> HARVEY RECONCILIATION COMPLETE")
    print("=" * 60)
    print(f" Completed Remote Rows Checked: {result['completed_remote_rows']:,}")
    print(f" Positive Headword Hits (MW+) : {result['hits']:,}")
    print(f" Dictionary Misses (MW-)      : {result['misses']:,}")
    print(f" Local Word Records Updated   : {result['local_words_updated']:,}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
