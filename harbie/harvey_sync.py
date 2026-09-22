"""Harvey Sync: Ingests completed Harbie lookups from PythonAnywhere back into

local all_your_base candidate database.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .common import STATUS_DONE
from .queue_store import QueueStore

LOG = logging.getLogger("harbie.sync")


class HarveySync:
    def __init__(self, remote_store: QueueStore, local_conn_factory, is_mysql: bool = True):
        self.remote_store = remote_store
        self.local_conn_factory = local_conn_factory
        self.is_mysql = is_mysql

    def sync_completed(self, limit: int = 5000) -> Dict[str, int]:
        """Fetch completed lookups from remote Harbie queue and apply to local database."""
        # Query completed entries from remote store
        p = self.remote_store._param_char()
        sql = f"""SELECT id, word, result_kind, outcome, completed_at
            FROM harbie_queue
            WHERE status = {p}
            ORDER BY completed_at ASC
            LIMIT {limit}"""

        with self.remote_store._connect() as rconn:
            rcur = rconn.cursor()
            try:
                rcur.execute(sql, (STATUS_DONE,))
                rows = rcur.fetchall()
            finally:
                rcur.close()

        if not rows:
            return {"synced": 0, "hits": 0, "misses": 0}

        hits = 0
        misses = 0
        updated_local = 0

        # Ingest into local all_your_base
        local_p = "%s" if self.is_mysql else "?"
        local_conn = self.local_conn_factory()
        local_cur = local_conn.cursor()
        try:
            for r in rows:
                job_id, word, result_kind, outcome, completed_at = r[0], r[1], r[2], r[3], r[4]
                status_val = 1 if result_kind == "HIT" else -1 if result_kind == "MISS" else 0
                if status_val == 1:
                    hits += 1
                elif status_val == -1:
                    misses += 1

                if status_val != 0:
                    local_cur.execute(
                        f"UPDATE word SET mw_status = {local_p} WHERE word = {local_p}",
                        (status_val, word),
                    )
                    updated_local += local_cur.rowcount
            local_conn.commit()
        finally:
            local_cur.close()
            local_conn.close()

        return {
            "completed_remote_rows": len(rows),
            "hits": hits,
            "misses": misses,
            "local_words_updated": updated_local,
        }
