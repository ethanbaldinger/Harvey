"""Build a worker-only distribution with an explicit secret-free allowlist."""
import hashlib
from pathlib import Path
import zipfile

FILES = ('__init__.py', 'common.py', 'worker.py', 'setup_worker.py',
         'Setup.bat', 'Start.bat', 'Stop.bat', 'INSTALL.txt')


def main():
    root = Path(__file__).parent
    output = root / 'dist' / 'Barbie-mirror-replay.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root / name, 'barbie/' + name)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    print(output)
    print('SHA256 ' + digest)


if __name__ == '__main__':
    main()
