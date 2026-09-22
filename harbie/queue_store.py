"""Harbie Queue Store: Database abstraction for the Harbie queue and control tables.

Supports both MySQL (for live PythonAnywhere production) and SQLite (for tests).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .common import (
    DEFAULT_CONTROL,
    HEARTBEAT_TIMEOUT_SEC,
    LANE_CANDIDATE,
    LANE_HOOVER,
    MODE_CONNECTED,
    MODE_OFFLINE,
    MYSQL_SCHEMA,
    SQLITE_SCHEMA,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    utc_now,
)


class QueueStore:
    def __init__(self, connection_factory, is_mysql: bool = False):
        self.connection_factory = connection_factory
        self.is_mysql = is_mysql
        self.init_schema()

    def init_schema(self) -> None:
        """Create tables if they do not exist and initialize default control row."""
        schema = MYSQL_SCHEMA if self.is_mysql else SQLITE_SCHEMA
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                for statement in schema:
                    cur.execute(statement)
                
                # Ensure default control record exists (id=1)
                cur.execute("SELECT COUNT(*) FROM harbie_control WHERE id = 1")
                exists = cur.fetchone()[0]
                if not exists:
                    placeholder = "%s" if self.is_mysql else "?"
                    cur.execute(
                        f"INSERT INTO harbie_control (id, value) VALUES (1, {placeholder})",
                        (json.dumps(DEFAULT_CONTROL),),
                    )
                conn.commit()
            finally:
                cur.close()

    @contextmanager
    def _connect(self):
        conn = self.connection_factory()
        try:
            yield conn
        finally:
            conn.close()

    def _param_char(self) -> str:
        return "%s" if self.is_mysql else "?"

    # -------------------------------------------------------------------------
    # Harvey (Planner / Laptop) APIs
    # -------------------------------------------------------------------------

    def push_batch(self, items: List[Dict[str, Any]]) -> int:
        """Push a batch of words into the queue.

        Each item is a dict: {'word': str, 'lane': 'CANDIDATE'|'HOOVER', 'priority': int}.
        Returns count of newly inserted rows.
        """
        if not items:
            return 0

        p = self._param_char()
        inserted = 0
        insert_cmd = "INSERT IGNORE INTO" if self.is_mysql else "INSERT OR IGNORE INTO"
        sql = f"""{insert_cmd} harbie_queue 
            (id, word, lane, priority, status, created_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p})"""

        now = utc_now()
        params = []
        for it in items:
            word = it["word"].strip()
            if not word:
                continue
            job_id = uuid.uuid4().hex
            lane = it.get("lane", LANE_CANDIDATE)
            priority = int(it.get("priority", 1000))
            params.append((job_id, word, lane, priority, STATUS_PENDING, now))

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.executemany(sql, params)
                inserted = cur.rowcount
                conn.commit()
            finally:
                cur.close()
        return inserted

    def update_harvey_heartbeat(self) -> None:
        """Update Harvey's last-seen timestamp to signal laptop is active."""
        now = utc_now()
        ctrl = self.get_control()
        ctrl["harvey_last_seen"] = now
        ctrl["mode"] = MODE_CONNECTED
        self.set_control(ctrl)

    # -------------------------------------------------------------------------
    # Control & Mode
    # -------------------------------------------------------------------------

    def get_control(self) -> Dict[str, Any]:
        """Fetch the control settings from the database."""
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT value FROM harbie_control WHERE id = 1")
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
                return dict(DEFAULT_CONTROL)
            finally:
                cur.close()

    def set_control(self, control_dict: Dict[str, Any]) -> None:
        """Update the control settings."""
        p = self._param_char()
        sql = f"UPDATE harbie_control SET value = {p} WHERE id = 1"
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, (json.dumps(control_dict),))
                conn.commit()
            finally:
                cur.close()

    def set_forced_mode(self, mode: Optional[str]) -> None:
        """Manually force Harbie into a specific mode (e.g. 'OFFLINE' for testing) or None for auto."""
        ctrl = self.get_control()
        ctrl["forced_mode"] = mode
        self.set_control(ctrl)

    def determine_mode(self, timeout_sec: int = HEARTBEAT_TIMEOUT_SEC) -> str:
        """Determine whether Harbie is in CONNECTED mode or OFFLINE mode.

        1. If 'forced_mode' is set to 'OFFLINE' or 'CONNECTED', override automatically.
        2. Otherwise, check Harvey's heartbeat:
           If Harvey's last heartbeat is within timeout_sec, Harbie is CONNECTED.
           If Harvey has gone dark / laptop shut -> Harbie is OFFLINE.
        """
        ctrl = self.get_control()
        forced = ctrl.get("forced_mode")
        if forced in (MODE_CONNECTED, MODE_OFFLINE):
            return forced

        last_seen_str = ctrl.get("harvey_last_seen")
        if not last_seen_str:
            return MODE_OFFLINE

        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = (now - last_seen).total_seconds()
            if delta <= timeout_sec:
                return MODE_CONNECTED
            return MODE_OFFLINE
        except Exception:
            return MODE_OFFLINE

    # -------------------------------------------------------------------------
    # Worker (Harbie / PA) APIs
    # -------------------------------------------------------------------------

    def claim_next_word(self, lane: str) -> Optional[Dict[str, Any]]:
        """Atomically claim the highest priority pending word in the given lane.

        Returns dict with row info or None if lane is empty.
        """
        p = self._param_char()
        now = utc_now()

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                # Find the next pending candidate
                cur.execute(
                    f"""SELECT id, word, lane, priority 
                    FROM harbie_queue 
                    WHERE lane = {p} AND status = {p}
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1""",
                    (lane, STATUS_PENDING),
                )
                row = cur.fetchone()
                if not row:
                    return None

                job_id, word, item_lane, priority = row[0], row[1], row[2], row[3]

                # Mark IN_FLIGHT
                cur.execute(
                    f"""UPDATE harbie_queue 
                    SET status = {p}, claimed_at = {p}
                    WHERE id = {p} AND status = {p}""",
                    (STATUS_IN_FLIGHT, now, job_id, STATUS_PENDING),
                )
                conn.commit()

                if cur.rowcount == 0:
                    # Race condition: another thread/worker claimed it
                    return None

                return {
                    "id": job_id,
                    "word": word,
                    "lane": item_lane,
                    "priority": priority,
                }
            finally:
                cur.close()

    def complete_word(
        self,
        job_id: str,
        result_kind: str,
        outcome: str,
        payload: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Mark a claimed word as completed."""
        p = self._param_char()
        now = utc_now()
        status = STATUS_DONE if outcome != "error" else STATUS_FAILED

        sql = f"""UPDATE harbie_queue
            SET status = {p}, completed_at = {p}, result_kind = {p}, 
                outcome = {p}, payload = {p}, error = {p}
            WHERE id = {p}"""

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    sql,
                    (status, now, result_kind, outcome, payload, error, job_id),
                )
                conn.commit()
            finally:
                cur.close()

    def record_heartbeat(self, report: Dict[str, Any]) -> None:
        """Record Harbie's live worker report."""
        p = self._param_char()
        now = utc_now()
        report_encoded = json.dumps(report)

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                if self.is_mysql:
                    cur.execute(
                        f"""INSERT INTO harbie_heartbeat (id, at, value) 
                        VALUES (1, {p}, {p})
                        ON DUPLICATE KEY UPDATE at = VALUES(at), value = VALUES(value)""",
                        (now, report_encoded),
                    )
                else:
                    cur.execute(
                        f"""INSERT OR REPLACE INTO harbie_heartbeat (id, at, value)
                        VALUES (1, {p}, {p})""",
                        (now, report_encoded),
                    )
                conn.commit()
            finally:
                cur.close()

    def get_stats(self) -> Dict[str, Any]:
        """Return counts of words by lane and status."""
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """SELECT lane, status, count(*) 
                    FROM harbie_queue 
                    GROUP BY lane, status"""
                )
                rows = cur.fetchall()
                breakdown = {}
                for r in rows:
                    lane, status, cnt = r[0], r[1], r[2]
                    breakdown.setdefault(lane, {})[status] = cnt
                return breakdown
            finally:
                cur.close()

    def get_heartbeat(self) -> Optional[Dict[str, Any]]:
        """Fetch the most recent live heartbeat report from Harbie."""
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT at, value FROM harbie_heartbeat WHERE id = 1")
                row = cur.fetchone()
                if row and row[1]:
                    data = json.loads(row[1])
                    data["reported_at"] = row[0]
                    return data
                return None
            finally:
                cur.close()

    def get_recent_completions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch recently completed lookups from Harbie."""
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""SELECT word, lane, result_kind, outcome, completed_at
                    FROM harbie_queue
                    WHERE status = '{STATUS_DONE}'
                    ORDER BY completed_at DESC
                    LIMIT {limit}"""
                )
                rows = cur.fetchall()
                completions = []
                for r in rows:
                    completions.append({
                        "word": r[0],
                        "lane": r[1],
                        "result_kind": r[2],
                        "outcome": r[3],
                        "completed_at": r[4],
                    })
                return completions
            finally:
                cur.close()

    def log_event(
        self,
        event_type: str,
        mode: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a structured audit log event in harbie_log."""
        p = self._param_char()
        now = utc_now()
        details_str = json.dumps(details) if details else None

        sql = f"""INSERT INTO harbie_log (created_at, event_type, mode, message, details_json)
                  VALUES ({p}, {p}, {p}, {p}, {p})"""

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, (now, event_type, mode, message, details_str))
                conn.commit()
            finally:
                cur.close()

    def get_audit_logs(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch audit log entries for timeline inspection."""
        p = self._param_char()
        where_clauses = []
        params = []

        if event_type:
            where_clauses.append(f"event_type = {p}")
            params.append(event_type)
        if since:
            where_clauses.append(f"created_at >= {p}")
            params.append(since)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""SELECT id, created_at, event_type, mode, message, details_json
                  FROM harbie_log
                  {where_sql}
                  ORDER BY id DESC
                  LIMIT {limit}"""

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
                logs = []
                for r in rows:
                    details = json.loads(r[5]) if r[5] else None
                    logs.append({
                        "id": r[0],
                        "created_at": r[1],
                        "event_type": r[2],
                        "mode": r[3],
                        "message": r[4],
                        "details": details,
                    })
                return logs
            finally:
                cur.close()

    def get_overnight_summary(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate overnight activity: bursts fired, calls made, hits, misses, and transitions."""
        p = self._param_char()
        since_clause = f"WHERE created_at >= {p}" if since else ""
        params = (since,) if since else ()

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""SELECT event_type, count(*) 
                    FROM harbie_log 
                    {since_clause}
                    GROUP BY event_type""",
                    params,
                )
                counts = {r[0]: r[1] for r in cur.fetchall()}

                # Mode transitions
                cur.execute(
                    f"""SELECT created_at, mode, message 
                    FROM harbie_log 
                    WHERE event_type = 'MODE_CHANGE' {"AND created_at >= " + p if since else ""}
                    ORDER BY id ASC""",
                    params,
                )
                transitions = [{"at": r[0], "mode": r[1], "message": r[2]} for r in cur.fetchall()]

                # First and last event timestamp
                cur.execute(
                    f"""SELECT MIN(created_at), MAX(created_at) FROM harbie_log {since_clause}""",
                    params,
                )
                min_max = cur.fetchone()

                return {
                    "first_event": min_max[0] if min_max else None,
                    "last_event": min_max[1] if min_max else None,
                    "event_counts": counts,
                    "total_bursts": counts.get("BURST_END", 0),
                    "total_lookups": counts.get("LOOKUP", 0),
                    "transitions": transitions,
                }
            finally:
                cur.close()


