"""One-shot test HARVEY -> mailbox -> BARBIE -> mailbox -> test HARVEY demo.

Default is entirely local. --mirror-env opts into replay-only PA reads using
the existing mirror credential file; it never displays credentials.
"""
import argparse
import json
import secrets
import tempfile
import threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .common import VERSION, db, request
from .mailbox import Mailbox
from .manager import collect
from .worker import Worker


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="BARBIE replay demo " + VERSION)
    parser.add_argument("--mirror-env", type=Path)
    parser.add_argument("--word", default="brominate")
    args = parser.parse_args()
    print("BARBIE", VERSION, "end-to-end replay demo", flush=True)
    servers, threads = [], []

    def serve(app):
        server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        servers.append(server)
        threads.append(thread)
        thread.start()
        return "http://127.0.0.1:" + str(server.server_port)

    try:
        with tempfile.TemporaryDirectory(prefix="barbie-demo-") as folder:
            root = Path(folder)
            worker_token, manager_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            mailbox = Mailbox(root / "mailbox.sqlite3", worker_token, manager_token)
            mailbox_url = serve(mailbox)
            if args.mirror_env:
                values = {}
                for line in args.mirror_env.read_text(encoding="utf-8-sig").splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        key, value = line.split("=", 1)
                        values[key.strip()] = value.strip().strip("\"'")
                mirror_url = values["MIRROR_BASE_URL"]
                mirror_token = values["MIRROR_CLIENT_KEY"] + "." + values["MIRROR_CLIENT_TOKEN"]
                source = "PythonAnywhere held-only replay"
            else:
                def fixture(environ, start_response):
                    if environ.get("QUERY_STRING") != "held=1":
                        raise RuntimeError("Replay guard missing")
                    start_response("200 OK", [("Content-Type", "application/json")])
                    return [json.dumps([{"meta": {"id": args.word}, "shortdef": ["local test fixture"]}]).encode()]
                mirror_url, mirror_token, source = serve(fixture), "fixture", "local fixture"
            request(mailbox_url + "/manager/assign", manager_token, {"words": [args.word], "lane": "reserve"})
            worker = Worker(root / "worker.sqlite3", mailbox_url, worker_token, mirror_url, mirror_token)
            assert worker.sync(), "Initial sync failed"
            assert worker.lookup_one(), "No assignment dispatched"
            assert worker.sync(), "Result delivery failed"
            assert collect(mailbox_url, manager_token, root / "manager.sqlite3") == 1
            with db(root / "manager.sqlite3") as conn:
                result = json.loads(conn.execute("SELECT result FROM results").fetchone()[0])
            summary = {"version": VERSION, "source": source, "word": args.word,
                       "outcome": result["outcome"], "response_items": len(result["payload"] or []),
                       "manager_collected": 1, "repeat_collection": collect(mailbox_url, manager_token, root / "manager.sqlite3"),
                       "test_quota": worker.report()["test_quota"]}
            print(json.dumps(summary, indent=2))
            if result["outcome"] != "response":
                raise RuntimeError("Demo did not receive a stored response")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


if __name__ == "__main__":
    main()
