#!/usr/bin/env python3
"""Original terminal launcher with bounded, rejection-only API key failover."""
import contextlib
import dataclasses
import email.utils
import hmac
import http.client
import http.server
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse

from process_owner import OwnedProcess
from diagnostic_log import emit, configure as configure_logging

MAX_KEYS = 64
MAX_KEYS_FILE = 65536
MAX_BODY = 16 * 1024 * 1024
MAX_ERROR = 65536
MAX_RESPONSE = 64 * 1024 * 1024
MAX_IN_FLIGHT = 8
REQUEST_SECONDS = 300
SESSION_SECONDS = 24 * 60 * 60
CONNECT_SECONDS = 10
BALANCE_COOLDOWN = 3600
KEY_PATTERN = re.compile(r'sk-ant-[A-Za-z0-9_-]{16,500}')
NAME_PATTERN = re.compile(r'ANTHROPIC_API_KEY(?:_[A-Za-z0-9_]+)?')
DEFAULT_KEYS = pathlib.Path.home() / '.config/claude-api-credit/keys.env'
STATE = pathlib.Path.home() / '.local/state/claude-api-credit'

# Runtime-readable environment contract. Other variables pass through to Claude.
ENV_REGISTRY = {
    'CLAUDE_CREDIT_KEYS_FILE': {
        'required': False, 'shape': 'absolute path', 'fallback': str(DEFAULT_KEYS),
        'note': 'Selects the private key file; defaults to keys.env.',
    },
    'CLAUDE_CREDIT_CLAUDE': {
        'required': False, 'shape': 'absolute path',
        'fallback': '`claude` on PATH, else ~/.local/bin/claude',
        'note': 'Selects the Claude Code executable the launcher starts.',
    },
}
CHILD_FIXED = {'CLAUDE_CODE_ENTRYPOINT': 'sdk-ts'}
CHILD_REMOVED = (
    'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY',
)


class CreditError(Exception):
    pass


def configured_keys():
    raw = os.environ.get('CLAUDE_CREDIT_KEYS_FILE', ENV_REGISTRY['CLAUDE_CREDIT_KEYS_FILE']['fallback'])
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        raise CreditError('CLAUDE_CREDIT_KEYS_FILE must be an absolute path.')
    return path


def claude_executable():
    raw = os.environ.get('CLAUDE_CREDIT_CLAUDE')
    if raw:
        path = pathlib.Path(raw).expanduser()
        if not path.is_absolute():
            raise CreditError('CLAUDE_CREDIT_CLAUDE must be an absolute path.')
        return path
    found = shutil.which('claude')
    path = pathlib.Path(found) if found else pathlib.Path.home() / '.local/bin/claude'
    if not os.access(path, os.X_OK):
        raise CreditError('Claude Code not found: install it or set CLAUDE_CREDIT_CLAUDE.')
    return path


