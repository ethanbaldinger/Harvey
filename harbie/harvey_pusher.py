"""Harvey Queue Pusher: Extracts candidate priorities and hoover pools from local

all_your_base database and deploys batches to Harbie on PythonAnywhere.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .common import LANE_CANDIDATE, LANE_HOOVER
from .queue_store import QueueStore

LOG = logging.getLogger("harbie.pusher")


class HarveyPusher:
    def __init__(self, target_store: QueueStore, local_conn_factory=None):
        self.store = target_store
        self.local_conn_factory = local_conn_factory

    def extract_local_candidates(
        self,
        candidate_limit: int = 15000,
        hoover_limit: int = 5000,
        min_hoover_len: int = 12,
        max_hoover_len: int = 24,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Extract priority candidate words and hoover buffer words from local all_your_base."""
        if not self.local_conn_factory:
            raise ValueError("Local connection factory required to query all_your_base")

        candidates: List[Dict[str, Any]] = []
        hoover: List[Dict[str, Any]] = []

        conn = self.local_conn_factory()
        cur = conn.cursor()
        try:
            # 1. High-priority candidate words from unlit bases
            # In all_your_base, unverified words have mw_status = 0.
            # We select distinct unverified words that appear in candidate tables, ordered by fanout / length.
            cur.execute(
                """
                SELECT TRIM(word) AS word, CHAR_LENGTH(TRIM(word)) AS word_len
                FROM word
                WHERE COALESCE(mw_status, 0) = 0
                  AND word IS NOT NULL
                  AND TRIM(word) <> ''
                  AND CHAR_LENGTH(TRIM(word)) BETWEEN 3 AND 16
                ORDER BY word_len ASC
                LIMIT %s
                """,
                (candidate_limit,),
            )
            for idx, r in enumerate(cur.fetchall()):
                word = str(r[0]).strip()
                if word:
                    candidates.append({
                        "word": word,
                        "lane": LANE_CANDIDATE,
                        "priority": idx + 1,
                    })

            # 2. Safe hoover pool: long words, fringe vocabulary with low HIT probability
            cur.execute(
                """
                SELECT DISTINCT TRIM(word) AS word, CHAR_LENGTH(TRIM(word)) AS word_len
                FROM word
                WHERE COALESCE(mw_status, 0) = 0
                  AND word IS NOT NULL
                  AND TRIM(word) <> ''
                  AND CHAR_LENGTH(TRIM(word)) BETWEEN %s AND %s
                ORDER BY word_len DESC
                LIMIT %s
                """,
                (min_hoover_len, max_hoover_len, hoover_limit),
            )
            for idx, r in enumerate(cur.fetchall()):
                word = str(r[0]).strip()
                if word:
                    hoover.append({
                        "word": word,
                        "lane": LANE_HOOVER,
                        "priority": 1000 + idx,
                    })

        finally:
            cur.close()
            conn.close()

        return candidates, hoover

    def push_work_package(
        self,
        candidate_items: List[Dict[str, Any]],
        hoover_items: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Push candidate and hoover items to target queue and ping Harvey heartbeat."""
        all_items = candidate_items + hoover_items
        inserted = self.store.push_batch(all_items)

        # Signal that Harvey is actively connected
        self.store.update_harvey_heartbeat()

        return {
            "candidates_prepared": len(candidate_items),
            "hoover_prepared": len(hoover_items),
            "total_submitted": len(all_items),
            "newly_inserted": inserted,
        }
