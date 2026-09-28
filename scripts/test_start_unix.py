import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import start_unix as launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backend.json').write_text(json.dumps({'home': str(self.root), 'port': 3080}))
        (self.root / 'tauri.conf.json').write_text('{"version":"0.2.0"}')
        (self.root / 'target/release').mkdir(parents=True)
        self.binary = self.root / 'target/release/dsh-wsl-tauri'
        self.binary.write_bytes(b'previous executable')
        self.stamp = self.root / 'ui-build-linux.json'
        self.record = {'shellVersion': '0.2.0', 'sourceSha256': 'source',
                       'binarySha256': launcher.digest(self.binary)}
        self.stamp.write_text(json.dumps(self.record))
        for patcher in (patch.object(launcher, 'ROOT', self.root),
                        patch.object(launcher, 'PLATFORM', 'linux'),
                        patch.object(launcher, 'source_hash', return_value='source'),
                        patch('sys.argv', ['start_unix.py', '--build-only'])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_manager_launch_does_not_need_kernel_or_backend_config(self):
        (self.root / 'backend.json').unlink()
        with patch('sys.argv', ['start_unix.py', '--manage']), patch.object(launcher.subprocess, 'Popen') as launch:
            launcher.main()
            self.assertEqual(launch.call_args.args[0], [str(self.binary), '--manage'])

    def test_unchanged_build_is_reused(self):
        with patch.object(launcher.subprocess, 'run') as run:
            launcher.main()
            run.assert_not_called()

    def test_failed_build_preserves_success_record(self):
        self.binary.write_bytes(b'changed')
        previous = self.stamp.read_text()
        with patch.object(launcher.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'cargo')), \
                patch.object(launcher.subprocess, 'Popen') as launch:
            with self.assertRaises(subprocess.CalledProcessError):
                launcher.main()
            self.assertEqual(self.stamp.read_text(), previous)
            launch.assert_not_called()

    def test_changed_source_rebuilds_and_records_success(self):
        with patch.object(launcher, 'source_hash', return_value='updated'), \
                patch.object(launcher.subprocess, 'run') as run:
            launcher.main()
            self.assertEqual(run.call_args.args[0], ['cargo', 'build', '--release', '--locked'])
            self.assertEqual(json.loads(self.stamp.read_text())['sourceSha256'], 'updated')


if __name__ == '__main__':
    unittest.main()
