"""Harbie System Test Suite: Comprehensive local tests for Harbie's dual-mode execution,

queue mechanics, mellow pacing, hoover tripwire, and Harvey sync.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harbie.common import (
    HARD_DAILY_LIMIT,
    HOOVER_THRESHOLD,
    LANE_CANDIDATE,
    LANE_HOOVER,
    MODE_CONNECTED,
    MODE_OFFLINE,
    STATUS_DONE,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    utc_now,
)
from harbie.harvey_pusher import HarveyPusher
from harbie.harvey_sync import HarveySync
from harbie.queue_store import QueueStore
from harbie.worker import HarbieWorker


class MockReservationLedger:
    def __init__(self, blocked_words=None):
        self.blocked_words = set(blocked_words or [])
        self.active_reservations = {}

    def reserve(self, word: str, worker: str = "HARBIE") -> bool:
        if word in self.blocked_words:
            return False
        self.active_reservations[word] = worker
        return True

    def release(self, word: str, worker: str = "HARBIE") -> None:
        self.active_reservations.pop(word, None)


class HarbieSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "harbie_test.sqlite3"
        self.local_db_path = self.root / "ayb_test.sqlite3"

        # Initialize Remote (PA simulated) store
        self.store = QueueStore(lambda: sqlite3.connect(self.db_path), is_mysql=False)

        # Initialize Local simulated all_your_base
        conn = sqlite3.connect(self.local_db_path)
        try:
            conn.execute(
                """CREATE TABLE word (
                    word TEXT PRIMARY KEY,
                    mw_status INTEGER DEFAULT 0
                )"""
            )
            # Insert test vocabulary
            words = [
                ("apple", 0),
                ("banana", 0),
                ("cherry", 0),
                ("supercalifragilistic", 0),
                ("pseudopseudohypoparathyroidism", 0),
                ("antidisestablishmentarianism", 0),
            ]
            conn.executemany("INSERT INTO word (word, mw_status) VALUES (?, ?)", words)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_01_queue_initialization_and_default_control(self):
        """Verify schema creation and default control settings."""
        ctrl = self.store.get_control()
        self.assertEqual(ctrl["daily_limit"], HARD_DAILY_LIMIT)
        self.assertEqual(ctrl["hoover_threshold"], HOOVER_THRESHOLD)
        self.assertEqual(ctrl["mode"], MODE_OFFLINE)
        self.assertIsNone(ctrl["harvey_last_seen"])

    def test_02_harvey_batch_push_and_deduplication(self):
        """Verify Harvey can push candidate batches with lane and priority separation."""
        pusher = HarveyPusher(self.store)
        candidates = [
            {"word": "behead", "lane": LANE_CANDIDATE, "priority": 10},
            {"word": "curtail", "lane": LANE_CANDIDATE, "priority": 20},
        ]
        hoover = [
            {"word": "supercalifragilistic", "lane": LANE_HOOVER, "priority": 1001},
        ]

        result = pusher.push_work_package(candidates, hoover)
        self.assertEqual(result["total_submitted"], 3)
        self.assertEqual(result["newly_inserted"], 3)

        # Attempt to push duplicates: must be ignored safely
        dup_result = pusher.push_work_package(candidates, [])
        self.assertEqual(dup_result["newly_inserted"], 0)

        stats = self.store.get_stats()
        self.assertEqual(stats[LANE_CANDIDATE][STATUS_PENDING], 2)
        self.assertEqual(stats[LANE_HOOVER][STATUS_PENDING], 1)

    def test_03_connected_vs_offline_mode_switching(self):
        """Verify Harbie senses Harvey's presence and automatically switches when laptop goes dark."""
        pusher = HarveyPusher(self.store)
        candidates = [{"word": "testword", "lane": LANE_CANDIDATE, "priority": 1}]
        pusher.push_work_package(candidates, [])

        # Pusher updates heartbeat -> should be CONNECTED
        self.assertEqual(self.store.determine_mode(timeout_sec=300), MODE_CONNECTED)

        # Simulate laptop going dark (Harvey last seen 10 minutes ago)
        ten_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        ctrl = self.store.get_control()
        ctrl["harvey_last_seen"] = ten_minutes_ago
        self.store.set_control(ctrl)

        # Harbie should now sense OFFLINE mode
        self.assertEqual(self.store.determine_mode(timeout_sec=300), MODE_OFFLINE)

    def test_04_autonomous_worker_execution(self):
        """Verify Harbie autonomously consumes words from the queue in replay mode."""
        pusher = HarveyPusher(self.store)
        candidates = [
            {"word": "testhit", "lane": LANE_CANDIDATE, "priority": 1},
            {"word": "testmiss", "lane": LANE_CANDIDATE, "priority": 2},
        ]
        pusher.push_work_package(candidates, [])

        # Create worker in replay mode
        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)
        
        # Step 1: processes 'testhit'
        rep1 = worker.step()
        self.assertIsNotNone(rep1)
        self.assertEqual(rep1["word"], "testhit")
        self.assertEqual(rep1["result_kind"], "HIT")
        self.assertEqual(worker.today_hits, 1)
        self.assertEqual(worker.today_calls, 1)

        # Step 2: processes 'testmiss'
        rep2 = worker.step()
        self.assertIsNotNone(rep2)
        self.assertEqual(rep2["word"], "testmiss")
        self.assertEqual(rep2["result_kind"], "MISS")
        self.assertEqual(worker.today_hits, 1)
        self.assertEqual(worker.today_misses, 1)
        self.assertEqual(worker.today_calls, 2)

        # Step 3: queue empty -> idle
        rep3 = worker.step()
        self.assertIsNone(rep3)

        # Check queue table state
        stats = self.store.get_stats()
        self.assertEqual(stats[LANE_CANDIDATE][STATUS_DONE], 2)

    def test_05_hoover_tripwire_at_950_hits(self):
        """Verify that reaching 950 hits forces Harbie to switch from CANDIDATE to HOOVER lane."""
        pusher = HarveyPusher(self.store)
        candidates = [
            {"word": "cand1", "lane": LANE_CANDIDATE, "priority": 1},
            {"word": "cand2", "lane": LANE_CANDIDATE, "priority": 2},
        ]
        hoover = [
            {"word": "hoover_word_1", "lane": LANE_HOOVER, "priority": 10},
        ]
        pusher.push_work_package(candidates, hoover)

        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)

        # Set hits to 949 (under threshold): next item must be candidate
        worker.today_hits = 949
        rep1 = worker.step()
        self.assertEqual(rep1["word"], "cand1")
        self.assertEqual(rep1["lane"], LANE_CANDIDATE)

        # Worker now has 950 hits! Tripwire triggered -> next item must come from HOOVER lane
        self.assertEqual(worker.today_hits, 950)
        rep2 = worker.step()
        self.assertEqual(rep2["word"], "hoover_word_1")
        self.assertEqual(rep2["lane"], LANE_HOOVER)

    def test_06_hard_daily_ceiling_protection(self):
        """Verify worker halts when daily calls or hits hit 1,000."""
        pusher = HarveyPusher(self.store)
        pusher.push_work_package([{"word": "cand1", "lane": LANE_CANDIDATE, "priority": 1}], [])

        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)
        worker.today_hits = 1000  # Ceiling reached

        rep = worker.step()
        self.assertIsNone(rep)  # Must refuse to look up

        # Word should remain PENDING
        stats = self.store.get_stats()
        self.assertEqual(stats[LANE_CANDIDATE][STATUS_PENDING], 1)

    def test_07_mutual_reservation_coordination_with_barbie(self):
        """Verify Harbie defers words currently locked by Barbie without collision."""
        pusher = HarveyPusher(self.store)
        pusher.push_work_package([{"word": "shared_word", "lane": LANE_CANDIDATE, "priority": 1}], [])

        # Barbie has locked 'shared_word'
        ledger = MockReservationLedger(blocked_words=["shared_word"])
        worker = HarbieWorker(self.store, reservation_ledger=ledger, replay_mode=True, pacing_sec=0)

        rep = worker.step()
        self.assertIsNone(rep)  # Deferred

        # Check queue status: marked COLLISION/FAILED, zero calls made
        self.assertEqual(worker.today_calls, 0)

    def test_08_harvey_sync_reconciliation(self):
        """Verify completed lookups are synced back into local all_your_base word table."""
        # 1. Push words to remote queue
        pusher = HarveyPusher(self.store)
        pusher.push_work_package(
            [
                {"word": "apple", "lane": LANE_CANDIDATE, "priority": 1},
                {"word": "banana", "lane": LANE_CANDIDATE, "priority": 2},
            ],
            [],
        )

        # 2. Harbie executes them (apple = HIT, banana = HIT in mock)
        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)
        worker.step()
        worker.step()

        # 3. Harvey syncs completed results back to local database
        sync = HarveySync(self.store, lambda: sqlite3.connect(self.local_db_path), is_mysql=False)
        res = sync.sync_completed()

        self.assertEqual(res["completed_remote_rows"], 2)
        self.assertEqual(res["hits"], 2)

        # Verify local table was updated
        conn = sqlite3.connect(self.local_db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT mw_status FROM word WHERE word = 'apple'")
            self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_09_mellow_pacing_delay_and_jitter(self):
        """Verify compute_delay adheres to human-cadence bounds with organic jitter."""
        from harbie.common import compute_delay
        delays = [compute_delay(80, 15) for _ in range(100)]
        for d in delays:
            self.assertGreaterEqual(d, 68.0)  # 80 * (1 - 0.15)
            self.assertLessEqual(d, 92.0)     # 80 * (1 + 0.15)
        # Ensure values are not all identical (genuine random distribution)
        self.assertGreater(len(set(delays)), 50)

    def test_10_pause_control_flag(self):
        """Verify Harbie stands down immediately when paused via control table."""
        pusher = HarveyPusher(self.store)
        pusher.push_work_package([{"word": "paused_cand", "lane": LANE_CANDIDATE, "priority": 1}], [])

        ctrl = self.store.get_control()
        ctrl["paused"] = True
        self.store.set_control(ctrl)

        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)
        rep = worker.step()
        self.assertIsNone(rep)
        self.assertEqual(worker.today_calls, 0)

        # Unpause and verify execution resumes
        ctrl["paused"] = False
        self.store.set_control(ctrl)
        rep2 = worker.step()
        self.assertIsNotNone(rep2)
        self.assertEqual(rep2["word"], "paused_cand")

    def test_11_mirror_persistence_callback(self):
        """Verify Harbie passes raw snapshot and attribution directly to mirror saver."""
        pusher = HarveyPusher(self.store)
        pusher.push_work_package([{"word": "persisted_word", "lane": LANE_CANDIDATE, "priority": 1}], [])

        saved_snapshots = []
        def mock_saver(word, payload, result_kind):
            saved_snapshots.append({"word": word, "payload": payload, "kind": result_kind})

        worker = HarbieWorker(self.store, mirror_saver=mock_saver, replay_mode=True, pacing_sec=0)
        worker.step()

        self.assertEqual(len(saved_snapshots), 1)
        self.assertEqual(saved_snapshots[0]["word"], "persisted_word")
        self.assertEqual(saved_snapshots[0]["kind"], "HIT")

    def test_12_day_boundary_reset(self):
        """Verify that when date changes, Harbie resets daily hit/call counters."""
        worker = HarbieWorker(self.store, replay_mode=True, pacing_sec=0)
        worker.today_calls = 500
        worker.today_hits = 450
        worker.current_day = "2026-09-20"  # Yesterday

        # Step checks reset
        worker.reset_daily_counters_if_new_day()
        self.assertEqual(worker.today_calls, 0)
        self.assertEqual(worker.today_hits, 0)

    def test_13_micro_burst_sizing_and_execution(self):
        """Verify Harbie executes micro-bursts of 3-6 lookups."""
        pusher = HarveyPusher(self.store)
        words = [{"word": f"burst_word_{i}", "lane": LANE_CANDIDATE, "priority": i} for i in range(20)]
        pusher.push_work_package(words, [])

        worker = HarbieWorker(
            self.store,
            replay_mode=True,
            burst_min=3,
            burst_max=6,
            burst_duration_sec=0, # no sleep during fast test
            rest_mean_sec=0,
        )

        reports = worker.step_burst()
        self.assertGreaterEqual(len(reports), 3)
        self.assertLessEqual(len(reports), 6)
        self.assertEqual(worker.today_calls, len(reports))

    def test_14_intra_burst_delay_math(self):
        """Verify intra-burst delay calculation spans target duration (~9 seconds)."""
        from harbie.common import compute_intra_burst_delay

        # 3 lookups -> 2 intervals over 9s -> ~4.5s each
        delay_3 = compute_intra_burst_delay(3, target_duration=9.0)
        self.assertGreaterEqual(delay_3, 3.8) # 4.5 * 0.85
        self.assertLessEqual(delay_3, 5.2)    # 4.5 * 1.15

        # 6 lookups -> 5 intervals over 9s -> ~1.8s each
        delay_6 = compute_intra_burst_delay(6, target_duration=9.0)
        self.assertGreaterEqual(delay_6, 1.5) # 1.8 * 0.85
        self.assertLessEqual(delay_6, 2.1)    # 1.8 * 1.15

    def test_15_gaussian_rest_distribution(self):
        """Verify randomized rest follows Gaussian distribution (mean=80s, std=10s)."""
        from harbie.common import compute_burst_rest
        import statistics

        samples = [compute_burst_rest(mean_sec=80.0, std_dev_sec=10.0, min_sec=30.0) for _ in range(500)]
        mean = statistics.mean(samples)
        stdev = statistics.stdev(samples)

        # Statistical verification within 2 sigma tolerance
        self.assertAlmostEqual(mean, 80.0, delta=2.5)
        self.assertAlmostEqual(stdev, 10.0, delta=2.5)
        # Verify safety floor
        for s in samples:
            self.assertGreaterEqual(s, 30.0)

    def test_16_forced_offline_test_mode(self):
        """Verify forced_mode override allows forcing OFFLINE mode while laptop is active."""
        pusher = HarveyPusher(self.store)
        pusher.push_work_package([{"word": "testword", "lane": LANE_CANDIDATE, "priority": 1}], [])

        # Active laptop heartbeat normally forces CONNECTED mode
        self.assertEqual(self.store.determine_mode(), MODE_CONNECTED)

        # Trigger forced offline test mode
        self.store.set_forced_mode(MODE_OFFLINE)
        self.assertEqual(self.store.determine_mode(), MODE_OFFLINE)

        # Restore auto mode -> snaps back to CONNECTED because heartbeat is fresh
        self.store.set_forced_mode(None)
        self.assertEqual(self.store.determine_mode(), MODE_CONNECTED)

    def test_17_audit_event_logging_and_summary(self):
        """Verify Harbie records structured audit logs and provides overnight summaries."""
        pusher = HarveyPusher(self.store)
        words = [{"word": f"log_word_{i}", "lane": LANE_CANDIDATE, "priority": i} for i in range(5)]
        pusher.push_work_package(words, [])

        worker = HarbieWorker(
            self.store,
            replay_mode=True,
            burst_min=3,
            burst_max=3,
            burst_duration_sec=0,
            rest_mean_sec=0,
        )

        reports = worker.step_burst()
        self.assertEqual(len(reports), 3)

        # Inspect audit logs
        logs = self.store.get_audit_logs(limit=20)
        self.assertGreaterEqual(len(logs), 5)  # BURST_START + 3 LOOKUPs + BURST_END

        event_types = [l["event_type"] for l in logs]
        self.assertIn("BURST_START", event_types)
        self.assertIn("LOOKUP", event_types)
        self.assertIn("BURST_END", event_types)

        # Inspect overnight summary
        summary = self.store.get_overnight_summary()
        self.assertEqual(summary["total_bursts"], 1)
        self.assertEqual(summary["total_lookups"], 3)
        self.assertIsNotNone(summary["first_event"])
        self.assertIsNotNone(summary["last_event"])


if __name__ == "__main__":
    unittest.main()
