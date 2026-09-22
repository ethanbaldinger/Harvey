"""DARBIE & Universal Worker Daemon: Autonomous Merriam-Webster Quota Burner.

Features:
- Dual Mode: HTTPS Mailbox sync with Harvey / PythonAnywhere vs. Autonomous Local Reserve.
- Dynamic Hit Governor: Automatically shifts to zero-hit HOOVER shock absorber words
  if hit accrual is running ahead of the time-of-day slope or hits reach 950.
- Hard Quota Protection: Strict 1,000-call daily ceiling per calendar day (UTC).
- Zero External Dependencies: Standard library only (Python 3.8+).
- Pre-seeded Offline Reserve: Automatically seeds ~2,000 candidate and hoover words.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

try:
    from .common import DEFAULT_SETTINGS, VERSION, db, request, settings, stamp
except (ImportError, ValueError):
    from common import DEFAULT_SETTINGS, VERSION, db, request, settings, stamp

MW_ENDPOINT = "https://www.dictionaryapi.com/api/v3/references/collegiate/json"
HARD_DAILY_LIMIT = 1000
HOOVER_THRESHOLD = 950

LOG = logging.getLogger("worker")


class RemoteWorker:
    def __init__(
        self,
        path: Path,
        worker_name: str = "DARBIE",
        mailbox_url: str = "https://badangel.pythonanywhere.com/barbie",
        worker_token: str = "",
        mirror_url: str = "https://badangel.pythonanywhere.com",
        mirror_token: str = "",
        mw_key: str = "",
        *,
        replay_only: bool = False,
        dynamic_slope: bool = True,
    ):
        self.path = Path(path)
        self.worker_name = worker_name.upper()
        self.mailbox_url = mailbox_url.rstrip("/") if mailbox_url else ""
        self.worker_token = worker_token
        self.mirror_url = mirror_url.rstrip("/") if mirror_url else ""
        self.mirror_token = mirror_token
        self.mw_key = mw_key
        self.replay_only = replay_only
        self.dynamic_slope = dynamic_slope
        self.online = False
        self.hoover_tripped = False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_reserve_if_empty()

    def _init_db(self) -> None:
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
                    hits INTEGER NOT NULL DEFAULT 0,
                    misses INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, provider)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT OR IGNORE INTO meta VALUES('settings', ?)", (json.dumps(DEFAULT_SETTINGS),))
            # Recover in-flight crashed lookups
            for row in conn.execute("SELECT * FROM jobs WHERE state='inflight'").fetchall():
                res = self.result(row, "uncertain", None, "interrupted_request")
                conn.execute("UPDATE jobs SET state='done', result=? WHERE id=?", (json.dumps(res), row["id"]))

    def _seed_reserve_if_empty(self) -> None:
        """Seed initial reserve from seed_reserve.json if jobs table is brand new."""
        with db(self.path) as conn:
            count = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
            if count > 0:
                return

        seed_file = self.path.parent / "seed_reserve.json"
        if not seed_file.exists():
            alt = Path(__file__).parent / "seed_reserve.json"
            if alt.exists():
                seed_file = alt

        if not seed_file.exists():
            return

        try:
            data = json.loads(seed_file.read_text(encoding="utf-8"))
            candidates = data.get("candidates", [])
            hoover = data.get("hoover", [])
            with db(self.path) as conn:
                for idx, w in enumerate(candidates):
                    conn.execute(
                        "INSERT OR IGNORE INTO jobs(id, word, lane, provider) VALUES(?, ?, 'reserve', 'mw_live')",
                        (f"seed_cand_{idx}", w.strip().lower()),
                    )
                for idx, w in enumerate(hoover):
                    conn.execute(
                        "INSERT OR IGNORE INTO jobs(id, word, lane, provider) VALUES(?, ?, 'hoover', 'mw_live')",
                        (f"seed_hoov_{idx}", w.strip().lower()),
                    )
            LOG.info("[%s] Pre-seeded reserve: %d candidates, %d hoover words.", self.worker_name, len(candidates), len(hoover))
        except Exception as exc:
            LOG.warning("[%s] Could not load seed_reserve.json: %s", self.worker_name, exc)

    def result(self, job: Any, outcome: str, payload: Any, error: Optional[str] = None) -> Dict[str, Any]:
        return {
            "id": job["id"],
            "word": job["word"],
            "mode": job["provider"],
            "outcome": outcome,
            "payload": payload,
            "error": error,
            "started": job["started"],
            "completed": stamp(),
            "worker": self.worker_name,
        }

    def config(self) -> Dict[str, Any]:
        with db(self.path) as conn:
            return json.loads(conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])

    def report(self) -> Dict[str, Any]:
        cfg = self.config()
        provider = cfg.get("provider", "mw_live")
        today = date.today().isoformat()
        with db(self.path) as conn:
            rows = {
                row["provider"]: dict(row)
                for row in conn.execute("SELECT * FROM ledger WHERE day=?", (today,))
            }
            return {
                "version": VERSION,
                "worker": self.worker_name,
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
        if not self.mailbox_url or not self.worker_token:
            return False

        today = date.today().isoformat()
        with db(self.path) as conn:
            last = conn.execute("SELECT value FROM meta WHERE key='reserve_day'").fetchone()
            refresh = last is None or last[0] != today
            results = [json.loads(row[0]) for row in conn.execute("SELECT result FROM jobs WHERE state='done' LIMIT 25")]

        try:
            _, reply = request(
                self.mailbox_url + "/worker/sync",
                self.worker_token,
                {"results": results, "report": self.report(), "refresh_reserve": refresh, "worker": self.worker_name},
            )
            new_settings = settings(reply["settings"])
            with db(self.path) as conn:
                sent = {res["id"] for res in results}
                for job_id in reply.get("accepted", []):
                    if job_id in sent:
                        conn.execute("UPDATE jobs SET state='delivered', result=NULL WHERE id=? AND state='done'", (job_id,))
                for job in reply.get("assignments", []):
                    lane = job.get("lane", "reserve")
                    if lane in ("live", "reserve", "hoover") and isinstance(job.get("word"), str):
                        conn.execute(
                            "INSERT OR IGNORE INTO jobs(id, word, lane, provider) VALUES(?, ?, ?, ?)",
                            (job["id"], job["word"].strip().lower(), lane, job.get("provider", new_settings["provider"])),
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
        """Execute one pending lookup prioritizing LIVE -> RESERVE -> HOOVER with dynamic hit slope protection."""
        cfg = self.config()
        provider = cfg.get("provider", "mw_live")
        if cfg.get("paused") or (self.replay_only and provider != "mirror_replay") or (provider == "mw_live" and not self.mw_key):
            return False

        today = date.today().isoformat()
        now_utc = datetime.now(timezone.utc)

        with db(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            next_at = conn.execute("SELECT value FROM meta WHERE key='next_at'").fetchone()
            if next_at and time.time() < float(next_at[0]):
                return False

            conn.execute("INSERT OR IGNORE INTO ledger(day, provider) VALUES(?, ?)", (today, provider))
            quota = conn.execute("SELECT * FROM ledger WHERE day=? AND provider=?", (today, provider)).fetchone()
            daily_limit = min(int(cfg.get("daily_limit", HARD_DAILY_LIMIT)), HARD_DAILY_LIMIT)
            today_calls = quota["requests"]
            today_hits = quota["hits"]

            if today_calls >= daily_limit:
                return False

            # Evaluate Dynamic Hit Slope Governor
            seconds_elapsed = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second
            day_fraction = max(0.01, min(1.0, seconds_elapsed / 86400.0))
            target_hits = int(HOOVER_THRESHOLD * day_fraction)
            
            slope_steep = (today_hits > target_hits + 20 and today_hits > 40)
            enforce_hoover = (today_hits >= HOOVER_THRESHOLD) or (self.dynamic_slope and slope_steep)

            if enforce_hoover and not self.hoover_tripped:
                self.hoover_tripped = True
                LOG.info(
                    "[%s] Hit governor active (hits=%d, target=%d, cap=%d). Throttling candidate lane -> HOOVER.",
                    self.worker_name, today_hits, target_hits, HOOVER_THRESHOLD
                )
            elif not enforce_hoover and self.hoover_tripped and today_hits < HOOVER_THRESHOLD:
                self.hoover_tripped = False
                LOG.info(
                    "[%s] Hit governor relaxed (hits=%d <= target=%d). Resuming candidate discovery.",
                    self.worker_name, today_hits, target_hits
                )

            # Lane selection hierarchy:
            # 1. LIVE assignments always take priority
            job = conn.execute(
                "SELECT * FROM jobs WHERE state='pending' AND lane='live' ORDER BY rowid LIMIT 1"
            ).fetchone()

            # 2. Fallback selection: HOOVER if hit governor tripped, else RESERVE
            if job is None:
                if enforce_hoover:
                    job = conn.execute(
                        "SELECT * FROM jobs WHERE state='pending' AND lane='hoover' ORDER BY rowid LIMIT 1"
                    ).fetchone()
                else:
                    job = conn.execute(
                        "SELECT * FROM jobs WHERE state='pending' AND lane='reserve' ORDER BY rowid LIMIT 1"
                    ).fetchone()
                    if job is None:
                        job = conn.execute(
                            "SELECT * FROM jobs WHERE state='pending' AND lane='hoover' ORDER BY rowid LIMIT 1"
                        ).fetchone()

            if job is None:
                return False

            started = stamp()
            conn.execute("UPDATE jobs SET state='inflight', started=?, day=? WHERE id=?", (started, today, job["id"]))
            conn.execute("UPDATE ledger SET reserved=reserved+1, requests=requests+1 WHERE day=? AND provider=?", (today, provider))
            gap = random.uniform(cfg.get("gap_min", 4), cfg.get("gap_max", 12))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('next_at', ?)", (str(time.time() + gap),))
            job = dict(job)
            job["started"] = started

        # Network dispatch
        result_kind = "MISS"
        billed = 0
        try:
            if provider == "mirror_replay":
                code, payload = request(
                    self.mirror_url + "/v1/collegiate/" + quote(job["word"], safe="") + "?held=1",
                    self.mirror_token,
                )
                if code == 200 and isinstance(payload, list):
                    result = self.result(job, "response", payload)
                    billed = int(any(isinstance(item, dict) for item in payload))
                    result_kind = "HIT" if billed else "MISS"
                else:
                    result = self.result(job, "not_held", None)
                    result_kind = "MISS"
            else:
                code, payload = request(
                    MW_ENDPOINT + "/" + quote(job["word"], safe="") + "?" + urlencode({"key": self.mw_key}),
                    "",
                )
                if code == 200 and isinstance(payload, list):
                    if not payload:
                        result = self.result(job, "not_held", payload)
                        result_kind = "MISS"
                    elif isinstance(payload[0], str):
                        result = self.result(job, "not_held", payload)
                        result_kind = "MISS"
                    elif isinstance(payload[0], dict) and "meta" in payload[0]:
                        result = self.result(job, "response", payload)
                        billed = 1
                        result_kind = "HIT"
                    else:
                        result = self.result(job, "response", payload)
                        billed = int(any(isinstance(item, dict) for item in payload))
                        result_kind = "HIT" if billed else "MISS"
                else:
                    raise ValueError("Invalid dictionary response")
            uncertain = False
        except Exception as exc:
            result = self.result(job, "uncertain", None, f"lookup_failed: {exc}")
            uncertain = True

        with db(self.path) as conn:
            conn.execute("UPDATE jobs SET state='done', result=? WHERE id=?", (json.dumps(result), job["id"]))
            conn.execute(
                """
                UPDATE ledger 
                SET reserved=reserved-?, billed=billed+?, hits=hits+?, misses=misses+? 
                WHERE day=? AND provider=?
                """,
                (
                    0 if uncertain else 1,
                    billed,
                    1 if result_kind == "HIT" else 0,
                    1 if result_kind == "MISS" and not uncertain else 0,
                    today,
                    provider,
                ),
            )
            # Re-read daily totals for display
            row = conn.execute("SELECT requests, hits, misses FROM ledger WHERE day=? AND provider=?", (today, provider)).fetchone()
            tot_calls = row["requests"] if row else 0
            tot_hits = row["hits"] if row else 0
            tot_misses = row["misses"] if row else 0

        ts_str = datetime.now().strftime("%H:%M:%S")
        LOG.info(
            "[%s] [%s] Lookup #%d: '%s' [%s] -> %s (Today: %d calls, %d hits, %d misses)",
            ts_str, self.worker_name, tot_calls, job["word"], job["lane"], result_kind,
            tot_calls, tot_hits, tot_misses
        )
        return True

    def run_loop(self, poll_interval: float = 4.0) -> None:
        """Continuous execution loop."""
        LOG.info("Starting %s worker daemon. Press Ctrl+C to stop.", self.worker_name)
        while True:
            try:
                self.sync()
                did_work = self.lookup_one()
                if not did_work:
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                LOG.info("Stopping %s...", self.worker_name)
                break
            except Exception as exc:
                LOG.warning("[%s] Cycle error: %s", self.worker_name, exc)
                time.sleep(poll_interval)


def show_status(db_path: Path, worker_name: str) -> None:
    """Print formatted terminal status."""
    if not db_path.exists():
        print(f"No local database found at {db_path}. Has the worker run yet?")
        return

    today = date.today().isoformat()
    with db(db_path) as conn:
        ledger = conn.execute("SELECT * FROM ledger WHERE day=?", (today,)).fetchall()
        jobs_summary = conn.execute(
            "SELECT lane, state, count(*) as cnt FROM jobs GROUP BY lane, state"
        ).fetchall()

    print("\n" + "=" * 68)
    print(f"  {worker_name} WORKER STATUS & QUOTA DASHBOARD (v{VERSION})")
    print("=" * 68)
    print(f" Date                : {today}")
    print(f" Database            : {db_path.resolve()}")

    calls = hits = misses = 0
    for row in ledger:
        calls += row["requests"]
        hits += row["hits"]
        misses += row["misses"]

    hit_pct = (hits / calls * 100.0) if calls > 0 else 0.0
    print(f" Daily Calls Spent   : {calls:,} / {HARD_DAILY_LIMIT:,} (Remaining: {max(0, HARD_DAILY_LIMIT - calls):,})")
    print(f" Daily Hits Accrued  : {hits:,} / {HOOVER_THRESHOLD:,} ({hit_pct:.1f}% hit rate)")
    print(f" Daily Misses        : {misses:,}")
    print("-" * 68)
    print(" Queue Backlog Summary:")
    if jobs_summary:
        by_lane = {}
        for r in jobs_summary:
            lane = r["lane"]
            if lane not in by_lane:
                by_lane[lane] = {}
            by_lane[lane][r["state"]] = r["cnt"]
        for lane, states in by_lane.items():
            state_str = ", ".join(f"{s}: {c:,}" for s, c in sorted(states.items()))
            print(f"   [{lane.upper():<8}] {state_str}")
    else:
        print("   (Queue is currently empty)")
    print("=" * 68 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Remote Worker Daemon v{VERSION}")
    parser.add_argument("--config", default="worker.json", help="Path to worker.json configuration")
    parser.add_argument("--status", action="store_true", help="Display live status summary and exit")
    parser.add_argument("--once", action="store_true", help="Run a single sync & lookup step and exit")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        # Look in script directory
        alt = Path(__file__).parent / "worker.json"
        if alt.exists():
            cfg_path = alt

    if args.status:
        db_p = Path("worker.sqlite3")
        w_name = "WORKER"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                db_p = Path(cfg.get("database", "worker.sqlite3"))
                w_name = cfg.get("worker_name", "WORKER")
            except Exception:
                pass
        show_status(db_p, w_name)
        return

    if not cfg_path.exists():
        LOG.error("Configuration file '%s' not found. Run Setup.bat first.", cfg_path)
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    worker = RemoteWorker(
        path=Path(cfg.get("database", "worker.sqlite3")),
        worker_name=cfg.get("worker_name", "DARBIE"),
        mailbox_url=cfg.get("mailbox_url", "https://badangel.pythonanywhere.com/barbie"),
        worker_token=cfg.get("worker_token", ""),
        mirror_url=cfg.get("mirror_url", "https://badangel.pythonanywhere.com"),
        mirror_token=cfg.get("mirror_token", ""),
        mw_key=cfg.get("mw_key", ""),
        replay_only=cfg.get("replay_only", False),
        dynamic_slope=cfg.get("dynamic_slope", True),
    )

    if args.once:
        worker.sync()
        worker.lookup_one()
    else:
        worker.run_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    main()
