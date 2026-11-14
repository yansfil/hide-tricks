"""Own process groups, including when the launcher is killed without cleanup."""
import os
import signal
import subprocess
import sys
import threading
import time


class OwnedProcess:
    def __init__(self, command, env=None, **stdio):
        reader, self.writer = os.pipe()
        try:
            self.process = subprocess.Popen(
                [sys.executable, __file__, str(reader), *command],
                env=env, pass_fds=(reader,), start_new_session=True, **stdio
            )
        except BaseException:
            os.close(self.writer)
            raise
        finally:
            os.close(reader)

    def send(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    def close(self):
        self.send(signal.SIGTERM)
        try:
            # The guard can exit before Claude flushes session files. Wait for
            # its entire group, so callers can safely remove owned directories.
            deadline = time.monotonic() + 2
            while self.group_alive() and time.monotonic() < deadline:
                self.process.poll()
                time.sleep(0.02)
            if self.group_alive():
                self.send(signal.SIGKILL)
            self.process.wait(timeout=2)
            deadline = time.monotonic() + 1
            while self.group_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.group_alive():
                raise ChildProcessError('Owned terminal process group did not exit.')
        finally:
            os.close(self.writer)

    def group_alive(self):
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False


def guard():
    reader = int(sys.argv[1])

    def owner_exited():
        try:
            while os.read(reader, 1):
                pass
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)

    # The terminal's interrupt is forwarded to this group. Claude handles it;
    # the guard keeps waiting so one Ctrl+C can cancel a turn normally.
    signal.signal(signal.SIGINT, lambda *_: None)
    threading.Thread(target=owner_exited, daemon=True).start()
    child = subprocess.Popen(sys.argv[2:])
    code = child.wait()
    sys.exit(code if code >= 0 else 128 - code)


if __name__ == '__main__':
    guard()
