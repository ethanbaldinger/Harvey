"""Universal Worker Distribution Builder: Packages standalone laptop workers (Darbie and beyond).

Usage:
  python build_worker_package.py [WORKER_NAME]

Examples:
  python build_worker_package.py Darbie
  python build_worker_package.py Earbie
  python build_worker_package.py Farbie
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

# Add Harvey root to sys.path
HARVEY_ROOT = Path(__file__).resolve().parent
if str(HARVEY_ROOT) not in sys.path:
    sys.path.insert(0, str(HARVEY_ROOT))

try:
    from harbie.hoover_generator import extract_hoover_from_wordlists
except ImportError:
    extract_hoover_from_wordlists = None


def generate_seed_reserve(root_dir: Path, cand_count: int = 1200, hoover_count: int = 1500) -> dict:
    """Extract candidate and hoover words for the worker's offline reserve."""
    print("  -> Generating offline candidate & hoover reserve...")
    
    # 1. Hoover words (16+ letters, sorted longest-first)
    hoover_words = []
    if extract_hoover_from_wordlists:
        all_hoover = extract_hoover_from_wordlists(min_length=16, max_length=35)
        hoover_words = sorted(list(all_hoover), key=lambda w: (-len(w), w))[:hoover_count]
    
    if not hoover_words:
        # Fallback list if wordlists directory is not locally available
        hoover_words = [
            "dichlorodiphenyltrichloroethane",
            "cyclotrimethylenetrinitramine",
            "trinitrophenylmethylnitramine",
            "antidisestablishmentarianism",
            "ethylenediaminetetraacetates",
            "electroencephalographically",
            "uncharacteristically",
            "incomprehensibility",
            "counterrevolutionaries",
        ] * 100
        hoover_words = hoover_words[:hoover_count]

    # 2. Candidate words (ENABLE / 2of12 curated common words)
    cand_words = []
    enable_path = Path(r"c:\BaseFinder\wordlists\enable.txt")
    if not enable_path.exists():
        enable_path = Path.cwd() / "wordlists" / "enable.txt"

    if enable_path.exists():
        try:
            raw = [
                line.strip().lower()
                for line in enable_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip().isalpha() and 6 <= len(line.strip()) <= 11
            ]
            # Deterministic pseudo-random shuffle for good dispersion
            import random
            rng = random.Random(42)
            rng.shuffle(raw)
            cand_words = raw[:cand_count]
        except Exception:
            pass

    if not cand_words:
        cand_words = ["abecedarian", "percipient", "mellifluous", "tintinnabulation", "serendipity"] * 200

    print(f"     Loaded {len(cand_words):,} candidate words and {len(hoover_words):,} Hoover shock absorbers.")
    return {"candidates": cand_words, "hoover": hoover_words}


def create_readme(worker_name: str) -> str:
    return f"""# {worker_name} — Merriam-Webster Remote Lookup Worker

Welcome! This package turns this computer into an autonomous helper worker for Merriam-Webster dictionary lookups.

---

### Quick 3-Step Setup

1. **Extract this zip file** into a folder of your choice (e.g. `C:\\{worker_name}`).
2. **Double-click `Setup.bat`**:
   - The setup wizard will open.
   - Enter your Merriam-Webster Collegiate API key when prompted (or press Enter for test mode).
3. **Double-click `Start.bat`**:
   - The worker window will open and begin running.
   - You will see lookups progress in real-time.

---

### Helpful Controls
- **`Check_Status.bat`**: Double-click anytime to see today's completed calls, hits, and remaining quota.
- **`Stop.bat`**: Double-click to safely shut down the worker.
- **Safety Guarantee**: The worker has a hard ceiling of 1,000 calls per calendar day (UTC) and automatically regulates hit rate to protect your API quota.

Thank you for contributing!
"""


def build_package(worker_name: str = "Darbie") -> Path:
    worker_clean = worker_name.strip().capitalize()
    print(f"\n====================================================================")
    print(f"  BUILDING REMOTE WORKER PACKAGE: {worker_clean.upper()}")
    print(f"====================================================================")

    dist_dir = HARVEY_ROOT / "dist"
    dist_dir.mkdir(exist_ok=True)
    zip_path = dist_dir / f"{worker_clean}-worker.zip"

    darbie_dir = HARVEY_ROOT / "darbie"

    # 1. Generate fresh seed reserve
    seed_reserve = generate_seed_reserve(HARVEY_ROOT)
    seed_path = darbie_dir / "seed_reserve.json"
    seed_path.write_text(json.dumps(seed_reserve, indent=2), encoding="utf-8")

    # 2. Generate customized README
    readme_content = create_readme(worker_clean)
    (darbie_dir / "README.md").write_text(readme_content, encoding="utf-8")

    # 3. Generate example configuration
    example_cfg = {
        "worker_name": worker_clean.upper(),
        "database": "worker.sqlite3",
        "mailbox_url": "https://badangel.pythonanywhere.com/barbie",
        "worker_token": f"worker-token-{worker_clean.lower()}-basefinder-2026",
        "mirror_url": "https://badangel.pythonanywhere.com",
        "mirror_token": "",
        "mw_key": "PASTE_YOUR_MERRIAM_WEBSTER_API_KEY_HERE",
        "replay_only": False,
        "dynamic_slope": True,
    }
    (darbie_dir / "worker.example.json").write_text(json.dumps(example_cfg, indent=2), encoding="utf-8")

    # 4. Files to package
    files_to_pack = [
        ("worker.py", "worker.py"),
        ("common.py", "common.py"),
        ("setup_worker.py", "setup_worker.py"),
        ("worker.example.json", "worker.example.json"),
        ("seed_reserve.json", "seed_reserve.json"),
        ("Setup.bat", "Setup.bat"),
        ("Start.bat", "Start.bat"),
        ("Stop.bat", "Stop.bat"),
        ("Check_Status.bat", "Check_Status.bat"),
        ("README.md", "README.md"),
    ]

    print(f"  -> Assembling {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as arc:
        for src_name, arc_name in files_to_pack:
            src = darbie_dir / src_name
            if src.exists():
                arc.write(src, arc_name)
                print(f"     + {arc_name} ({src.stat().st_size:,} bytes)")
            else:
                print(f"     ! MISSING: {src_name}")

    # 5. Compute SHA256
    raw_data = zip_path.read_bytes()
    digest = hashlib.sha256(raw_data).hexdigest()
    sha_file = zip_path.with_suffix(".sha256")
    sha_file.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")

    print("-" * 68)
    print(f"  SUCCESS: Package created successfully!")
    print(f"  Target: {zip_path.resolve()}")
    print(f"  Size  : {len(raw_data):,} bytes")
    print(f"  SHA256: {digest}")
    print("=" * 68 + "\n")
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="Universal Worker Package Builder")
    parser.add_argument("worker_name", nargs="?", default="Darbie", help="Name of the worker (e.g. Darbie, Earbie)")
    args = parser.parse_args()

    build_package(args.worker_name)


if __name__ == "__main__":
    main()
