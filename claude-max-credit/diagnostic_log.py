"""A bounded JSON journal shared safely by concurrent launcher processes."""
import datetime
import fcntl
import json
import os
import pathlib
import sys
import threading

MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2
_lock = threading.Lock()
_directory = None


def configure(directory=None):
    global _directory
    directory = pathlib.Path(directory) if directory else pathlib.Path.home() / '.local/state/claude-api-credit'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    _directory = directory
    emit('proxy.logging_ready')
    return directory / 'proxy.jsonl'


def emit(event, **fields):
    # Callers supply metadata only. Never include exceptions, bodies, or headers.
    record = json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         'pid': os.getpid(), 'event': event, **fields}) + '\n'
    if _directory is None:
        print(record, end='', file=sys.stderr, flush=True)
        return
    with _lock:
        lock_fd = os.open(_directory / '.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            path = _directory / 'proxy.jsonl'
            if path.exists() and path.stat().st_size + len(record.encode()) > MAX_LOG_BYTES:
                for index in range(LOG_BACKUPS, 0, -1):
                    source = path if index == 1 else path.with_name(f'proxy.jsonl.{index - 1}')
                    if source.exists():
                        source.replace(path.with_name(f'proxy.jsonl.{index}'))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as target:
                target.write(record)
        finally:
            os.close(lock_fd)
