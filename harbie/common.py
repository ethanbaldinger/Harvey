"""Harbie: Autonomous 24/7 Merriam-Webster Worker for PythonAnywhere.

Common definitions, schemas, and timing helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import random
import time
from typing import Any, Dict, Optional

VERSION = "0.1.0"
HARD_DAILY_LIMIT = 1000
HOOVER_THRESHOLD = 950
HEARTBEAT_TIMEOUT_SEC = 300  # 5 minutes: if Harvey hasn't checked in within 5m, Harbie goes Offline

# =========================================================================
# PACING & MICRO-BURST PARAMETERS (TUNE HERE)
# =========================================================================
# An active offline micro-burst performs between 3 and 6 lookups over ~9 seconds.
# After each burst completes, Harbie takes a randomized Gaussian rest pause.

BURST_MIN_LOOKUPS = 3       # Minimum number of lookups in a single micro-burst
BURST_MAX_LOOKUPS = 6       # Maximum number of lookups in a single micro-burst
BURST_DURATION_SEC = 9.0    # Target elapsed duration for the active burst window (seconds)

# Rest pause between micro-bursts follows a Gaussian (normal) distribution:
REST_MEAN_SEC = 80.0        # Mean duration of rest pause between bursts (seconds)
REST_STD_DEV_SEC = 10.0     # Standard deviation of rest pause (seconds)
REST_MIN_SEC = 30.0         # Absolute minimum safety floor for rest pause (seconds)

DEFAULT_PACING_SEC = 80
DEFAULT_JITTER_PCT = 15

LANE_CANDIDATE = "CANDIDATE"
LANE_HOOVER = "HOOVER"

STATUS_PENDING = "PENDING"
STATUS_IN_FLIGHT = "IN_FLIGHT"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"

MODE_CONNECTED = "CONNECTED"
MODE_OFFLINE = "OFFLINE"

def utc_now() -> str:
    """Return ISO8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()

def compute_intra_burst_delay(burst_size: int, target_duration: float = BURST_DURATION_SEC) -> float:
    """Compute spacing between lookups inside a micro-burst so the burst spans ~target_duration.
    
    For N lookups, there are (N - 1) intervals. Adds slight organic jitter (±15%).
    """
    if burst_size <= 1:
        return 0.0
    intervals = max(1, burst_size - 1)
    base_delay = target_duration / intervals
    jitter = random.uniform(0.85, 1.15)
    return max(0.5, base_delay * jitter)

def compute_burst_rest(
    mean_sec: float = REST_MEAN_SEC,
    std_dev_sec: float = REST_STD_DEV_SEC,
    min_sec: float = REST_MIN_SEC,
) -> float:
    """Compute randomized rest pause between bursts using Gaussian (normal) distribution.
    
    Mean = 80s, StdDev = 10s (approx 68% of rests fall between 70s and 90s).
    Clamped to min_sec floor.
    """
    sample = random.gauss(mean_sec, std_dev_sec)
    return max(min_sec, sample)

def compute_delay(base_sec: int = DEFAULT_PACING_SEC, jitter_pct: int = DEFAULT_JITTER_PCT) -> float:
    """Compute pacing delay in seconds with organic jitter (legacy single-call helper)."""
    pct = max(0, min(50, jitter_pct)) / 100.0
    factor = 1.0 + random.uniform(-pct, pct)
    return max(5.0, base_sec * factor)

DEFAULT_CONTROL = {
    "harvey_last_seen": None,
    "mode": MODE_OFFLINE,
    "forced_mode": None,  # None = auto-detect based on heartbeat; 'OFFLINE' = force offline test mode; 'CONNECTED' = force connected
    "pacing_seconds": DEFAULT_PACING_SEC,
    "jitter_pct": DEFAULT_JITTER_PCT,
    "daily_limit": HARD_DAILY_LIMIT,
    "hoover_threshold": HOOVER_THRESHOLD,
    "paused": False,
}

MYSQL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harbie_queue (
        id VARCHAR(32) PRIMARY KEY,
        word VARCHAR(250) COLLATE utf8mb4_bin NOT NULL,
        lane VARCHAR(16) NOT NULL,
        priority INT NOT NULL DEFAULT 1000,
        status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
        created_at VARCHAR(40) NOT NULL,
        claimed_at VARCHAR(40) NULL,
        completed_at VARCHAR(40) NULL,
        result_kind VARCHAR(16) NULL,
        outcome VARCHAR(32) NULL,
        payload LONGTEXT NULL,
        error VARCHAR(255) NULL,
        UNIQUE KEY uq_harbie_word (word),
        KEY ix_lane_status (lane, status, priority)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS harbie_control (
        id INT PRIMARY KEY,
        value LONGTEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS harbie_heartbeat (
        id INT PRIMARY KEY,
        at VARCHAR(40) NOT NULL,
        value LONGTEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS harbie_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        created_at VARCHAR(40) NOT NULL,
        event_type VARCHAR(32) NOT NULL,
        mode VARCHAR(16) NOT NULL,
        message VARCHAR(255) NOT NULL,
        details_json LONGTEXT NULL,
        KEY ix_log_time (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harbie_queue (
        id TEXT PRIMARY KEY,
        word TEXT NOT NULL UNIQUE,
        lane TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 1000,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TEXT NOT NULL,
        claimed_at TEXT,
        completed_at TEXT,
        result_kind TEXT,
        outcome TEXT,
        payload TEXT,
        error TEXT
    )""",

    """CREATE INDEX IF NOT EXISTS ix_lane_status ON harbie_queue (lane, status, priority)""",

    """CREATE TABLE IF NOT EXISTS harbie_control (
        id INTEGER PRIMARY KEY,
        value TEXT NOT NULL
    )""",

    """CREATE TABLE IF NOT EXISTS harbie_heartbeat (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        value TEXT NOT NULL
    )""",

    """CREATE TABLE IF NOT EXISTS harbie_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        mode TEXT NOT NULL,
        message TEXT NOT NULL,
        details_json TEXT
    )""",

    """CREATE INDEX IF NOT EXISTS ix_log_time ON harbie_log (created_at)""",
]
