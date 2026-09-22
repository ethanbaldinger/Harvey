"""Shared protocol helpers for DARBIE worker; standard library only."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

VERSION = "0.1.0"
WORKER_NAME = "DARBIE"
DEFAULT_SETTINGS = {
    "gap_min": 4,
    "gap_max": 12,
    "daily_limit": 1000,
    "paused": False,
    "provider": "mirror_replay",
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def settings(value):
    if set(value) != set(DEFAULT_SETTINGS):
        raise ValueError("Settings must contain gap_min, gap_max, daily_limit, paused, provider")
    if value["provider"] not in ("mirror_replay", "mw_live"):
        raise ValueError("Unknown dictionary provider")
    if type(value["paused"]) is not bool:
        raise ValueError("paused must be boolean")
    if type(value["daily_limit"]) is not int or not 0 <= value["daily_limit"] <= 1000:
        raise ValueError("daily_limit must be an integer from 0 to 1000")
    if any(type(value[k]) not in (int, float) for k in ("gap_min", "gap_max")):
        raise ValueError("Pacing must be numeric")
    if not 4 <= value["gap_min"] <= value["gap_max"] <= 3600:
        raise ValueError("Pacing must satisfy 4 <= minimum <= maximum <= 3600")
    return dict(value)


@contextmanager
def db(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward a credential to a redirected destination.


def request(url, token, body=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    ):
        raise ValueError("HTTPS required except for loopback tests")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not be embedded in URLs")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"{WORKER_NAME}/" + VERSION}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(url, data=data, headers=headers)
    opener = build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("Response too large")
            return resp.status, json.loads(raw)
    except HTTPError as exc:
        raw = exc.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Response too large")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, None
