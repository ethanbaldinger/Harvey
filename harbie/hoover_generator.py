"""Hoover Generator: Extracts and sorts ultra-safe 16+ letter candidate words

for bottomless offline shock absorber execution across all workers.

Words of length >= 16 have an infinitesimal hit rate in Merriam-Webster Collegiate,
producing unbilled MISS responses. Sorting them longest-to-shortest guarantees
maximum safety and zero billing surprises during long unattended runs.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .common import LANE_HOOVER

LOG = logging.getLogger("harbie.hoover_gen")


def extract_hoover_from_wordlists(
    base_dir: Optional[Path] = None,
    min_length: int = 16,
    max_length: int = 40,
) -> Set[str]:
    """Extract distinct alphabetic words from local wordlists with length >= min_length."""
    root = base_dir or Path(r"c:\BaseFinder\wordlists")
    if not root.exists():
        # Fall back to relative search
        alt = Path.cwd() / "wordlists"
        if alt.exists():
            root = alt

    target_files = [
        "compound.txt",
        "single.txt",
        "2of12full.txt",
        "scowl60.txt",
        "enable.txt",
        "crosswd.txt",
    ]

    words: Set[str] = set()
    for fn in target_files:
        p = root / fn
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                raw = line.strip().split()[0].strip() if line.strip() else ""
                clean = raw.lower()
                if clean.isalpha() and min_length <= len(clean) <= max_length:
                    words.add(clean)
        except Exception as exc:
            LOG.warning("Could not read wordlist %s: %s", p, exc)

    return words


def extract_hoover_from_db(
    conn_factory: Callable[[], Any],
    min_length: int = 16,
    max_length: int = 40,
    limit: int = 20000,
) -> List[str]:
    """Extract unverified words from local all_your_base database."""
    words: List[str] = []
    conn = conn_factory()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DISTINCT LOWER(TRIM(word)) AS clean_word
            FROM word
            WHERE COALESCE(mw_status, 0) = 0
              AND word IS NOT NULL
              AND TRIM(word) <> ''
              AND CHAR_LENGTH(TRIM(word)) BETWEEN %s AND %s
            ORDER BY CHAR_LENGTH(clean_word) DESC, clean_word ASC
            LIMIT %s
            """,
            (min_length, max_length, limit),
        )
        for r in cur.fetchall():
            w = str(r[0]).strip().lower()
            if w.isalpha():
                words.append(w)
    finally:
        cur.close()
        conn.close()
    return words


def build_sorted_hoover_pool(
    local_conn_factory: Optional[Callable[[], Any]] = None,
    min_length: int = 16,
    max_length: int = 40,
    limit: int = 10000,
    wordlists_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Build a deduplicated, longest-first Hoover candidate list ready for queue insertion.
    
    Items are ordered strictly:
    1. Length DESC (longest words first: 31, 30, ... 16)
    2. Alphabetical ASC
    """
    all_words: Set[str] = set()

    # 1. Harvest from wordlists
    wl_words = extract_hoover_from_wordlists(wordlists_dir, min_length=min_length, max_length=max_length)
    all_words.update(wl_words)
    LOG.info("Harvested %d candidate words from local wordlists", len(wl_words))

    # 2. Harvest from all_your_base if connection is available
    if local_conn_factory:
        try:
            db_words = extract_hoover_from_db(local_conn_factory, min_length=min_length, max_length=max_length)
            all_words.update(db_words)
            LOG.info("Harvested %d candidate words from database (combined total: %d)", len(db_words), len(all_words))
        except Exception as exc:
            LOG.warning("Could not query local database for hoover words: %s", exc)

    # 3. Sort strictly longest to shortest
    sorted_words = sorted(all_words, key=lambda w: (-len(w), w))

    if limit > 0:
        sorted_words = sorted_words[:limit]

    items = []
    for idx, w in enumerate(sorted_words):
        items.append({
            "word": w,
            "lane": LANE_HOOVER,
            "priority": 1000 + idx,  # priority preserved in longest-first order
            "word_len": len(w),
        })

    return items


def main():
    parser = argparse.ArgumentParser(description="Generate longest-first Hoover reserve pool")
    parser.add_argument("--min-len", type=int, default=16, help="Minimum word length (default 16)")
    parser.add_argument("--limit", type=int, default=10000, help="Maximum number of words (default 10,000)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pool = build_sorted_hoover_pool(min_length=args.min_len, limit=args.limit)
    print(f"Generated {len(pool)} Hoover words (min len {args.min_len}).")
    if pool:
        print(f"Top 5 longest: {[p['word'] + ' (' + str(p['word_len']) + ')' for p in pool[:5]]}")
        print(f"Last 5 shortest: {[p['word'] + ' (' + str(p['word_len']) + ')' for p in pool[-5:]]}")


if __name__ == "__main__":
    main()
