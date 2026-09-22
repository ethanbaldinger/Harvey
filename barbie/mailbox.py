"""Authenticated WSGI mailbox. No dictionary access and no mirror donations."""
import argparse
import hmac
import json
import os
import uuid
from wsgiref.simple_server import make_server

from .common import DEFAULT_SETTINGS, VERSION, db, settings, stamp


class Mailbox:
    def __init__(self, path, worker_token, manager_token, *, connection_factory=None):
        if min(len(worker_token), len(manager_token)) < 24 or worker_token == manager_token:
            raise ValueError("Supply two distinct tokens of at least 24 characters")
        self.path, self.worker_token, self.manager_token = path, worker_token, manager_token
        self.connect = connection_factory or (lambda: db(self.path))
        if connection_factory is not None:
            return
        with db(path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, word TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK(lane IN ('live','reserve')),
                    created TEXT NOT NULL, result TEXT, received TEXT,
                    collected TEXT, UNIQUE(provider,word));
                CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY, at TEXT, value TEXT);
            ''')
            conn.execute("INSERT OR IGNORE INTO control VALUES(1,?)", (json.dumps(DEFAULT_SETTINGS),))

    def action(self, route, body):
        with self.connect() as conn:
            if route == "/manager/assign":
                words, lane = body["words"], body.get("lane", "live")
                provider = json.loads(conn.execute("SELECT value FROM control WHERE id=1").fetchone()[0])["provider"]
                if lane not in ("live", "reserve") or not isinstance(words, list) or len(words) > 10000:
                    raise ValueError("Invalid assignment batch")
                for word in words:
                    if not isinstance(word, str) or not word.strip() or len(word) > 250:
                        raise ValueError("Invalid word")
                    conn.execute("INSERT OR IGNORE INTO jobs(id,word,provider,lane,created) VALUES(?,?,?,?,?)",
                                 (uuid.uuid4().hex, word.strip(), provider, lane, stamp()))
                return {"pending": conn.execute("SELECT count(*) FROM jobs WHERE result IS NULL").fetchone()[0]}
            if route == "/manager/settings":
                value = settings(body)
                conn.execute("UPDATE control SET value=? WHERE id=1", (json.dumps(value),))
                return value
            if route == "/manager/results":
                return {"results": [dict(row) for row in conn.execute(
                    "SELECT id,word,result,received FROM jobs WHERE result IS NOT NULL AND collected IS NULL ORDER BY received LIMIT 100") ]}
            if route == "/manager/ack":
                ids = body["ids"]
                if not isinstance(ids, list) or len(ids) > 100:
                    raise ValueError("Invalid acknowledgement batch")
                for job_id in ids:
                    conn.execute("UPDATE jobs SET collected=COALESCE(collected,?) WHERE id=? AND result IS NOT NULL", (stamp(), job_id))
                return {"ok": True}
            if route == "/manager/status":
                row = conn.execute("SELECT at,value FROM heartbeat WHERE id=1").fetchone()
                return {"version": VERSION, "last_report": dict(row) if row else None,
                        "pending": conn.execute("SELECT count(*) FROM jobs WHERE result IS NULL").fetchone()[0],
                        "awaiting_manager": conn.execute("SELECT count(*) FROM jobs WHERE result IS NOT NULL AND collected IS NULL").fetchone()[0]}
            if route == "/worker/sync":
                results = body.get("results", [])
                if not isinstance(results, list) or len(results) > 25:
                    raise ValueError("At most 25 results per sync")
                accepted = []
                for result in results:
                    row = conn.execute("SELECT word,provider,result FROM jobs WHERE id=?", (result["id"],)).fetchone()
                    if row is None or row["word"] != result["word"]:
                        raise ValueError("Unknown assignment")
                    if result.get("mode") != row["provider"]:
                        raise ValueError("Result provider differs from assignment")
                    if result.get("outcome") not in ("response", "not_held", "uncertain", "error"):
                        raise ValueError("Invalid result outcome")
                    if result["outcome"] == "response" and not isinstance(result.get("payload"), list):
                        raise ValueError("Dictionary response must be an array")
                    encoded = json.dumps(result, sort_keys=True)
                    if row["result"] is not None and row["result"] != encoded:
                        raise ValueError("Conflicting result for an existing assignment")
                    conn.execute("UPDATE jobs SET result=?,received=COALESCE(received,?) WHERE id=?", (encoded, stamp(), result["id"]))
                    accepted.append(result["id"])
                conn.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?,?)", (stamp(), json.dumps(body.get("report", {}))))
                control = json.loads(conn.execute("SELECT value FROM control WHERE id=1").fetchone()[0])
                provider = control["provider"]
                refresh = bool(body.get("refresh_reserve")) or body.get("report", {}).get("mode") != provider
                assignments = [dict(row) for row in conn.execute(
                    "SELECT id,word,lane,provider FROM jobs WHERE result IS NULL AND lane='live' AND provider=? ORDER BY created LIMIT 1", (provider,))]
                if refresh:
                    assignments += [dict(row) for row in conn.execute(
                        "SELECT id,word,lane,provider FROM jobs WHERE result IS NULL AND lane='reserve' AND provider=? ORDER BY created LIMIT 10000", (provider,))]
                return {"accepted": accepted, "assignments": assignments,
                        "settings": control, "reserve_refreshed": refresh and any(
                            job['lane'] == 'reserve' for job in assignments)}
            raise ValueError("Unknown route")

    def __call__(self, environ, start_response):
        route = environ.get("PATH_INFO", "")
        expected = self.worker_token if route.startswith("/worker/") else self.manager_token
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + expected).encode()):
            status, value = "401 Unauthorized", {"error": "unauthorized"}
        elif environ.get("REQUEST_METHOD") != "POST":
            status, value = "405 Method Not Allowed", {"error": "POST required"}
        else:
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
                if not 0 < length <= 8_000_000:
                    raise ValueError("Invalid request size")
                body = json.loads(environ["wsgi.input"].read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                value = self.action(route, body)
                status = "200 OK"
            except (ValueError, KeyError, TypeError):
                status, value = "400 Bad Request", {"error": "invalid request or conflicting assignment"}
            except Exception:
                status, value = "503 Service Unavailable", {"error": "mailbox unavailable; retry later"}
        raw = json.dumps(value).encode()
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(raw)))])
        return [raw]


def main():
    parser = argparse.ArgumentParser(description="BARBIE mailbox " + VERSION)
    parser.add_argument("--db", default="mailbox.sqlite3")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = Mailbox(args.db, os.environ["BARBIE_WORKER_TOKEN"], os.environ["BARBIE_MANAGER_TOKEN"])
    print("BARBIE mailbox", VERSION, "loopback port", args.port, flush=True)
    with make_server("127.0.0.1", args.port, app) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