def local_token(path):
    """The key Claude sees for the local proxy, kept so Claude asks to use it once per account."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with path.open('rb') as source:
            info = os.fstat(source.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CreditError(f'Local token must be owned by you with mode 600: chmod 600 "{path}"') from None
            token = source.read(256).decode('ascii', 'replace').strip()
        if not KEY_PATTERN.fullmatch(token):
            raise CreditError(f'Local token is malformed; delete it to make a new one: {path}')
        return token
    token = 'sk-ant-proxy-' + secrets.token_urlsafe(32)
    with os.fdopen(fd, 'w') as target:
        target.write(token + '\n')
    return token


def load_keys(path):
    try:
        with path.open('rb') as source:
            info = os.fstat(source.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CreditError(f'Key file must be owned by you with mode 600: chmod 600 "{path}"')
            raw = source.read(MAX_KEYS_FILE + 1)
    except FileNotFoundError:
        raise CreditError(f'Create your private key file: {path}') from None
    if len(raw) > MAX_KEYS_FILE:
        raise CreditError('Key file exceeds 64 KiB.')
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeDecodeError:
        raise CreditError('Key file must be UTF-8.') from None
    entries, names, values = [], set(), set()
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, sep, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not sep or not NAME_PATTERN.fullmatch(name) or name in names:
            raise CreditError(f'Invalid or duplicate key variable on line {number}.')
        names.add(name)
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith('#')):
                raise CreditError(f'Invalid quoted key on line {number}.')
            value = value[1:end]
        else:
            value = value.split('#', 1)[0].strip()
        if not value:
            continue
        if not KEY_PATTERN.fullmatch(value):
            raise CreditError(f'Invalid Anthropic key format on line {number}.')
        if value in values:
            continue
        values.add(value)
        entries.append(Key(name, value))
        if len(entries) > MAX_KEYS:
            raise CreditError(f'At most {MAX_KEYS} distinct keys are supported.')
    if not entries:
        raise CreditError(f'Add at least one API key to {path}')
    return entries


@dataclasses.dataclass
class Key:
    name: str
    value: str = dataclasses.field(repr=False)
    blocked_until: float = 0
    disabled: bool = False


class KeyPool:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.keys = []
        self.active = None
        self.refresh()

    def refresh(self):
        # Re-read on requests so an editor's atomic save becomes visible without
        # restarting the terminal. Never retain a silently invalid edited file.
        with self.lock:
            previous = {key.value: key for key in self.keys}
            entries = load_keys(self.path)
            self.keys = [previous.get(key.value, key) for key in entries]
            for old, fresh in zip(self.keys, entries):
                old.name = fresh.name
            if self.active not in self.keys:
                self.active = self.keys[0]

    def choose(self, attempted):
        with self.lock:
            self.refresh()
            start = self.keys.index(self.active)
            for offset in range(len(self.keys)):
                key = self.keys[(start + offset) % len(self.keys)]
                if key.value not in attempted and not key.disabled and key.blocked_until <= time.time():
                    self.active = key
                    return key
            raise CreditError('No available key. Add a key or check balances and rate limits in keys.env.')

    def reject(self, key, reason, cooldown):
        with self.lock:
            key.disabled = reason == 'authentication'
            key.blocked_until = time.time() + cooldown


def retry_delay(headers):
    value = headers.get('retry-after', '')
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 60
    if not (seconds >= 0):
        seconds = 60
    return min(3600, max(1, seconds))


def rejection(status, data, headers):
    """Only provider-declared rejection permits replay on a different key."""
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        obj = {}
    error = obj.get('error', {}) if isinstance(obj, dict) else {}
    if not isinstance(error, dict):
        error = {}
    kind, message = error.get('type'), error.get('message', '')
    if status == 401:
        return 'authentication', BALANCE_COOLDOWN
    if status == 429 or kind == 'rate_limit_error':
        return 'rate_limit', retry_delay(headers)
    # Anthropic reports exhausted credit as a 400 invalid_request_error.
    # Keep this text adaptation at the provider boundary, never inspect TUI text.
    if status == 402 or (status == 400 and isinstance(message, str) and
            re.search(r'credit balance (?:is )?too low|insufficient credits?', message, re.I)):
        return 'balance', BALANCE_COOLDOWN
    return None


class Proxy(http.server.ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = MAX_IN_FLIGHT

    def __init__(self, pool, token, upstream=('api.anthropic.com', 443, True), *, request_seconds=REQUEST_SECONDS):
        self.pool, self.token, self.upstream = pool, token, upstream
        self.request_seconds = request_seconds
        self.slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self.socket_lock = threading.Lock()
        self.live_sockets = set()
        self.stopping = threading.Event()
        self.tls = ssl.create_default_context()
        super().__init__(('127.0.0.1', 0), Handler)

    def process_request(self, request, address):
        request.settimeout(15)
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        with self.socket_lock:
            self.live_sockets.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            self.release_socket(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.release_socket(request)
            self.slots.release()

    def release_socket(self, sock):
        with self.socket_lock:
            self.live_sockets.discard(sock)

    @contextlib.contextmanager
    def upstream_connection(self, timeout=CONNECT_SECONDS):
        host, port, secure = self.upstream
        connection = (http.client.HTTPSConnection(host, port, timeout=timeout, context=self.tls)
                      if secure else http.client.HTTPConnection(host, port, timeout=timeout))
        sock = None
        try:
            connection.connect()
            sock = connection.sock
            with self.socket_lock:
                if self.stopping.is_set():
                    raise CreditError('Proxy is stopping.')
                self.live_sockets.add(sock)
            yield connection
        finally:
            if sock is not None:
                self.release_socket(sock)
            connection.close()

    def handle_error(self, request, address):
        emit('proxy.handler_failed')

    def close_owned(self):
        self.stopping.set()
        self.shutdown()
        with self.socket_lock:
            sockets = list(self.live_sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        self.server_close()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def reply(self, status, data, content_type='application/json', extra=None):
        self.client_status = status
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.response_bytes += len(data)
        self.close_connection = True

    def fail(self, status, message):
        self.reply(status, json.dumps({'type': 'error', 'error': {
            'type': 'api_error', 'message': message}}).encode())

    def read_limited(self, response, limit):
        output = bytearray()
        while True:
            self.deadline_timeout(response)
            chunk = response.read1(min(8192, limit + 1 - len(output)))
            if not chunk:
                self.deadline_timeout(response)
                return bytes(output)
            output.extend(chunk)
            if len(output) > limit:
                raise CreditError('Upstream response exceeded its byte limit.')

    def deadline_timeout(self, response):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.timed_out.is_set():
            self.timed_out.set()
            raise TimeoutError()
        if self.server.stopping.is_set():
            raise CreditError('Proxy stopped.')
        if self.upstream_socket.fileno() >= 0:
            self.upstream_socket.settimeout(remaining)

    def expire_request(self):
        self.timed_out.set()
        sock = getattr(self, 'upstream_socket', None)
        if self.phase == 'request_body':
            sock = self.connection
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    @staticmethod
    def frame_error(frame):
        for line in frame.splitlines():
            if line.startswith(b'data:'):
                data = line[5:].strip()
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get('type') == 'error':
                    return data
        return None

    def first_frame(self, response):
        frame = bytearray()
        while True:
            self.deadline_timeout(response)
            line = response.readline(MAX_ERROR + 1 - len(frame))
            frame.extend(line)
            if len(frame) > MAX_ERROR:
                raise CreditError('Upstream SSE frame exceeded its byte limit.')
            if not line or line in (b'\n', b'\r\n'):
                self.deadline_timeout(response)
                return bytes(frame)

    def stream(self, response, initial, key):
        self.client_status = response.status
        self.send_response(response.status)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.started = True
        count, pending = 0, b''
        chunk = initial
        while chunk:
            count += len(chunk)
            if count > MAX_RESPONSE:
                raise CreditError('Streaming response exceeded its byte limit.')
            self.wfile.write(chunk)
            self.wfile.flush()
            self.response_bytes += len(chunk)
            pending += chunk
            while separator := re.search(rb'\r?\n\r?\n', pending):
                frame, pending = pending[:separator.start()], pending[separator.end():]
                error = self.frame_error(frame)
                decision = rejection(200, error, response.headers) if error else None
                if decision:
                    self.server.pool.reject(key, *decision)
                    emit('key.rejected_after_stream_start', request_id=self.request_id,
                         key=key.name, reason=decision[0])
                    # Already delivered output is never replayed automatically.
            if len(pending) > MAX_ERROR:
                raise CreditError('Streaming SSE frame exceeded its byte limit.')
            self.deadline_timeout(response)
            chunk = response.read1(8192)
        self.deadline_timeout(response)
        self.close_connection = True

    def dispatch(self):
        self.started = False
        begin = time.monotonic()
        self.deadline = begin + self.server.request_seconds
        self.connection.settimeout(15)
        request_id = secrets.token_hex(6)
        self.request_id = request_id
        self.phase = 'validate_request'
        self.timed_out = threading.Event()
        self.response_bytes = 0
        self.client_status = self.upstream_status = self.key_name = None
        self.attempts = 0
        timer = None
        try:
            supplied = self.headers.get('x-api-key', '')
            if not supplied:
                supplied = self.headers.get('authorization', '').removeprefix('Bearer ')
            if not hmac.compare_digest(supplied.encode('utf-8'), self.server.token.encode('utf-8')):
                self.fail(401, 'Local proxy authentication failed.')
                return
            path = urllib.parse.urlsplit(self.path)
            allowed = (self.command == 'GET' and path.path == '/v1/models') or (
                self.command == 'POST' and path.path in ('/v1/messages', '/v1/messages/count_tokens'))
            if path.scheme or path.netloc or not allowed:
                self.fail(404, 'Unsupported API path.')
                return
            if self.headers.get('Transfer-Encoding'):
                self.fail(400, 'Chunked request bodies are not supported.')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.fail(400, 'Invalid Content-Length.')
                return
            if not 0 <= length <= MAX_BODY:
                self.fail(413, 'Request body exceeds 16 MiB.')
                return
            self.phase = 'request_body'
            timer = threading.Timer(max(0, self.deadline - time.monotonic()), self.expire_request)
            timer.start()
            body = self.rfile.read(length)
            if len(body) != length:
                self.fail(400, 'Incomplete request body.')
                return
            headers = {name: value for name, value in self.headers.items()
                       if name.lower() not in ('host', 'x-api-key', 'authorization', 'content-length',
                                               'connection', 'transfer-encoding', 'accept-encoding',
                                               'proxy-authorization', 'proxy-connection')}
            headers['Accept-Encoding'] = 'identity'
            attempted, last = set(), None
            for _ in range(MAX_KEYS):
                try:
                    key = self.server.pool.choose(attempted)
                except CreditError:
                    if last is not None:
                        status, data, content_type, extra = last
                        self.reply(status, data, content_type, extra)
                        return
                    raise
                attempted.add(key.value)
                self.key_name, self.attempts = key.name, len(attempted)
                headers['x-api-key'] = key.value
                self.phase = 'connect'
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                with self.server.upstream_connection(min(CONNECT_SECONDS, remaining)) as connection:
                    self.upstream_socket = connection.sock
                    self.deadline_timeout(None)
                    self.phase = 'send_request'
                    connection.request(self.command, self.path, body=body, headers=headers)
                    self.phase = 'response_headers'
                    response = connection.getresponse()
                    self.upstream_status = response.status
                    emit('upstream.response', request_id=request_id, key=key.name, status=response.status,
                         elapsed_ms=round((time.monotonic() - begin) * 1000), attempt=len(attempted))
                    content_type = response.getheader('Content-Type', 'application/json')
                    extra = {name: response.getheader(name) for name in ('retry-after', 'request-id')
                             if response.getheader(name) is not None}
                    if response.status >= 400:
                        self.phase = 'error_body'
                        data = self.read_limited(response, MAX_ERROR)
                        decision = rejection(response.status, data, response.headers)
                        if decision:
                            self.server.pool.reject(key, *decision)
                            emit('key.rejected', request_id=request_id, key=key.name, reason=decision[0], attempts=len(attempted))
                            last = response.status, data, content_type, extra
                            continue
                        self.reply(response.status, data, content_type, extra)
                        return
                    if 'text/event-stream' in content_type:
                        self.phase = 'first_frame'
                        initial = self.first_frame(response)
                        error = self.frame_error(initial)
                        decision = rejection(200, error, response.headers) if error else None
                        if decision:
                            self.server.pool.reject(key, *decision)
                            emit('key.rejected', request_id=request_id, key=key.name, reason=decision[0], attempts=len(attempted))
                            # No output has reached Claude, so this rejection is safe to retry.
                            last = 429 if decision[0] == 'rate_limit' else 400, error, 'application/json', extra
                            continue
                        self.phase = 'stream'
                        self.stream(response, initial, key)
                    else:
                        self.phase = 'response_body'
                        self.reply(response.status, self.read_limited(response, MAX_BODY), content_type, extra)
                    return
            if last is not None:
                self.reply(*last)
                return
            raise CreditError('Key retry limit reached.')
        except (CreditError, OSError, http.client.HTTPException) as error:
            deadline_reached = self.timed_out.is_set() or time.monotonic() >= self.deadline
            timeout = isinstance(error, TimeoutError) or deadline_reached
            emit('proxy.request_failed', request_id=request_id, kind=type(error).__name__,
                 phase=self.phase, key=self.key_name, upstream_status=self.upstream_status,
                 elapsed_ms=round((time.monotonic() - begin) * 1000), stream_started=self.started,
                 reason='request_deadline' if deadline_reached else
                        'transport_timeout' if timeout else 'request_failed')
            if not self.started:
                with contextlib.suppress(OSError):
                    self.fail(504 if timeout else 502,
                              'Upstream timed out. This request was not replayed. Check proxy.jsonl.' if timeout else
                              str(error) if isinstance(error, CreditError) else
                              'Upstream connection failed. This request was not replayed.')
            self.close_connection = True
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            emit('proxy.request_finished', request_id=request_id, phase=self.phase, key=self.key_name,
                 client_status=self.client_status, upstream_status=self.upstream_status,
                 elapsed_ms=round((time.monotonic() - begin) * 1000),
                 attempts=self.attempts, response_bytes=self.response_bytes, stream_started=self.started)


def run_terminal(pool, arguments, cli=None, upstream=('api.anthropic.com', 443, True), token=None):
    cli = cli or claude_executable()
    token = token or 'sk-ant-proxy-' + secrets.token_urlsafe(32)
    server = Proxy(pool, token, upstream)
    thread = threading.Thread(target=server.serve_forever, name='credit-proxy')
    thread.start()
    child = None
    handlers = {}
    terminate = threading.Event()
    try:
        emit('proxy.started', port=server.server_port, request_seconds=server.request_seconds)
        env = dict(os.environ)
        for name in CHILD_REMOVED:
            env.pop(name, None)
        env.update(CHILD_FIXED)
        env['ANTHROPIC_API_KEY'] = token
        env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
        child = OwnedProcess([str(cli), *arguments], env=env)
        # Claude runs in a session of its own, so the terminal's interrupt and
        # window-size signals reach only this launcher and are passed on.
        def forward(sig, _):
            child.send(sig)
            if sig not in (signal.SIGINT, signal.SIGWINCH):
                terminate.set()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
            handlers[sig] = signal.signal(sig, forward)
        expires = time.monotonic() + SESSION_SECONDS
        while child.process.poll() is None:
            if terminate.wait(0.2):
                return 128 + signal.SIGTERM
            if time.monotonic() >= expires:
                raise CreditError('Session reached its 24-hour lifetime limit. Restart the launcher.')
        return child.process.returncode
    finally:
        try:
            if child is not None:
                child.close()
        finally:
            try:
                server.close_owned()
                thread.join(timeout=12)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments in (['--version'], ['--help']):
        claude = str(claude_executable())
        os.execv(claude, [claude, *arguments])
    path = configured_keys()
    if arguments == ['keys', 'path']:
        print(path)
        return 0
    if arguments == ['keys', 'list'] or arguments == ['keys', 'check']:
        entries = load_keys(path)
        for key in entries:
            print(key.name)
        print(f'{len(entries)} distinct key(s); values hidden.')
        return 0
    if arguments == ['--credit-help']:
        print(f'Edit {path} (mode 600), then run claude-api-credit [Claude arguments].\n'
              'keys path | keys list | keys check\n'
              '401, exhausted credit, or 429: switch keys before any streamed output.\n'
              'Edits reload on the next request. Successful requests keep the active key.')
        return 0
    pool = KeyPool(path)
    log_path = configure_logging(STATE)
    print(f'claude-api-credit diagnostics: {log_path}', file=sys.stderr, flush=True)
    return run_terminal(pool, arguments, token=local_token(STATE / 'local-token'))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (CreditError, OSError) as error:
        print(f'claude-api-credit: {error}', file=sys.stderr)
        sys.exit(1)
