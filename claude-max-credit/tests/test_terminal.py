"""What a terminal or Hide pane sees: a resize reaches Claude, and one key prompt per account."""
import contextlib
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from credit_proxy import CreditError, local_token
from test_failover import ONE


def wait_for(path, seconds):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return path.exists()


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='credit-terminal-')
        self.dir = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_window_resize_reaches_claude_in_its_own_session(self):
        # A pane resize signals the launcher's process group only; Claude runs
        # in a session of its own, so the launcher has to pass the resize on.
        keys = self.dir / 'keys.env'
        keys.write_text(f'ANTHROPIC_API_KEY_1={ONE}\n')
        keys.chmod(0o600)
        ready, resized = self.dir / 'ready', self.dir / 'resized'
        fake = self.dir / 'claude'
        fake.write_text(f'#!{sys.executable}\n'
                        'import pathlib,signal,sys,time\n'
                        'signal.signal(signal.SIGWINCH, lambda *_: pathlib.Path(sys.argv[2]).touch())\n'
                        'pathlib.Path(sys.argv[1]).touch()\n'
                        'time.sleep(20)\n')
        fake.chmod(0o700)
        entry = self.dir / 'entry.py'
        entry.write_text(f'import sys,pathlib;sys.path.insert(0,{str(SOURCE)!r})\n'
                         'from credit_proxy import KeyPool,run_terminal\n'
                         f'sys.exit(run_terminal(KeyPool(pathlib.Path({str(keys)!r})),'
                         f'[{str(ready)!r},{str(resized)!r}],cli=pathlib.Path({str(fake)!r})))\n')
        launcher = subprocess.Popen([sys.executable, str(entry)], stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        try:
            self.assertTrue(wait_for(ready, 10))
            os.kill(launcher.pid, signal.SIGWINCH)
            self.assertTrue(wait_for(resized, 3))
            self.assertIsNone(launcher.poll(), 'a resize must not end the session')
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(launcher.pid, signal.SIGTERM)
            launcher.wait(10)

    def test_local_token_is_kept_so_claude_asks_once_per_account(self):
        path = self.dir / 'state' / 'local-token'
        first = local_token(path)
        self.assertEqual(local_token(path), first)
        self.assertTrue(first.startswith('sk-ant-proxy-'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaises(CreditError):
            local_token(path)


if __name__ == '__main__':
    unittest.main()
