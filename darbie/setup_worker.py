"""One-time setup wizard on remote worker's laptop."""
from __future__ import annotations

import getpass
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request


def validate_mw_key(key: str) -> bool:
    """Test MW key with a benign test query to verify validity."""
    if not key or len(key) < 15:
        return False
    url = f"https://www.dictionaryapi.com/api/v3/references/collegiate/json/test?key={urllib.parse.quote(key)}"
    req = urllib.request.Request(url, headers={"User-Agent": "BaseFinder-Worker/0.1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict)
    except Exception as exc:
        print(f"  [WARN] Could not verify key online: {exc}")
        return True  # Don't block setup if offline


def main():
    root = Path(__file__).parent
    print("\n" + "=" * 64)
    print("  BASEFINDER REMOTE WORKER SETUP WIZARD")
    print("=" * 64)

    existing_cfg = root / "worker.json"
    if existing_cfg.exists():
        ans = input("Configuration file already exists. Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Setup cancelled. Existing configuration preserved.")
            return

    default_name = root.name.capitalize() if root.name != "worker" else "Darbie"
    name_input = input(f"Worker Name [{default_name}]: ").strip()
    worker_name = (name_input or default_name).upper()

    print(f"\nSetting up worker: {worker_name}")
    print("Enter your Merriam-Webster Collegiate API Key.")
    print("(Press Enter to leave blank and run in mirror-replay mode for testing)")
    mw_key = getpass.getpass("Merriam-Webster API Key: ").strip()

    if mw_key:
        print("Validating key with Merriam-Webster Collegiate API...")
        if validate_mw_key(mw_key):
            print("  [SUCCESS] Key verified! Active dictionary quota confirmed.")
        else:
            print("  [WARNING] Key validation was inconclusive. Proceeding anyway.")
    else:
        print("  [INFO] No key provided. Worker will run in replay test mode.")

    mailbox = input("\nPythonAnywhere Mailbox URL [https://badangel.pythonanywhere.com/barbie]: ").strip()
    mailbox = mailbox or "https://badangel.pythonanywhere.com/barbie"

    worker_token = getpass.getpass("Worker Token (press Enter for default): ").strip()
    if not worker_token:
        worker_token = f"worker-token-{worker_name.lower()}-basefinder-2026"

    config = {
        "worker_name": worker_name,
        "database": "worker.sqlite3",
        "mailbox_url": mailbox.rstrip("/"),
        "mirror_url": "https://badangel.pythonanywhere.com",
        "worker_token": worker_token,
        "mirror_token": "",
        "mw_key": mw_key,
        "replay_only": not bool(mw_key),
        "dynamic_slope": True,
    }

    (root / "worker.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    
    # Save private credentials
    creds = {"mw_key": mw_key, "worker_token": worker_token}
    (root / "credentials.json").write_text(json.dumps(creds, indent=2), encoding="utf-8")

    print("\n" + "-" * 64)
    print(f"  Configuration successfully written to worker.json!")
    print(f"  Worker '{worker_name}' is ready.")
    print(f"  To start worker: double-click Start.bat")
    print(f"  To check status: double-click Check_Status.bat")
    print("-" * 64 + "\n")


if __name__ == "__main__":
    main()
