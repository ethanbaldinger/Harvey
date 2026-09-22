"""Harvey Batch Pusher CLI: Pushes overnight candidate batches from local all_your_base

to PythonAnywhere's harbie_queue.

Usage:
  python -m harbie.push_to_pa [--candidates 20000] [--hoover 5000] [--tunnel-port 13306]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import mysql.connector

from .harvey_pusher import HarveyPusher
from .queue_store import QueueStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("harbie.pusher_cli")


def main():
    parser = argparse.ArgumentParser(description="Push candidate batches from local all_your_base to PA harbie_queue")
    parser.add_argument("--candidates", type=int, default=20000, help="Number of candidate words to queue (default 20,000)")
    parser.add_argument("--hoover", type=int, default=5000, help="Number of hoover buffer words to queue (default 5,000)")
    parser.add_argument("--pa-host", type=str, default="127.0.0.1", help="PA MySQL host or tunnel host (default 127.0.0.1)")
    parser.add_argument("--pa-port", type=int, default=13306, help="PA MySQL port (default 13306 via SSH tunnel)")
    parser.add_argument("--pa-user", type=str, default="badangel")
    parser.add_argument("--pa-password", type=str, default="SandyMagdalena")
    parser.add_argument("--local-host", type=str, default="localhost")
    parser.add_argument("--local-user", type=str, default="ethan")
    parser.add_argument("--local-password", type=str, default="ethanpw")
    parser.add_argument("--local-db", type=str, default="all_your_base")
    args = parser.parse_args()

    # 1. Connect to Local all_your_base
    LOG.info("Connecting to local MySQL database '%s'...", args.local_db)
    def get_local_conn():
        return mysql.connector.connect(
            host=args.local_host,
            user=args.local_user,
            password=args.local_password,
            database=args.local_db,
        )

    # 2. Connect to Remote PA MySQL
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

    try:
        remote_store = QueueStore(get_pa_conn, is_mysql=True)
    except Exception as exc:
        LOG.error("Failed to connect to PA MySQL: %s", exc)
        LOG.error("Make sure your SSH tunnel to PA is running on port %d!", args.pa_port)
        sys.exit(1)

    # 3. Extract Candidates and Hoover Words
    pusher = HarveyPusher(remote_store, local_conn_factory=get_local_conn)
    LOG.info("Extracting top %d candidates and %d hoover words from %s...", args.candidates, args.hoover, args.local_db)
    candidates, hoover = pusher.extract_local_candidates(
        candidate_limit=args.candidates,
        hoover_limit=args.hoover,
    )

    # 4. Push to PA Queue
    LOG.info("Deploying batch to PythonAnywhere harbie_queue...")
    result = pusher.push_work_package(candidates, hoover)

    # 5. Display Summary
    stats = remote_store.get_stats()
    print("\n" + "=" * 60)
    print(" HARVEY -> HARBIE OVERNIGHT BATCH DEPLOYED SUCCESSFULLY")
    print("=" * 60)
    print(f" Candidates Prepared : {result['candidates_prepared']:,}")
    print(f" Hoover Pool Prepared: {result['hoover_prepared']:,}")
    print(f" Newly Inserted Rows : {result['newly_inserted']:,}")
    print(f" Total Queue State   : {stats}")
    print(f" Harvey Heartbeat    : SIGNALS 'CONNECTED' (valid for 5m)")
    print("=" * 60)
    print(" You can safely shut your laptop. Harbie will roll 24/7 on PA.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
