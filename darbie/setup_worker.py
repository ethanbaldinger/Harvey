"""One-time setup on Darbie's laptop. Credentials stay in a local private file."""
from getpass import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit


def main():
    root = Path(__file__).parent
    if (root / "worker.json").exists():
        raise SystemExit("Already configured. Existing settings were kept.")
    
    mailbox = input("Mailbox URL [https://badangel.pythonanywhere.com/barbie]: ").strip()
    mailbox = mailbox or "https://badangel.pythonanywhere.com/barbie"
    parsed = urlsplit(mailbox)
    if parsed.scheme != "https" or parsed.hostname != "badangel.pythonanywhere.com" or parsed.query or parsed.fragment:
        raise SystemExit("Expected the PythonAnywhere HTTPS mailbox URL")
    
    worker = getpass("Darbie worker token: ").strip()
    mirror = getpass("Darbie mirror credential (optional if using live key): ").strip()
    mw_key = getpass("Merriam-Webster API key (optional; leave blank for replay only): ").strip()

    cred_dict = dict(worker_token=worker, mirror_token=mirror)
    if mw_key:
        cred_dict["mw_key"] = mw_key

    credentials = root / "credentials.json"
    with credentials.open("w", encoding="utf-8") as handle:
        json.dump(cred_dict, handle, indent=2)

    config = dict(
        database="worker.sqlite3",
        mailbox_url=mailbox.rstrip("/"),
        mirror_url="https://badangel.pythonanywhere.com/mw",
        credentials_file="credentials.json",
        worker_token=worker,
        mirror_token=mirror,
        mw_key=mw_key,
        replay_only=not bool(mw_key),
    )
    (root / "worker.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    
    if mw_key:
        print("Ready with live MW API key (1000 daily quota protection active). Run Start.bat.")
    else:
        print("Ready. Run Start.bat. This worker will run in mirror replay mode.")


if __name__ == "__main__":
    main()
