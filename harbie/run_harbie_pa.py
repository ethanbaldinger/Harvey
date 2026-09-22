"""Harbie Always-On Runner for PythonAnywhere.

Usage on PythonAnywhere:
  python -m harbie.run_harbie_pa

Requirements on PythonAnywhere:
- MW_API_KEY environment variable (or in config.json)
- Connects directly to local MySQL database `badangel$mw`
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys

from .common import (
    BURST_DURATION_SEC,
    BURST_MAX_LOOKUPS,
    BURST_MIN_LOOKUPS,
    DEFAULT_JITTER_PCT,
    DEFAULT_PACING_SEC,
    REST_MEAN_SEC,
    REST_MIN_SEC,
    REST_STD_DEV_SEC,
)
from .queue_store import QueueStore
from .worker import HarbieWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Harbie] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
LOG = logging.getLogger("harbie.pa")


def load_config() -> dict:
    """Load configuration from config.json or environment."""
    candidate_paths = [
        Path(os.getenv("CONFIG_PATH", "")),
        Path("/home/badangel/mw-mirror/harvey/harbie/config.json"),
        Path(__file__).parent / "config.json",
        Path.cwd() / "harbie" / "config.json",
        Path.cwd() / "config.json",
    ]
    cfg = {}
    for p in candidate_paths:
        if p and p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                LOG.info("Loaded configuration from %s", p)
                break
            except Exception as exc:
                LOG.warning("Could not parse config at %s: %s", p, exc)

    # Environment overrides
    mw_key = os.getenv("MW_API_KEY") or cfg.get("mw_key", "")
    db_host = os.getenv("DB_HOST") or cfg.get("mysql", {}).get("host", "badangel.mysql.pythonanywhere-services.com")
    db_user = os.getenv("DB_USER") or cfg.get("mysql", {}).get("user", "badangel")
    db_password = os.getenv("DB_PASSWORD") or cfg.get("mysql", {}).get("password", "")
    db_name = os.getenv("DB_NAME") or cfg.get("mysql", {}).get("database", "badangel$mw")

    return {
        "mw_key": mw_key,
        "mysql": {
            "host": db_host,
            "user": db_user,
            "password": db_password,
            "database": db_name,
        },
        # Micro-burst configuration
        "burst_min": int(os.getenv("BURST_MIN") or cfg.get("burst_min", BURST_MIN_LOOKUPS)),
        "burst_max": int(os.getenv("BURST_MAX") or cfg.get("burst_max", BURST_MAX_LOOKUPS)),
        "burst_duration_sec": float(os.getenv("BURST_DURATION_SEC") or cfg.get("burst_duration_sec", BURST_DURATION_SEC)),
        "rest_mean_sec": float(os.getenv("REST_MEAN_SEC") or cfg.get("rest_mean_sec", REST_MEAN_SEC)),
        "rest_std_dev_sec": float(os.getenv("REST_STD_DEV_SEC") or cfg.get("rest_std_dev_sec", REST_STD_DEV_SEC)),
        "rest_min_sec": float(os.getenv("REST_MIN_SEC") or cfg.get("rest_min_sec", REST_MIN_SEC)),
        "pacing_sec": int(os.getenv("PACING_SEC") or cfg.get("pacing_sec", DEFAULT_PACING_SEC)),
        "jitter_pct": int(os.getenv("JITTER_PCT") or cfg.get("jitter_pct", DEFAULT_JITTER_PCT)),
    }


def main():
    config = load_config()
    mw_key = config["mw_key"]
    if not mw_key:
        LOG.error("FATAL: MW_API_KEY is not set. Set MW_API_KEY environment variable or config.json.")
        sys.exit(1)

    import mysql.connector

    def get_pa_mysql():
        return mysql.connector.connect(
            host=config["mysql"]["host"],
            user=config["mysql"]["user"],
            password=config["mysql"]["password"],
            database=config["mysql"]["database"],
            autocommit=True,
            connection_timeout=10,
        )

    LOG.info("Connecting to PythonAnywhere database '%s'...", config["mysql"]["database"])
    store = QueueStore(get_pa_mysql, is_mysql=True)

    # Persistence callback: save snapshots directly into badangel$mw.snapshot
    def persist_snapshot(word: str, payload: any, result_kind: str):
        try:
            import hashlib
            payload_json = json.dumps(payload) if payload else "[]"
            sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            conn = get_pa_mysql()
            cur = conn.cursor()
            try:
                cur.execute(
                    """INSERT INTO snapshot (word, payload, sha256, worker, created_at)
                       VALUES (%s, %s, %s, 'HARBIE', NOW())
                       ON DUPLICATE KEY UPDATE updated_at = NOW()""",
                    (word, payload_json, sha),
                )
                conn.commit()
            finally:
                cur.close()
                conn.close()
        except Exception as exc:
            LOG.warning("Could not persist raw snapshot to snapshot table: %s", exc)

    worker = HarbieWorker(
        queue_store=store,
        mw_key=mw_key,
        mirror_saver=persist_snapshot,
        burst_min=config["burst_min"],
        burst_max=config["burst_max"],
        burst_duration_sec=config["burst_duration_sec"],
        rest_mean_sec=config["rest_mean_sec"],
        rest_std_dev_sec=config["rest_std_dev_sec"],
        rest_min_sec=config["rest_min_sec"],
        pacing_sec=config["pacing_sec"],
        jitter_pct=config["jitter_pct"],
        replay_mode=False,
    )

    LOG.info("Harbie PA Worker initialized. Starting 24/7 autonomous loop...")
    worker.run_loop()


if __name__ == "__main__":
    main()
