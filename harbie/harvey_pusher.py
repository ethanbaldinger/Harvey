"""Harvey Queue Pusher: Extracts candidate priorities and hoover pools from local

all_your_base database and deploys batches to Harbie on PythonAnywhere.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .common import LANE_CANDIDATE, LANE_HOOVER, LANE_LIVE
from .hoover_generator import build_sorted_hoover_pool
from .queue_store import QueueStore

LOG = logging.getLogger("harbie.pusher")


class HarveyPusher:
    def __init__(self, target_store: QueueStore, local_conn_factory=None):
        self.store = target_store
        self.local_conn_factory = local_conn_factory

    def extract_local_candidates(
        self,
        candidate_limit: int = 15000,
        hoover_limit: int = 10000,
        min_hoover_len: int = 16,
        max_hoover_len: int = 40,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Extract priority candidate words and 16+ hoover buffer words (longest first)."""
        candidates: List[Dict[str, Any]] = []

        if self.local_conn_factory:
            conn = self.local_conn_factory()
            cur = conn.cursor()
            try:
                # 1. High-priority candidate words from unlit bases (lengths 3 to 15)
                cur.execute(
                    """
                    SELECT TRIM(word) AS word, CHAR_LENGTH(TRIM(word)) AS word_len
                    FROM word
                    WHERE COALESCE(mw_status, 0) = 0
                      AND word IS NOT NULL
                      AND TRIM(word) <> ''
                      AND CHAR_LENGTH(TRIM(word)) BETWEEN 3 AND 15
                    ORDER BY word_len ASC
                    LIMIT %s
                    """,
                    (candidate_limit,),
                )
                for idx, r in enumerate(cur.fetchall()):
                    word = str(r[0]).strip().lower()
                    if word and word.isalpha():
                        candidates.append({
                            "word": word,
                            "lane": LANE_CANDIDATE,
                            "priority": idx + 1,
                        })
            finally:
                cur.close()
                conn.close()

        # 2. Extract massive 16+ Hoover pool ordered longest to shortest
        hoover = build_sorted_hoover_pool(
            local_conn_factory=self.local_conn_factory,
            min_length=min_hoover_len,
            max_length=max_hoover_len,
            limit=hoover_limit,
        )

        return candidates, hoover

    def push_live_batch(self, words: List[str], priority: int = 1) -> int:
        """Push a real-time priority batch into the LIVE lane."""
        inserted = self.store.push_live_words(words, priority=priority)
        self.store.update_harvey_heartbeat()
        LOG.info("Dispatched %d words to LIVE lane (inserted: %d)", len(words), inserted)
        return inserted

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

    def maintain_reserves(
        self,
        min_candidate_depth: int = 2000,
        min_hoover_depth: int = 5000,
        candidate_topup: int = 3000,
        hoover_topup: int = 5000,
    ) -> Dict[str, Any]:
        """Check remote queue backlog and automatically replenish if reserves are low."""
        counts = self.store.get_lane_counts()
        cand_pending = counts.get(LANE_CANDIDATE, 0)
        hoover_pending = counts.get(LANE_HOOVER, 0)
        live_pending = counts.get(LANE_LIVE, 0)

        LOG.info(
            "Current remote queue depth: LIVE=%d, CANDIDATE=%d, HOOVER=%d",
            live_pending, cand_pending, hoover_pending,
        )

        cand_added = 0
        hoover_added = 0

        # Replenish candidate reserve if low
        if cand_pending < min_candidate_depth and self.local_conn_factory:
            LOG.info("Candidate reserve below %d (has %d). Topping up %d fresh candidates...", min_candidate_depth, cand_pending, candidate_topup)
            cands, _ = self.extract_local_candidates(candidate_limit=candidate_topup, hoover_limit=0)
            if cands:
                cand_added = self.store.push_batch(cands)
                LOG.info("Topped up %d fresh candidate words.", cand_added)

        # Replenish Hoover reserve if low
        if hoover_pending < min_hoover_depth:
            LOG.info("Hoover reserve below %d (has %d). Topping up %d 16+ words...", min_hoover_depth, hoover_pending, hoover_topup)
            _, hoover = self.extract_local_candidates(candidate_limit=0, hoover_limit=hoover_topup)
            if hoover:
                hoover_added = self.store.push_batch(hoover)
                LOG.info("Topped up %d 16+ Hoover words.", hoover_added)

        self.store.update_harvey_heartbeat()

        return {
            "initial_counts": counts,
            "candidates_added": cand_added,
            "hoover_added": hoover_added,
            "heartbeat_updated": True,
        }

