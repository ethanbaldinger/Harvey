"""DARBIE Worker: Persistent remote lookup worker for third laptop execution.

Connects to PythonAnywhere mailbox over HTTPS.
Autonomous fallback on local SQLite reserve when laptop is disconnected from manager.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import logging
import os
from pathlib import Path
import random
import signal
import sys
import time
from urllib.parse import quote, urlencode

try:
    from .common import DEFAULT_SETTINGS, VERSION, WORKER_NAME, db, request, settings, stamp
except (ImportError, ValueError):
    from common import DEFAULT_SETTINGS, VERSION, WORKER_NAME, db, request, settings, stamp

MW_ENDPOINT = "https://www.dictionaryapi.com/api/v3/references/collegiate/json"
HARD_DAILY_LIMIT = 1000

LOG = logging.getLogger("darbie.worker")


class DarbieWorker:
    def __init__(
        self,
        path: Path,
        mailbox_url: str,
        worker_token: str,
        mirror_url: str,
        mirror_token: str,
        mw_key: str = "",
        *,
        replay_only: bool = True,
    ):
        self.path = Path(path)
        self.mailbox_url = mailbox_url.rstrip("/")
        self.worker_token = worker_token
        self.mirror_url = mirror_url.rstrip("/")
        self.mirror_token = mirror_token
        self.mw_key = mw_key
        self.replay_only = replay_only
        self.online = False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with db(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    word TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    result TEXT,
                    started TEXT,
                    day TEXT
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    day TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    reserved INTEGER NOT NULL DEFAULT 0,
                    billed INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, provider)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO meta VALUES('settings', ?)", (json.dumps(DEFAULT_SETTINGS),))
            # Handle in-flight crashes
            for row in conn.execute("SELECT * FROM jobs WHERE state='inflight'").fetchall():
                result = self.result(row, "uncertain", None, "interrupted_request")
                conn.execute("UPDATE jobs SET state='done', result=? WHERE id=?", (json.dumps(result), row["id"]))

    @staticmethod
    def result(job, outcome, payload, error=None):
        return {
            "id": job["id"],
            "word": job["word"],
            "mode": job["provider"],
            "outcome": outcome,
            "payload": payload,
            "error": error,
            "started": job["started"],
            "completed": stamp(),
            "worker": WORKER_NAME,
        }

    def config(self):
        with db(self.path) as conn:
            return json.loads(conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])

    def report(self):
        provider = self.config()["provider"]
        today = date.today().isoformat()
        with db(self.path) as conn:
            rows = {
                row["provider"]: dict(row)
                for row in conn.execute("SELECT * FROM ledger WHERE day=?", (today,))
            }
            return {
                "version": VERSION,
                "worker": WORKER_NAME,
                "mode": provider,
                "at": stamp(),
                "test_quota": rows.get("mirror_replay", {}),
                "live_quota": rows.get("mw_live", {}),
                "blocked": (
                    "replay_only" if self.replay_only and provider != "mirror_replay"
                    else "missing_mw_key" if provider == "mw_live" and not self.mw_key
                    else None
                ),
                "pending": conn.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0],
                "awaiting_delivery": conn.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0],
            }

    def sync(self) -> bool:
        """Exchange results and retrieve live/reserve assignments from PythonAnywhere mailbox."""
        today = date.today().isoformat()
        with db(self.path) as conn:
            last = conn.execute("SELECT value FROM meta WHERE key='reserve_day'").fetchone()
            refresh = last is None or last[0] != today
            results = [json.loads(row[0]) for row in conn.execute("SELECT result FROM jobs WHERE state='done' LIMIT 25")]

        try:
            _, reply = request(
                self.mailbox_url + "/worker/sync",
                self.worker_token,
                {"results": results, "report": self.report(), "refresh_reserve": refresh, "worker": WORKER_NAME},
            )
            new_settings = settings(reply["settings"])
            with db(self.path) as conn:
                sent = {result["id"] for result in results}
                for job_id in reply.get("accepted", []):
                    if job_id in sent:
                        conn.execute("UPDATE jobs SET state='delivered', result=NULL WHERE id=? AND state='done'", (job_id,))
                for job in reply.get("assignments", []):
                    if job.get("lane") in ("live", "reserve") and isinstance(job.get("word"), str):
                        conn.execute(
                            "INSERT OR IGNORE INTO jobs(id, word, lane, provider) VALUES(?, ?, ?, ?)",
                            (job["id"], job["word"].strip().lower(), job["lane"], job.get("provider", new_settings["provider"])),
                        )
                conn.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(new_settings),))
                if reply.get("reserve_refreshed"):
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('reserve_day', ?)", (today,))
            self.online = True
            return True
        except Exception:
            self.online = False
            return False

    def lookup_one(self) -> bool:
        """Execute one pending lookup prioritizing LIVE lane before RESERVE lane."""
        config = self.config()
        provider = config["provider"]
        if config["paused"] or (self.replay_only and provider != "mirror_replay") or (provider == "mw_live" and not self.mw_key):
            return False

        today = date.today().isoformat()
        with db(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            next_at = conn.execute("SELECT value FROM meta WHERE key='next_at'").fetchone()
            if next_at and time.time() < float(next_at[0]):
                return False

            conn.execute("INSERT OR IGNORE INTO ledger(day, provider) VALUES(?, ?)", (today, provider))
            quota = conn.execute("SELECT * FROM ledger WHERE day=? AND provider=?", (today, provider)).fetchone()
            daily_limit = min(int(config.get("daily_limit", HARD_DAILY_LIMIT)), HARD_DAILY_LIMIT)
            if quota["billed"] + quota["reserved"] >= daily_limit:
                return False

            # Strict 2-tier ordering: 'live' (0) precedes 'reserve' (1)
            job = conn.execute(
                """SELECT * FROM jobs 
                   WHERE state='pending' AND provider=? 
                   ORDER BY CASE lane WHEN 'live' THEN 0 ELSE 1 END, rowid 
                   LIMIT 1""",
                (provider,),
            ).fetchone()
            if job is None:
                return False

            started = stamp()
            conn.execute("UPDATE jobs SET state='inflight', started=?, day=? WHERE id=?", (started, today, job["id"]))
            conn.execute("UPDATE ledger SET reserved=reserved+1, requests=requests+1 WHERE day=? AND provider=?", (today, provider))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('next_at', ?)", (str(time.time() + random.uniform(config["gap_min"], config["gap_max"])),))
            job = dict(job)
            job["started"] = started

        # Network dispatch
        try:
            if provider == "mirror_replay":
                code, payload = request(
                    self.mirror_url + "/v1/collegiate/" + quote(job["word"], safe="") + "?held=1",
                    self.mirror_token,
                )
            else:
                code, payload = request(
                    MW_ENDPOINT + "/" + quote(job["word"], safe="") + "?" + urlencode({"key": self.mw_key}),
                    "",
                )

            if code == 204 and provider == "mirror_replay":
                result, billed = self.result(job, "not_held", None), 0
            elif code == 200 and isinstance(payload, list):
                result = self.result(job, "response", payload)
                billed = int(any(isinstance(item, dict) for item in payload))
            else:
                raise ValueError("Invalid dictionary response")
            uncertain = False
        except Exception:
            result, billed, uncertain = self.result(job, "uncertain", None, "lookup_failed"), 0, True

        with db(self.path) as conn:
            conn.execute("UPDATE jobs SET state='done', result=? WHERE id=?", (json.dumps(result), job["id"]))
            conn.execute(
                "UPDATE ledger SET reserved=reserved-?, billed=billed+? WHERE day=? AND provider=?",
                (0 if uncertain else 1, billed, today, provider),
            )
            gap = random.uniform(config["gap_min"], config["gap_max"])
            conn.execute("INSERT OR REPLACE INTO meta VALUES('next_at', ?)", (str(time.time() + gap),))

        LOG.info("[%s] Completed '%s' [%s] -> %s (billed=%d)", WORKER_NAME, job["word"], job["lane"], result["outcome"], billed)
        return True

    def run_loop(self, poll_interval: float = 5.0):
        """Continuous execution loop."""
        LOG.info("Starting %s worker daemon. Press Ctrl+C to stop.", WORKER_NAME)
        while True:
            try:
                self.sync()
                did_work = self.lookup_one()
                if not did_work:
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                LOG.info("Stopping %s...", WORKER_NAME)
                break
            except Exception as exc:
                LOG.warning("Worker cycle error: %s", exc)
                time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description=f"{WORKER_NAME} Worker Daemon v{VERSION}")
    parser.add_argument("--config", default="worker.json", help="Path to worker.json configuration")
    parser.add_argument("--once", action="store_true", help="Run a single sync & lookup step and exit")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        LOG.error("Configuration file '%s' not found. Run Setup.bat first or copy worker.example.json.", cfg_path)
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    worker = DarbieWorker(
        path=Path(cfg.get("database", "worker.sqlite3")),
        mailbox_url=cfg.get("mailbox_url", "https://badangel.pythonanywhere.com/barbie"),
        worker_token=cfg.get("worker_token", ""),
        mirror_url=cfg.get("mirror_url", ""),
        mirror_token=cfg.get("mirror_token", ""),
        mw_key=cfg.get("mw_key", ""),
        replay_only=cfg.get("replay_only", False),
    )

    if args.once:
        worker.sync()
        worker.lookup_one()
    else:
        worker.run_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    main()
