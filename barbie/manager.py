"""Test HARVEY: publish assignments/settings and durably collect returned results.

Intentionally does not import live mw_manager, touch legacy MySQL, or donate
replayed snapshots back into the mirror. Live integration is a separate step.
"""
import argparse
import json
import os

from .common import VERSION, db, request, stamp


def collect(url, token, path):
    _, response = request(url.rstrip("/") + "/manager/results", token, {})
    with db(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, word TEXT, result TEXT, received TEXT)")
        for result in response["results"]:
            existing = conn.execute("SELECT result FROM results WHERE id=?", (result["id"],)).fetchone()
            if existing and existing[0] != result["result"]:
                raise ValueError("Conflicting result")
            conn.execute("INSERT OR IGNORE INTO results VALUES(?,?,?,?)",
                         (result["id"], result["word"], result["result"], stamp()))
    # The durable local commit precedes the mailbox acknowledgement.
    request(url.rstrip("/") + "/manager/ack", token, {"ids": [r["id"] for r in response["results"]]})
    return len(response["results"])


def main():
    parser = argparse.ArgumentParser(description="BARBIE test manager " + VERSION)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    sub = parser.add_subparsers(dest="action", required=True)
    assign = sub.add_parser("assign")
    assign.add_argument("--lane", choices=("live", "reserve"), default="live")
    assign.add_argument("words", nargs="+")
    sub.add_parser("status")
    ctl = sub.add_parser("settings")
    ctl.add_argument("file")
    get = sub.add_parser("collect")
    get.add_argument("--db", default="manager.sqlite3")
    args = parser.parse_args()
    token = os.environ["BARBIE_MANAGER_TOKEN"]
    if args.action == "collect":
        value = {"collected": collect(args.url, token, args.db)}
    else:
        body = {}
        if args.action == "assign":
            body = {"words": args.words, "lane": args.lane}
        elif args.action == "settings":
            with open(args.file, encoding="utf-8-sig") as handle:
                body = json.load(handle)
        _, value = request(args.url.rstrip("/") + "/manager/" + args.action, token, body)
    print("BARBIE test manager", VERSION)
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
