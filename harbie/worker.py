"""Harbie Worker: Standalone 24/7 Merriam-Webster Worker for PythonAnywhere.

Features:
- Dual Mode: Connected (Harvey live) vs. Offline (Laptop shut / travel).
- Autonomous Queue Execution: Consumes from harbie_queue.
- Mellow Pacing: ~80s human cadence with organic jitter.
- Hoover Tripwire: Switches from CANDIDATE lane to HOOVER lane at 950 daily hits to
  protect the 1,000-hit ceiling.
- Zero Collision with Barbie: Respects mutual atomic reservations in mw_lookup_reservation.
- Direct Mirror Persistence: Writes snapshots and attestations directly to PA MySQL.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import logging
import random
import time
from typing import Any, Callable, Dict, Optional
import urllib.request
import urllib.parse

from .common import (
    DEFAULT_JITTER_PCT,
    DEFAULT_PACING_SEC,
    HARD_DAILY_LIMIT,
    HOOVER_THRESHOLD,
    BURST_DURATION_SEC,
    BURST_MAX_LOOKUPS,
    BURST_MIN_LOOKUPS,
    DEFAULT_JITTER_PCT,
    DEFAULT_PACING_SEC,
    HARD_DAILY_LIMIT,
    HOOVER_THRESHOLD,
    LANE_LIVE,
    LANE_CANDIDATE,
    LANE_HOOVER,
    MODE_CONNECTED,
    MODE_OFFLINE,
    REST_MEAN_SEC,
    REST_MIN_SEC,
    REST_STD_DEV_SEC,
    STATUS_DONE,
    STATUS_FAILED,
    compute_burst_rest,
    compute_delay,
    compute_intra_burst_delay,
    utc_now,
)
from .queue_store import QueueStore

LOG = logging.getLogger("harbie.worker")
MW_COLLEGIATE_ENDPOINT = "https://www.dictionaryapi.com/api/v3/references/collegiate/json"


class HarbieWorker:
    def __init__(
        self,
        queue_store: QueueStore,
        mw_key: str = "",
        *,
        mirror_saver: Optional[Callable[[str, Any, str], None]] = None,
        reservation_ledger: Optional[Any] = None,
        replay_mode: bool = False,
        # =========================================================================
        # PACING & MICRO-BURST PARAMETERS (TUNE HERE)
        # =========================================================================
        # In offline mode, Harbie fires micro-bursts of 3-6 lookups over ~9 seconds,
        # followed by a randomized rest pause sampled from a Gaussian distribution:
        # mean = 80 seconds, standard deviation = 10 seconds.
        burst_min: int = BURST_MIN_LOOKUPS,             # Min lookups per burst (default: 3)
        burst_max: int = BURST_MAX_LOOKUPS,             # Max lookups per burst (default: 6)
        burst_duration_sec: float = BURST_DURATION_SEC, # Target total duration of active burst (default: 9.0s)
        rest_mean_sec: float = REST_MEAN_SEC,           # Gaussian mean rest between bursts (default: 80.0s)
        rest_std_dev_sec: float = REST_STD_DEV_SEC,     # Gaussian std dev of rest (default: 10.0s)
        rest_min_sec: float = REST_MIN_SEC,             # Absolute minimum floor for rest (default: 30.0s)
        pacing_sec: int = DEFAULT_PACING_SEC,           # Legacy single-call pacing fallback
        jitter_pct: int = DEFAULT_JITTER_PCT,
    ):
        self.store = queue_store
        self.mw_key = mw_key
        self.mirror_saver = mirror_saver
        self.reservation_ledger = reservation_ledger
        self.replay_mode = replay_mode

        # Micro-burst timing configuration
        self.burst_min = burst_min
        self.burst_max = burst_max
        self.burst_duration_sec = burst_duration_sec
        self.rest_mean_sec = rest_mean_sec
        self.rest_std_dev_sec = rest_std_dev_sec
        self.rest_min_sec = rest_min_sec
        self.pacing_sec = pacing_sec
        self.jitter_pct = jitter_pct

        self.current_day: str = date.today().isoformat()
        self.today_calls: int = 0
        self.today_hits: int = 0
        self.today_misses: int = 0
        self.last_mode: str = MODE_OFFLINE
        self.hoover_tripped: bool = False
        self.running: bool = True

    def reset_daily_counters_if_new_day(self) -> None:
        """Reset quota counters when crossing midnight UTC."""
        today = date.today().isoformat()
        if today != self.current_day:
            LOG.info("Day boundary crossed: resetting Harbie daily counters from %s to %s", self.current_day, today)
            self.current_day = today
            self.today_calls = 0
            self.today_hits = 0
            self.today_misses = 0
            self.hoover_tripped = False
            try:
                self.store.log_event("DAY_RESET", self.last_mode, f"Day boundary reset: new day {today}")
            except Exception as exc:
                LOG.warning("Could not log day reset event: %s", exc)

    def query_mw(self, word: str) -> Tuple[str, str, Optional[Any], Optional[str]]:
        """Query Merriam-Webster Collegiate API.

        Returns (result_kind, outcome, payload, error).
        result_kind: 'HIT', 'MISS', or 'ERROR'
        outcome: 'response', 'not_held', or 'error'
        """
        if self.replay_mode:
            # Replay mode for testing: words containing 'hit' return hits, words with 'miss' return misses
            w_lower = word.lower()
            if "miss" in w_lower:
                return "MISS", "not_held", ["suggestion1", "suggestion2"], None
            if "error" in w_lower:
                return "ERROR", "error", None, "simulated error"
            return "HIT", "response", [{"meta": {"id": word}, "hwi": {"hw": word}, "shortdef": ["test def"]}], None

        if not self.mw_key:
            return "ERROR", "error", None, "MW_API_KEY is not configured"

        encoded = urllib.parse.quote(word.strip())
        url = f"{MW_COLLEGIATE_ENDPOINT}/{encoded}?key={self.mw_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "BaseFinder-Harbie/0.1.0"})

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                data = json.loads(raw.decode("utf-8"))

                if not isinstance(data, list):
                    return "ERROR", "error", None, "Unexpected MW response format"

                if not data:
                    # Empty list: not held
                    return "MISS", "not_held", data, None

                first = data[0]
                if isinstance(first, str):
                    # List of spelling suggestions -> not in dictionary (MISS)
                    return "MISS", "not_held", data, None

                if isinstance(first, dict) and "meta" in first:
                    # Found headword -> valid entry (HIT)
                    return "HIT", "response", data, None

                return "MISS", "not_held", data, None

        except Exception as exc:
            LOG.warning("MW lookup failed for '%s': %s", word, exc)
            return "ERROR", "error", None, str(exc)

    def step(self) -> Optional[Dict[str, Any]]:
        """Perform a single worker cycle.

        Returns report dict of the completed cycle, or None if idle.
        """
        self.reset_daily_counters_if_new_day()

        # 1. Determine Mode (Connected vs Offline)
        mode = self.store.determine_mode()
        if mode != self.last_mode:
            try:
                self.store.log_event("MODE_CHANGE", mode, f"Harbie transitioned from {self.last_mode} to {mode}")
            except Exception as exc:
                LOG.warning("Could not log mode change: %s", exc)
            self.last_mode = mode

        # 2. Check Control flags
        ctrl = self.store.get_control()
        if ctrl.get("paused"):
            LOG.debug("Harbie is paused via control settings.")
            self._heartbeat("PAUSED", None)
            return None

        # 3. Check Daily Limits
        daily_limit = ctrl.get("daily_limit", HARD_DAILY_LIMIT)
        if self.today_calls >= daily_limit or self.today_hits >= daily_limit:
            LOG.info("Daily limit reached (%d calls, %d hits). Standing by.", self.today_calls, self.today_hits)
            try:
                self.store.log_event("CEILING_REACHED", mode, f"Daily limit reached ({self.today_calls} calls, {self.today_hits} hits)")
            except Exception as exc:
                LOG.warning("Could not log ceiling event: %s", exc)
            self._heartbeat("CEILING_REACHED", None)
            return None

        # 4. Dynamic Slope & Hoover Tripwire Evaluation
        hoover_threshold = ctrl.get("hoover_threshold", HOOVER_THRESHOLD)
        enable_dynamic_slope = ctrl.get("dynamic_slope", False)

        enforce_hoover = self.today_hits >= hoover_threshold
        target_hits = hoover_threshold
        if not enforce_hoover and enable_dynamic_slope:
            now_utc = datetime.now(timezone.utc)
            seconds_elapsed = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second
            day_fraction = max(0.01, min(1.0, seconds_elapsed / 86400.0))
            target_hits = int(hoover_threshold * day_fraction)
            if self.today_hits > target_hits + 20 and self.today_hits > 40:
                enforce_hoover = True

        if enforce_hoover and not self.hoover_tripped:
            self.hoover_tripped = True
            LOG.info(
                "Hit governor active (hits=%d, target=%d, hard_limit=%d). Throttling candidate lane -> HOOVER.",
                self.today_hits, target_hits, hoover_threshold
            )
            try:
                self.store.log_event("HOOVER_TRIP", mode, f"Hit governor active (hits={self.today_hits}, target={target_hits}). Throttling to HOOVER.")
            except Exception as exc:
                LOG.warning("Could not log hoover trip event: %s", exc)
        elif not enforce_hoover and self.hoover_tripped and self.today_hits < hoover_threshold:
            self.hoover_tripped = False
            LOG.info(
                "Hit governor relaxed (hits=%d <= target=%d). Resuming CANDIDATE discovery.",
                self.today_hits, target_hits
            )

        # 5. 3-Tier Lane Selection:
        # Tier 1: LIVE (always claimed first; real-time dispatches from Harvey)
        job = self.store.claim_next_word(LANE_LIVE)

        # Tier 2 & 3: Fallback lanes (Candidate & Hoover)
        if job is None:
            # If in CONNECTED mode, check if fallback is permitted while laptop is active
            allow_fallback_when_connected = ctrl.get("allow_fallback_when_connected", True)
            if mode == MODE_CONNECTED and not allow_fallback_when_connected:
                LOG.debug("Connected mode: live lane empty. Standing by for Harvey dispatches.")
                self._heartbeat("WAITING_FOR_LIVE", None)
                return None

            # Enforce hoover if tripped or above hit-rate slope
            if enforce_hoover:
                job = self.store.claim_next_word(LANE_HOOVER)
            else:
                job = self.store.claim_next_word(LANE_CANDIDATE)
                if job is None:
                    job = self.store.claim_next_word(LANE_HOOVER)

        if job is None:
            LOG.debug("No pending jobs found in queue.")
            self._heartbeat("IDLE_EMPTY_QUEUE", None)
            return None

        word = job["word"]
        job_id = job["id"]
        lane = job["lane"]

        # 6. Mutual Reservation with Barbie
        if self.reservation_ledger is not None:
            # Verify no active reservation by Barbie
            try:
                reserved = self.reservation_ledger.reserve(word, worker="HARBIE")
                if not reserved:
                    LOG.info("Word '%s' is locked by Barbie. Deferring.", word)
                    self.store.complete_word(job_id, "COLLISION", "collision_deferred", None, "Reserved by Barbie")
                    return None
            except Exception as exc:
                LOG.warning("Reservation check error: %s", exc)

        # 7. Execute Lookup
        result_kind, outcome, payload, error = self.query_mw(word)

        # 8. Record Daily Counts
        if outcome != "error":
            self.today_calls += 1
            if result_kind == "HIT":
                self.today_hits += 1
            elif result_kind == "MISS":
                self.today_misses += 1

        # Direct, immediate logging to stdout for PA Always-On Task log and bash console
        ts_str = datetime.now().strftime("%H:%M:%S")
        LOG.info(
            "[%s] [%s] Lookup #%d: '%s' [%s] -> %s (Today: %d calls, %d hits, %d misses)",
            ts_str, mode, self.today_calls, word, lane, result_kind,
            self.today_calls, self.today_hits, self.today_misses
        )

        # 9. Save to Mirror if configured
        if self.mirror_saver and outcome != "error":
            try:
                self.mirror_saver(word, payload, result_kind)
            except Exception as exc:
                LOG.error("Failed to persist snapshot for '%s': %s", word, exc)

        # 10. Complete Word in Queue
        payload_str = json.dumps(payload) if payload is not None else None
        self.store.complete_word(job_id, result_kind, outcome, payload_str, error)

        # Record structured lookup event
        try:
            self.store.log_event(
                "LOOKUP",
                mode,
                f"{word} [{lane}] -> {result_kind}",
                {
                    "word": word,
                    "lane": lane,
                    "result_kind": result_kind,
                    "outcome": outcome,
                    "today_calls": self.today_calls,
                    "today_hits": self.today_hits,
                },
            )
        except Exception as exc:
            LOG.warning("Could not log lookup event: %s", exc)

        # 11. Release Reservation
        if self.reservation_ledger is not None:
            try:
                self.reservation_ledger.release(word, worker="HARBIE")
            except Exception as exc:
                LOG.warning("Failed to release reservation for '%s': %s", word, exc)

        # 12. Record Heartbeat
        report = self._heartbeat("ACTIVE", word, result_kind=result_kind, lane=lane)
        return report

    def _heartbeat(self, state: str, word: Optional[str], **extra) -> Dict[str, Any]:
        report = {
            "worker": "HARBIE",
            "state": state,
            "mode": self.last_mode,
            "word": word,
            "today_calls": self.today_calls,
            "today_hits": self.today_hits,
            "today_misses": self.today_misses,
            "active_day": self.current_day,
            "at": utc_now(),
            **extra,
        }
        self.store.record_heartbeat(report)
        return report

    def step_burst(self) -> List[Dict[str, Any]]:
        """Execute a single micro-burst of lookups.

        Picks a random burst count between burst_min and burst_max (e.g. 3-6 lookups),
        spaces them across ~burst_duration_sec (~9 seconds), and returns the list of
        completed reports.
        """
        burst_size = random.randint(self.burst_min, self.burst_max)
        intra_delay = compute_intra_burst_delay(burst_size, self.burst_duration_sec)
        mode = self.store.determine_mode()

        try:
            self.store.log_event("BURST_START", mode, f"Starting micro-burst of {burst_size} lookups")
        except Exception:
            pass

        LOG.debug("Starting micro-burst of %d lookups (intra-delay: %.2fs)...", burst_size, intra_delay)
        reports = []
        for i in range(burst_size):
            rep = self.step()
            if rep is None:
                # Queue was empty, ceiling reached, or worker paused
                break
            reports.append(rep)

            # Space lookups within the burst (skip delay after last lookup)
            if i < burst_size - 1 and self.burst_duration_sec > 0:
                time.sleep(intra_delay)

        if reports:
            try:
                self.store.log_event(
                    "BURST_END",
                    mode,
                    f"Micro-burst finished with {len(reports)} lookups",
                    {"burst_size": len(reports), "words": [r.get("word") for r in reports if r]},
                )
            except Exception:
                pass

        return reports

    def run_loop(self, max_bursts: Optional[int] = None) -> None:
        """Run continuous worker loop with micro-bursts and randomized Gaussian rest pauses."""
        LOG.info(
            "Starting Harbie Worker daemon (micro-bursts: %d-%d lookups over ~%.1fs, rest: mean=%.0fs, std=%.0fs)...",
            self.burst_min,
            self.burst_max,
            self.burst_duration_sec,
            self.rest_mean_sec,
            self.rest_std_dev_sec,
        )
        burst_count = 0
        while self.running:
            reports = self.step_burst()
            burst_count += 1
            if max_bursts and burst_count >= max_bursts:
                break

            if not reports:
                # Idle state: queue empty, paused, or daily ceiling reached
                idle_sleep = min(15.0, self.rest_mean_sec) if self.rest_mean_sec > 0 else 0
                if idle_sleep > 0:
                    time.sleep(idle_sleep)
                continue

            # Distinguish between LIVE interactive jobs and background reserve bursts
            is_live_burst = any(r.get("lane") == LANE_LIVE for r in reports if r)
            if is_live_burst:
                rest = random.uniform(4.0, 8.0)
                LOG.info("Live job completed. Short gap %.1fs before next dispatch...", rest)
            else:
                # Active fallback burst completed: take randomized Gaussian rest pause
                rest = compute_burst_rest(self.rest_mean_sec, self.rest_std_dev_sec, self.rest_min_sec)
                mode = self.store.determine_mode()
                try:
                    self.store.log_event("REST", mode, f"Micro-burst resting for {rest:.1f}s", {"rest_sec": round(rest, 1)})
                except Exception:
                    pass
                LOG.info(
                    "Micro-burst finished: %d lookups. Resting for %.1fs (Gaussian mean=%.0fs, std=%.0fs)...",
                    len(reports),
                    rest,
                    self.rest_mean_sec,
                    self.rest_std_dev_sec,
                )
            if rest > 0:
                time.sleep(rest)
