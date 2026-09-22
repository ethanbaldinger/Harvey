"""Persistent lookup worker; remote provider selection, locally held credentials."""
import argparse
import json
import os
import random
import signal
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    from .common import DEFAULT_SETTINGS, VERSION, db, request, settings, stamp
except (ImportError, ValueError):
    from common import DEFAULT_SETTINGS, VERSION, db, request, settings, stamp

MW_ENDPOINT = "https://www.dictionaryapi.com/api/v3/references/collegiate/json"
HARD_DAILY_LIMIT = 1000



@contextmanager
def singleton(path):
    """OS releases the worker lock after a crash. Leave the lock file in place."""
    handle = open(str(path) + ".lock", "a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


class Worker:
    def __init__(self, path, mailbox_url, worker_token, mirror_url, mirror_token, mw_key="", *, replay_only=True):
        self.path = path
        self.mailbox_url, self.worker_token = mailbox_url.rstrip("/"), worker_token
        self.mirror_url, self.mirror_token = mirror_url.rstrip("/"), mirror_token
        self.mw_key = mw_key
        self.replay_only = replay_only
        self.online = False
        with db(path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, word TEXT NOT NULL, lane TEXT NOT NULL,
                    provider TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', result TEXT,
                    started TEXT, day TEXT);
                CREATE TABLE IF NOT EXISTS ledger (
                    day TEXT NOT NULL, provider TEXT NOT NULL, reserved INTEGER NOT NULL DEFAULT 0,
                    billed INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day,provider));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            conn.execute("INSERT OR IGNORE INTO meta VALUES('settings',?)", (json.dumps(DEFAULT_SETTINGS),))
            # A request interrupted by a crash may have completed remotely. Retain
            # the reserved quota unit and report uncertainty, never blindly retry.
            for row in conn.execute("SELECT * FROM jobs WHERE state='inflight'").fetchall():
                result = self.result(row, "uncertain", None, "interrupted_request")
                conn.execute("UPDATE jobs SET state='done',result=? WHERE id=?", (json.dumps(result), row["id"]))

    @staticmethod
    def result(job, outcome, payload, error=None):
        return {"id": job["id"], "word": job["word"], "mode": job["provider"],
                "outcome": outcome, "payload": payload, "error": error,
                "started": job["started"], "completed": stamp()}

    def config(self):
        with db(self.path) as conn:
            return json.loads(conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])

    def report(self):
        provider = self.config()["provider"]
        with db(self.path) as conn:
            rows = {row["provider"]: dict(row) for row in conn.execute("SELECT * FROM ledger WHERE day=?", (date.today().isoformat(),))}
            return {"version": VERSION, "mode": provider, "at": stamp(),
                    "test_quota": rows.get("mirror_replay", {}), "live_quota": rows.get("mw_live", {}),
                    "blocked": ("replay_only" if self.replay_only and provider != "mirror_replay" else
                                "missing_mw_key" if provider == "mw_live" and not self.mw_key else None),
                    "pending": conn.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0],
                    "awaiting_delivery": conn.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0]}

    def sync(self):
        today = date.today().isoformat()
        with db(self.path) as conn:
            last = conn.execute("SELECT value FROM meta WHERE key='reserve_day'").fetchone()
            refresh = last is None or last[0] != today
            results = [json.loads(row[0]) for row in conn.execute("SELECT result FROM jobs WHERE state='done' LIMIT 25")]
        try:
            _, reply = request(self.mailbox_url + "/worker/sync", self.worker_token,
                               {"results": results, "report": self.report(), "refresh_reserve": refresh})
            new_settings = settings(reply["settings"])
            with db(self.path) as conn:
                # Only acknowledge the exact results submitted in this exchange.
                sent = {result["id"] for result in results}
                for job_id in reply["accepted"]:
                    if job_id not in sent:
                        raise ValueError("Unexpected receipt")
                    conn.execute("UPDATE jobs SET state='delivered',result=NULL WHERE id=? AND state='done'", (job_id,))
                for job in reply["assignments"]:
                    if job["lane"] not in ("live", "reserve") or not isinstance(job["word"], str):
                        raise ValueError("Invalid assignment")
                    if job["provider"] != new_settings["provider"]:
                        raise ValueError("Wrong assignment provider")
                    existing = conn.execute("SELECT word,lane,provider FROM jobs WHERE id=?", (job["id"],)).fetchone()
                    if existing and any(existing[k] != job[k] for k in ("word", "lane", "provider")):
                        raise ValueError("Assignment identity changed")
                    conn.execute("INSERT OR IGNORE INTO jobs(id,word,lane,provider) VALUES(?,?,?,?)", (job["id"], job["word"], job["lane"], job["provider"]))
                conn.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(new_settings),))
                if reply.get("reserve_refreshed"):
                    conn.execute("INSERT OR REPLACE INTO meta VALUES('reserve_day',?)", (today,))
            self.online = True
            return True
        except (OSError, ValueError, KeyError, TypeError):
            self.online = False
            return False

    def lookup_one(self):
        config = self.config()
        provider = config["provider"]
        if (config["paused"] or (self.replay_only and provider != "mirror_replay") or
                (provider == "mw_live" and not self.mw_key)):
            return False
        today = date.today().isoformat()  # Test clock matches HARVEY's local-day convention.
        with db(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            next_at = conn.execute("SELECT value FROM meta WHERE key='next_at'").fetchone()
            if next_at and time.time() < float(next_at[0]):
                return False
            conn.execute("INSERT OR IGNORE INTO ledger(day,provider) VALUES(?,?)", (today, provider))
            quota = conn.execute("SELECT * FROM ledger WHERE day=? AND provider=?", (today, provider)).fetchone()
            daily_limit = min(int(config.get("daily_limit", HARD_DAILY_LIMIT)), HARD_DAILY_LIMIT)
            if quota["billed"] + quota["reserved"] >= daily_limit:
                return False
            job = conn.execute("SELECT * FROM jobs WHERE state='pending' AND provider=? ORDER BY CASE lane WHEN 'live' THEN 0 ELSE 1 END,rowid LIMIT 1", (provider,)).fetchone()
            if job is None:
                return False
            started = stamp()
            conn.execute("UPDATE jobs SET state='inflight',started=?,day=? WHERE id=?", (started, today, job["id"]))
            conn.execute("UPDATE ledger SET reserved=reserved+1,requests=requests+1 WHERE day=? AND provider=?", (today, provider))
            # Persist before dispatch, including pacing across restarts.
            conn.execute("INSERT OR REPLACE INTO meta VALUES('next_at',?)", (str(time.time() + random.uniform(config["gap_min"], config["gap_max"])),))
            job = dict(job)
            job["started"] = started
        try:
            if provider == "mirror_replay":
                code, payload = request(self.mirror_url + "/v1/collegiate/" + quote(job["word"], safe="") + "?held=1", self.mirror_token)
            else:
                code, payload = request(MW_ENDPOINT + "/" + quote(job["word"], safe="") + "?" + urlencode({"key": self.mw_key}), "")
            if code == 204 and provider == "mirror_replay":
                result, billed = self.result(job, "not_held", None), 0
            elif code == 200 and isinstance(payload, list):
                result = self.result(job, "response", payload)
                billed = int(any(isinstance(item, dict) for item in payload))
            else:
                raise ValueError("Invalid dictionary response")
            uncertain = False
        except (OSError, ValueError):
            result, billed, uncertain = self.result(job, "uncertain", None, "lookup_failed"), 0, True
        with db(self.path) as conn:
            conn.execute("UPDATE jobs SET state='done',result=? WHERE id=?", (json.dumps(result), job["id"]))
            conn.execute("UPDATE ledger SET reserved=reserved-?,billed=billed+? WHERE day=? AND provider=?", (0 if uncertain else 1, billed, today, provider))
            # Keep at least the configured gap after completion, too.
            conn.execute("INSERT OR REPLACE INTO meta VALUES('next_at',?)", (str(time.time() + random.uniform(config["gap_min"], config["gap_max"])),))
        return True


def main():
    parser = argparse.ArgumentParser(description="BARBIE " + VERSION)
    parser.add_argument("--config", default="worker.json")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    path = config_path.parent / config.get("database", "worker.sqlite3")
    stop_path = Path(str(path) + ".stop")
    if args.stop:
        stop_path.write_text(stamp(), encoding="utf-8")
        print("BARBIE", VERSION, "stop requested; wait for the worker's stopped message")
        return
    credentials = {}
    if config.get('credentials_file'):
        credentials = json.loads((config_path.parent / config['credentials_file']).read_text(encoding='utf-8'))
    def credential(name, required=True):
        val = credentials.get(name)
        if not val and config.get(name + '_env'):
            val = os.environ.get(config[name + '_env'])
        if required and not val:
            raise KeyError(f"Missing credential {name}")
        return val or ""
    stop_requested = False

    def stop(*_):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    mw_key = credential('mw_key', required=False)
    replay_only = config.get("replay_only", True)
    with singleton(path):
        initial_stop = stop_path.read_text() if stop_path.exists() else None
        worker = Worker(path, config["mailbox_url"], credential('worker_token'),
                        config["mirror_url"], credential('mirror_token'),
                        mw_key=mw_key, replay_only=replay_only)
        print("BARBIE", VERSION, worker.config()["provider"], "Ctrl+C or Stop to finish.", flush=True)
        next_sync = 0.0
        last_provider = worker.config()["provider"]
        while not stop_requested:
            if stop_path.exists() and stop_path.read_text() != initial_stop:
                break
            if time.monotonic() >= next_sync:
                was_online = worker.online
                worker.sync()
                if was_online != worker.online:
                    print("BARBIE", VERSION, "mailbox connected" if worker.online else "mailbox unavailable; using reserve", flush=True)
                next_sync = time.monotonic() + (5 if worker.online else 30)
            if worker.config()["provider"] != last_provider:
                last_provider = worker.config()["provider"]
                print("BARBIE", VERSION, "provider:", last_provider, flush=True)
            if stop_requested or (stop_path.exists() and stop_path.read_text() != initial_stop):
                break
            if worker.lookup_one():
                if worker.online:
                    worker.sync()  # Deliver each result immediately when possible.
            time.sleep(0.2)
        worker.sync()  # Bounded by the network timeout; pending results stay on disk.
        print("BARBIE", VERSION, "stopped safely; undelivered results retained.", flush=True)


if __name__ == "__main__":
    main()
