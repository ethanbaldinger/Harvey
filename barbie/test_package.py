"""Exercise the extracted distribution as a real worker process."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from .build_package import FILES, main as build
from .mailbox import Mailbox
from .test_system import Server, WORKER_TOKEN, MANAGER_TOKEN


class PackageTest(unittest.TestCase):
    def test_extracted_worker_starts_delivers_stops_and_restarts(self):
        build()
        with tempfile.TemporaryDirectory(prefix='barbie-package-') as directory:
            root = Path(directory)
            with zipfile.ZipFile(Path(__file__).parent / 'dist' / 'Barbie-mirror-replay.zip') as archive:
                self.assertEqual(set(archive.namelist()), {'barbie/' + name for name in FILES})
                archive.extractall(root)
            mailbox = Mailbox(root / 'mail.sqlite3', WORKER_TOKEN, MANAGER_TOKEN)
            server = Server(mailbox)
            self.addCleanup(server.close)
            def fixture(environ, start_response):
                self.assertEqual(environ['QUERY_STRING'], 'held=1')
                start_response('200 OK', [('Content-Type', 'application/json')])
                return [b'[]']
            mirror = Server(fixture)
            self.addCleanup(mirror.close)
            config = root / 'barbie' / 'worker.json'
            config.write_text(json.dumps(dict(database='worker.sqlite3', mailbox_url=server.url,
                mirror_url=mirror.url, credentials_file='credentials.json')))
            (config.parent / 'credentials.json').write_text(json.dumps(dict(worker_token=WORKER_TOKEN, mirror_token='fixture')))
            mailbox.action('/manager/assign', dict(words=['previously-stored-fixture'], lane='reserve'))
            for iteration in range(2):
                last = mailbox.action('/manager/status', {})['last_report']
                previous = last['at'] if last else None
                command = [sys.executable, '-B', '-m', 'barbie.worker', '--config', str(config)]
                proc = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        status = mailbox.action('/manager/status', {})
                        if status['awaiting_manager'] == 1 and status['last_report']['at'] != previous:
                            break
                        time.sleep(.1)
                    self.assertEqual(mailbox.action('/manager/status', {})['awaiting_manager'], 1)
                    subprocess.run(command + ['--stop'], cwd=root, check=True, capture_output=True, timeout=20)
                    output, _ = proc.communicate(timeout=25)
                    self.assertEqual(proc.returncode, 0, output)
                    self.assertIn('stopped safely', output)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
