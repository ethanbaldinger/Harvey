"""Behavioral tests: actual loopback HTTP, durable state, replay safety."""
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .common import DEFAULT_SETTINGS, db, request
from .mailbox import Mailbox
from .manager import collect
from .worker import Worker, singleton

WORKER_TOKEN = "test-worker-token-0000000000000000"
MANAGER_TOKEN = "test-manager-token-000000000000000"
MIRROR_TOKEN = "test-mirror-token-0000000000000000"


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class Server:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mailbox = Mailbox(self.root / "mail.sqlite3", WORKER_TOKEN, MANAGER_TOKEN)
        self.server = Server(self.mailbox)
        self.calls = []

        def mirror(environ, start_response):
            self.calls.append((environ["PATH_INFO"], environ["QUERY_STRING"]))
            self.assertEqual(environ["QUERY_STRING"], "held=1")
            self.assertEqual(environ["HTTP_AUTHORIZATION"], "Bearer " + MIRROR_TOKEN)
            word = environ["PATH_INFO"].rsplit("/", 1)[-1]
            if word == "not-held":
                start_response("204 No Content", [])
                return [b""]
            payload = ["suggestion"] if word == "miss" else [{"meta": {"id": word}, "shortdef": ["example"]}]
            raw = json.dumps(payload).encode()
            start_response("200 OK", [("Content-Type", "application/json")])
            return [raw]

        self.mirror = Server(mirror)
        self.worker = self.new_worker()

    def tearDown(self):
        self.server.close()
        self.mirror.close()
        self.tmp.cleanup()

    def new_worker(self):
        return Worker(self.root / "worker.sqlite3", self.server.url, WORKER_TOKEN, self.mirror.url, MIRROR_TOKEN)

    def assign(self, words, lane="live"):
        return request(self.server.url + "/manager/assign", MANAGER_TOKEN, {"words": words, "lane": lane})

    def ready(self):
        with db(self.worker.path) as conn:
            conn.execute("DELETE FROM meta WHERE key='next_at'")

    def test_replay_lock_refuses_live_settings_even_with_key(self):
        self.worker.mw_key = 'must-never-be-used'
        request(self.server.url + '/manager/settings', MANAGER_TOKEN,
                DEFAULT_SETTINGS | {'provider': 'mw_live'})
        self.assign(['cat'])
        self.assertTrue(self.worker.sync())
        with patch('barbie.worker.request', side_effect=AssertionError('No dispatch allowed')):
            self.assertFalse(self.worker.lookup_one())
        self.assertEqual(self.worker.report()['blocked'], 'replay_only')

    def test_end_to_end_hit_miss_and_unknown_are_distinct(self):
        self.assign(["hit", "miss", "not-held"], "reserve")
        self.assertTrue(self.worker.sync())
        for _ in range(3):
            self.ready()
            self.assertTrue(self.worker.lookup_one())
            self.assertTrue(self.worker.sync())
        self.assertEqual(collect(self.server.url, MANAGER_TOKEN, self.root / "manager.sqlite3"), 3)
        self.assertEqual(collect(self.server.url, MANAGER_TOKEN, self.root / "manager.sqlite3"), 0)
        with db(self.root / "manager.sqlite3") as conn:
            result = {row["word"]: json.loads(row["result"]) for row in conn.execute("SELECT * FROM results")}
        self.assertIsInstance(result["hit"]["payload"][0], dict)
        self.assertEqual(result["miss"]["payload"], ["suggestion"])
        self.assertEqual(result["not-held"]["outcome"], "not_held")
        self.assertEqual(self.worker.report()["test_quota"]["billed"], 1)

    def test_offline_reserve_survives_restart_and_reconnect(self):
        self.assign(["a", "b"], "reserve")
        self.worker.sync()
        with patch("barbie.worker.request", side_effect=OSError("offline")):
            self.assertFalse(self.worker.sync())
        self.assertTrue(self.worker.lookup_one())
        restarted = self.new_worker()
        self.assertEqual(restarted.report()["awaiting_delivery"], 1)
        self.assertTrue(restarted.sync())
        self.assertEqual(restarted.report()["awaiting_delivery"], 0)
        self.assertEqual(restarted.report()["pending"], 1)

    def test_lost_receipt_retry_is_idempotent(self):
        self.assign(["hit"])
        self.worker.sync()
        self.worker.lookup_one()
        with db(self.worker.path) as conn:
            result = json.loads(conn.execute("SELECT result FROM jobs").fetchone()[0])
        self.mailbox.action("/worker/sync", {"results": [result]})
        self.assertTrue(self.worker.sync())
        with db(self.mailbox.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE result IS NOT NULL").fetchone()[0], 1)

    def test_quota_and_pacing_survive_restart(self):
        self.assign(["a", "b"], "reserve")
        self.mailbox.action("/manager/settings", {**DEFAULT_SETTINGS, "daily_limit": 1})
        self.worker.sync()
        self.assertTrue(self.worker.lookup_one())
        restarted = self.new_worker()
        self.assertFalse(restarted.lookup_one())
        self.ready()
        self.assertFalse(restarted.lookup_one())
        self.assertEqual(len(self.calls), 1)

    def test_crash_retains_quota_reservation_and_reports_uncertain(self):
        self.assign(["hit"])
        self.worker.sync()
        with patch("barbie.worker.request", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.lookup_one()
        restarted = self.new_worker()
        self.assertEqual(restarted.report()["test_quota"]["reserved"], 1)
        self.assertEqual(restarted.report()["awaiting_delivery"], 1)
        restarted.sync()
        with db(self.mailbox.path) as conn:
            result = json.loads(conn.execute("SELECT result FROM jobs").fetchone()[0])
        self.assertEqual(result["outcome"], "uncertain")

    def test_remote_pause_persists_offline(self):
        self.assign(["hit"], "reserve")
        self.mailbox.action("/manager/settings", {**DEFAULT_SETTINGS, "paused": True})
        self.worker.sync()
        self.assertFalse(self.new_worker().lookup_one())

    def test_initial_empty_handshake_does_not_consume_daily_reserve(self):
        self.assertTrue(self.worker.sync())
        self.assign(['stored-later'], 'reserve')
        self.assertTrue(self.worker.sync())
        self.assertTrue(self.worker.lookup_one())

    def test_quota_rollover_preserves_yesterdays_completed_work(self):
        self.assign(['first', 'second'], 'reserve')
        request(self.server.url + '/manager/settings', MANAGER_TOKEN, DEFAULT_SETTINGS | {'daily_limit': 1})
        self.worker.sync()
        self.assertTrue(self.worker.lookup_one())
        self.ready()
        self.assertFalse(self.worker.lookup_one())
        tomorrow = date.fromordinal(date.today().toordinal() + 1)
        with patch('barbie.worker.date') as clock:
            clock.today.return_value = tomorrow
            restarted = self.new_worker()
            self.assertTrue(restarted.lookup_one())
            self.assertTrue(restarted.sync())
            self.assertEqual(restarted.report()['test_quota']['billed'], 1)
        self.assertEqual(collect(self.server.url, MANAGER_TOKEN, self.root / 'manager.sqlite3'), 2)

    def test_reserve_refreshed_once_daily_and_old_jobs_preserved(self):
        self.assign(["old"], "reserve")
        self.worker.sync()
        self.assign(["new"], "reserve")
        self.worker.sync()
        self.assertEqual(self.worker.report()["pending"], 1)
        with db(self.worker.path) as conn:
            conn.execute("UPDATE meta SET value='yesterday' WHERE key='reserve_day'")
        self.worker.sync()
        self.assertEqual(self.worker.report()["pending"], 2)

    def test_worker_cannot_publish_settings_and_no_unsafe_settings(self):
        with self.assertRaises(HTTPError) as raised:
            request(self.server.url + "/manager/settings", WORKER_TOKEN, DEFAULT_SETTINGS)
        self.assertEqual(raised.exception.code, 401)
        for changes in ({"daily_limit": 1001}, {"gap_min": 0}, {"gap_max": float("nan")}, {"paused": "false"}):
            with self.assertRaises(ValueError):
                self.mailbox.action("/manager/settings", {**DEFAULT_SETTINGS, **changes})

    def test_conflicting_result_rejected(self):
        self.assign(["hit"])
        self.worker.sync()
        self.worker.lookup_one()
        with db(self.worker.path) as conn:
            result = json.loads(conn.execute("SELECT result FROM jobs").fetchone()[0])
        self.mailbox.action("/worker/sync", {"results": [result]})
        with self.assertRaises(ValueError):
            self.mailbox.action("/worker/sync", {"results": [{**result, "payload": []}]})

    def test_singleton_prevents_second_worker(self):
        with singleton(self.worker.path):
            with self.assertRaises(OSError):
                with singleton(self.worker.path):
                    self.fail("Second worker acquired the lock")

    def test_hardwired_quota_limit_blocks_at_1000_even_if_settings_tampered(self):
        self.assign(["hit"], "reserve")
        with db(self.worker.path) as conn:
            conn.execute("UPDATE meta SET value=? WHERE key='settings'",
                         (json.dumps({**DEFAULT_SETTINGS, "daily_limit": 5000}),))
            conn.execute("INSERT OR REPLACE INTO ledger(day,provider,reserved,billed,requests) VALUES(?,?,0,1000,1000)",
                         (date.today().isoformat(), "mirror_replay"))
        self.assertFalse(self.worker.lookup_one())


if __name__ == "__main__":
    unittest.main()
