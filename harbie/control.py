"""Harbie Control & Test Observer CLI.

Allows you to trigger Harbie into Offline Mode for testing while watching
its live execution and micro-bursts directly from your laptop.

Usage:
  python -m harbie.control --status
  python -m harbie.control --force-offline
  python -m harbie.control --auto
  python -m harbie.control --watch
  python -m harbie.control --pause
  python -m harbie.control --resume
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import mysql.connector

from .common import MODE_CONNECTED, MODE_OFFLINE
from .queue_store import QueueStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOG = logging.getLogger("harbie.control")


def get_store(args) -> QueueStore:
    db_name = "badangel$mw"
    def get_conn():
        return mysql.connector.connect(
            host=args.pa_host,
            port=args.pa_port,
            user=args.pa_user,
            password=args.pa_password,
            database=db_name,
            connection_timeout=10,
        )
    return QueueStore(get_conn, is_mysql=True)


def print_status(store: QueueStore):
    ctrl = store.get_control()
    mode = store.determine_mode()
    forced = ctrl.get("forced_mode")
    hb = store.get_heartbeat()
    stats = store.get_stats()

    print("\n" + "=" * 65)
    print(" HARBIE WORKER CONTROL & TELEMETRY STATUS")
    print("=" * 65)
    print(f" Effective Mode      : {mode} " + (f"[FORCED TEST OVERRIDE: {forced}]" if forced else "[AUTO]"))
    print(f" Forced Mode Setting : {forced or 'None (automatic heartbeat detection)'}")
    print(f" Paused              : {ctrl.get('paused', False)}")
    print(f" Harvey Last Seen    : {ctrl.get('harvey_last_seen') or 'Never'}")
    print(f" Pacing / Rest       : Gaussian mean={ctrl.get('pacing_seconds', 80)}s, std=10s (Burst: 3-6 over ~9s)")
    print(f" Daily Ceiling       : {ctrl.get('daily_limit', 1000)} | Hoover Tripwire: {ctrl.get('hoover_threshold', 950)}")
    print("-" * 65)
    if hb:
        print(f" Worker Live State   : {hb.get('state')} (reported at {hb.get('reported_at', 'unknown')})")
        print(f" Today's Progress    : {hb.get('today_calls', 0):,} calls | {hb.get('today_hits', 0):,} hits | {hb.get('today_misses', 0):,} misses")
        print(f" Last Word Processed : '{hb.get('word') or 'none'}' [{hb.get('lane', 'unknown')}] -> {hb.get('result_kind', 'unknown')}")
    else:
        print(" Worker Live State   : No heartbeat recorded yet (Worker not running or starting up)")
    lane_counts = store.get_lane_counts()
    print("-" * 65)
    print(" 3-TIER QUEUE BACKLOG:")
    print(f"   [Tier 1] LIVE (Real-Time Preempt)    : {lane_counts.get('LIVE', 0):,} pending")
    print(f"   [Tier 2] CANDIDATE (Active Reserve)  : {lane_counts.get('CANDIDATE', 0):,} pending")
    print(f"   [Tier 3] HOOVER (16+ Deep Cushion)   : {lane_counts.get('HOOVER', 0):,} pending")
    print("=" * 65 + "\n")


def watch_live(store: QueueStore, poll_interval: float = 2.5):
    print("\n" + "=" * 65)
    print(" LIVE HARBIE TELEMETRY STREAM (Press Ctrl+C to exit)")
    print("=" * 65)
    seen_completions = set()
    last_reported_at = None

    try:
        while True:
            ctrl = store.get_control()
            mode = store.determine_mode()
            forced = ctrl.get("forced_mode")
            hb = store.get_heartbeat()

            # Display heartbeat changes
            if hb and hb.get("reported_at") != last_reported_at:
                last_reported_at = hb.get("reported_at")
                mode_str = f"{mode}" + (" (FORCED TEST)" if forced else "")
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] [{mode_str:<17}] State: {hb.get('state'):<14} | Calls: {hb.get('today_calls', 0):<3} | Hits: {hb.get('today_hits', 0):<3} | Last: '{hb.get('word')}' ({hb.get('result_kind')})")

            # Display individual completed lookups
            recent = store.get_recent_completions(limit=10)
            for item in reversed(recent):
                key = (item["word"], item["completed_at"])
                if key not in seen_completions:
                    seen_completions.add(key)
                    if len(seen_completions) > 100:
                        # prevent unbounded growth
                        seen_completions.clear()
                    ts = item["completed_at"].split("T")[-1][:8] if "T" in item["completed_at"] else item["completed_at"]
                    print(f"      -> [{ts}] Completed '{item['word']}' [{item['lane']}] -> {item['result_kind']} ({item['outcome']})")

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[Exited live stream]\n")


def print_logs(store: QueueStore, limit: int = 50, event_type: Optional[str] = None):
    logs = store.get_audit_logs(limit=limit, event_type=event_type)
    print("\n" + "=" * 75)
    print(f" HARBIE AUDIT TIMELINE (Last {len(logs)} Events)")
    print("=" * 75)
    if not logs:
        print(" No audit log events recorded yet.")
        print("=" * 75 + "\n")
        return

    print(f"{'TIME (UTC)':<20} {'MODE':<11} {'EVENT':<16} {'MESSAGE'}")
    print("-" * 75)
    for entry in reversed(logs):
        ts = entry["created_at"].replace("T", " ")[:19]
        mode = entry["mode"]
        event = entry["event_type"]
        msg = entry["message"]
        print(f"{ts:<20} {mode:<11} {event:<16} {msg}")
    print("=" * 75 + "\n")


def print_night_summary(store: QueueStore):
    summary = store.get_overnight_summary()
    print("\n" + "=" * 70)
    print(" OVERNIGHT HARBIE ACTIVITY & AUDIT SUMMARY")
    print("=" * 70)
    print(f" Activity Window : {summary.get('first_event') or 'None'} to {summary.get('last_event') or 'None'}")
    print(f" Micro-Bursts    : {summary.get('total_bursts', 0):,} bursts fired")
    print(f" Total Lookups   : {summary.get('total_lookups', 0):,} lookups completed")
    print("-" * 70)
    print(" Event Counts Breakdown:")
    for ev, cnt in summary.get("event_counts", {}).items():
        print(f"   - {ev:<18} : {cnt:,}")
    print("-" * 70)
    transitions = summary.get("transitions", [])
    if transitions:
        print(" Mode Transitions:")
        for t in transitions:
            ts = t["at"].replace("T", " ")[:19]
            print(f"   [{ts}] {t['mode']} -> {t['message']}")
    else:
        print(" Mode Transitions: None recorded")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Harbie remote control, live observer, and audit logger")
    parser.add_argument("--push-live", nargs="+", help="Push one or more words into the highest-priority LIVE lane for immediate lookup")
    parser.add_argument("--force-offline", action="store_true", help="Force Harbie into OFFLINE mode for testing while laptop is connected")
    parser.add_argument("--auto", action="store_true", help="Restore normal automatic heartbeat detection (clears forced mode)")
    parser.add_argument("--force-connected", action="store_true", help="Force Harbie into CONNECTED mode")
    parser.add_argument("--pause", action="store_true", help="Pause Harbie worker")
    parser.add_argument("--resume", action="store_true", help="Resume Harbie worker")
    parser.add_argument("--status", action="store_true", help="Print current status and heartbeat telemetry")
    parser.add_argument("--watch", action="store_true", help="Live stream telemetry and watch micro-burst lookups in real-time")
    parser.add_argument("--log", action="store_true", help="Print the audit log timeline of events and lookups")
    parser.add_argument("--limit", type=int, default=50, help="Number of log entries to display (default 50)")
    parser.add_argument("--audit", "--night-summary", dest="audit", action="store_true", help="Print aggregate overnight summary (bursts, lookups, transitions)")
    parser.add_argument("--pa-host", type=str, default="127.0.0.1")
    parser.add_argument("--pa-port", type=int, default=13306)
    parser.add_argument("--pa-user", type=str, default="badangel")
    parser.add_argument("--pa-password", type=str, default="SandyMagdalena")
    args = parser.parse_args()

    store = get_store(args)

    if args.push_live:
        clean_words = [w.strip().lower() for w in args.push_live if w.strip()]
        inserted = store.push_live_words(clean_words)
        store.update_harvey_heartbeat()
        print(f"\n[OK] Pushed {len(clean_words)} words to LIVE lane (newly inserted: {inserted}).")
        print(f"     Words: {clean_words}\n")
        print_status(store)
    elif args.force_offline:
        store.set_forced_mode(MODE_OFFLINE)
        print("\n[OK] Harbie forced into OFFLINE TEST MODE. You can now observe its offline micro-bursts live!\n")
        print_status(store)
    elif args.auto:
        store.set_forced_mode(None)
        print("\n[OK] Harbie returned to AUTO-DETECT mode.\n")
        print_status(store)
    elif args.force_connected:
        store.set_forced_mode(MODE_CONNECTED)
        print("\n[OK] Harbie forced into CONNECTED MODE.\n")
        print_status(store)
    elif args.pause:
        ctrl = store.get_control()
        ctrl["paused"] = True
        store.set_control(ctrl)
        print("\n[OK] Harbie worker PAUSED.\n")
    elif args.resume:
        ctrl = store.get_control()
        ctrl["paused"] = False
        store.set_control(ctrl)
        print("\n[OK] Harbie worker RESUMED.\n")
    elif args.watch:
        watch_live(store)
    elif args.log:
        print_logs(store, limit=args.limit)
    elif args.audit:
        print_night_summary(store)
    else:
        # Default action
        print_status(store)


if __name__ == "__main__":
    main()
