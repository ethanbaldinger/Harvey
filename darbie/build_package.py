"""Build a standalone Darbie worker distribution zip for easy laptop deployment."""
import hashlib
from pathlib import Path
import zipfile

FILES = (
    "__init__.py",
    "common.py",
    "worker.py",
    "setup_worker.py",
    "worker.example.json",
    "Setup.bat",
    "Start.bat",
    "Stop.bat",
    "README.md",
)


def main():
    root = Path(__file__).parent
    dist_dir = root / "dist"
    dist_dir.mkdir(exist_ok=True)
    output = dist_dir / "Darbie-worker.zip"
    
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            src = root / name
            if src.exists():
                archive.write(src, "darbie/" + name)
                
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    print(f"Created {output} (SHA256: {digest})")


if __name__ == "__main__":
    main()
