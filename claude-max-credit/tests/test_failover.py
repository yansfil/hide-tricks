"""Exercise HTTP and OS boundaries without charging an actual provider."""
import contextlib
import http.client
import http.server
import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from credit_proxy import CreditError, KeyPool, Proxy, load_keys, ENV_REGISTRY
from process_owner import OwnedProcess

ONE = 'sk-ant-test-' + 'a' * 32
TWO = 'sk-ant-test-' + 'b' * 32
THREE = 'sk-ant-test-' + 'c' * 32
ERROR = lambda kind, message: json.dumps({'type': 'error', 'error': {'type': kind, 'message': message}}).encode()
BALANCE = ERROR('invalid_request_error', 'Your credit balance is too low to access the Anthropic API.')
LIMIT = ERROR('rate_limit_error', 'Rate limit reached.')
SUCCESS = b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test"}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'


class Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, scenario):
        self.scenario, self.calls = scenario, []
        super().__init__(('127.0.0.1', 0), UpstreamHandler)


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        key = self.headers.get('x-api-key')
        self.server.calls.append((key, data, dict(self.headers)))
        status, payload, headers = self.server.scenario(key)
        if status == 'disconnect':
            self.connection.shutdown(2)
            return
        self.send_response(status)
        self.send_header('Content-Length', str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='credit-proxy-test-')
        self.path = pathlib.Path(self.temp.name) / 'keys.env'
        self.write(f'ANTHROPIC_API_KEY_1={ONE}\nANTHROPIC_API_KEY_2="{TWO}"\n')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def write(self, text):
        self.path.write_text(text)
        self.path.chmod(0o600)

    def proxy(self, scenario):
        upstream = Upstream(scenario)
        up_thread = threading.Thread(target=upstream.serve_forever)
        up_thread.start()
        def close_upstream():
            upstream.shutdown()
            upstream.server_close()
            up_thread.join(2)
        self.stack.callback(close_upstream)
        proxy = Proxy(KeyPool(self.path), 'local-test-token', ('127.0.0.1', upstream.server_port, False))
        thread = threading.Thread(target=proxy.serve_forever)
        thread.start()
        def close_proxy():
            proxy.close_owned()
            thread.join(2)
        self.stack.callback(close_proxy)
        return proxy, upstream

    def request(self, proxy, token='local-test-token'):
        client = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
        try:
            client.request('POST', '/v1/messages?beta=true', b'{"stream":true}', {
                'x-api-key': token, 'anthropic-beta': 'claude-code-20250219',
                'User-Agent': 'test-terminal-sdk',
            })
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_exhausted_key_switches_before_stream_and_stays_on_success(self):
        proxy, upstream = self.proxy(lambda key: (400, BALANCE, {}) if key == ONE else
                                     (200, SUCCESS, {'Content-Type': 'text/event-stream'}))
        self.assertEqual(self.request(proxy), (200, SUCCESS))
        self.assertEqual(self.request(proxy), (200, SUCCESS))
        self.assertEqual([call[0] for call in upstream.calls], [ONE, TWO, TWO])
        self.assertEqual(upstream.calls[1][1], b'{"stream":true}')
        self.assertEqual(upstream.calls[1][2]['anthropic-beta'], 'claude-code-20250219')
        self.assertNotIn('local-test-token', str(upstream.calls))

    def test_rate_limit_moves_to_next_key_and_honors_cooldown(self):
        proxy, upstream = self.proxy(lambda key: (429, LIMIT, {'Retry-After': '120'}) if key == ONE else
                                     (200, b'{"ok":true}', {}))
        self.assertEqual(self.request(proxy)[0], 200)
        self.assertGreater(proxy.pool.keys[0].blocked_until, time.time() + 110)
        self.assertEqual([call[0] for call in upstream.calls], [ONE, TWO])

    def test_auth_failure_disabled_and_replaced_key_is_read_live(self):
        proxy, upstream = self.proxy(lambda key: (401, ERROR('authentication_error', 'Invalid key'), {})
                                     if key == ONE else (200, b'{}', {}))
        self.assertEqual(self.request(proxy)[0], 200)
        self.assertTrue(proxy.pool.keys[0].disabled)
        self.write(f'ANTHROPIC_API_KEY_NEW={THREE}\n')
        self.assertEqual(self.request(proxy)[0], 200)
        self.assertEqual(upstream.calls[-1][0], THREE)

    def test_all_keys_exhausted_are_tried_once_without_infinite_retry(self):
        proxy, upstream = self.proxy(lambda _: (400, BALANCE, {}))
        self.assertEqual(self.request(proxy), (400, BALANCE))
        self.assertEqual(len(upstream.calls), 2)
        self.assertEqual(self.request(proxy)[0], 502)
        self.assertEqual(len(upstream.calls), 2)

    def test_provider_failure_and_ambiguous_disconnect_are_not_replayed(self):
        for response in [(500, ERROR('api_error', 'Server failure'), {}), ('disconnect', b'', {})]:
            with self.subTest(response=response[0]):
                proxy, upstream = self.proxy(lambda _, response=response: response)
                self.assertIn(self.request(proxy)[0], (500, 502))
                self.assertEqual(len(upstream.calls), 1)

    def test_first_stream_error_retries_but_partial_output_never_replays(self):
        first_error = b'event: error\ndata: ' + LIMIT + b'\n\n'
        proxy, upstream = self.proxy(lambda key: (200, first_error if key == ONE else SUCCESS,
                                                 {'Content-Type': 'text/event-stream'}))
        self.assertEqual(self.request(proxy), (200, SUCCESS))
        self.assertEqual(len(upstream.calls), 2)
        proxy2, upstream2 = self.proxy(lambda key: (200, SUCCESS + first_error if key == ONE else SUCCESS,
                                                   {'Content-Type': 'text/event-stream'}))
        self.assertEqual(self.request(proxy2), (200, SUCCESS + first_error))
        self.assertEqual(len(upstream2.calls), 1)
        self.assertEqual(self.request(proxy2), (200, SUCCESS))
        self.assertEqual([c[0] for c in upstream2.calls], [ONE, TWO])
        crlf = (SUCCESS + first_error).replace(b'\n', b'\r\n')
        proxy3, upstream3 = self.proxy(lambda key: (200, crlf if key == ONE else SUCCESS,
                                                   {'Content-Type': 'text/event-stream'}))
        self.assertEqual(self.request(proxy3), (200, crlf))
        self.assertEqual(self.request(proxy3), (200, SUCCESS))
        self.assertEqual([c[0] for c in upstream3.calls], [ONE, TWO])

    def test_unauthorized_client_cannot_use_provider_credentials(self):
        proxy, upstream = self.proxy(lambda _: (200, b'{}', {}))
        self.assertEqual(self.request(proxy, 'wrong-token')[0], 401)
        self.assertEqual(upstream.calls, [])

    def test_key_file_dedup_permissions_and_invalid_edits_fail_explicitly(self):
        self.write(f'export ANTHROPIC_API_KEY_1="{ONE}" # primary\nANTHROPIC_API_KEY_2={ONE}\n')
        self.assertEqual(len(load_keys(self.path)), 1)
        pool = KeyPool(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(CreditError):
            pool.choose(set())
        self.write('ANTHROPIC_API_KEY_1=$(touch /tmp/should-not-execute)\n')
        with self.assertRaises(CreditError):
            load_keys(self.path)

    def test_environment_registry_matches_example(self):
        example = pathlib.Path(__file__).parents[1] / '.env.example'
        names = {line.lstrip('# ').split('=', 1)[0] for line in example.read_text().splitlines() if '=' in line}
        self.assertEqual(names, set(ENV_REGISTRY))

    def test_close_interrupts_an_active_upstream_read_and_client_connection(self):
        received, release = threading.Event(), threading.Event()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        def stall():
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                received.set()
                release.wait(4)
        stalled = threading.Thread(target=stall)
        stalled.start()
        proxy = Proxy(KeyPool(self.path), 'local-test-token',
                      ('127.0.0.1', listener.getsockname()[1], False))
        serving = threading.Thread(target=proxy.serve_forever)
        serving.start()
        result = []
        def call():
            try:
                result.append(self.request(proxy))
            except (OSError, http.client.HTTPException) as error:
                result.append(type(error).__name__)
        client = threading.Thread(target=call)
        client.start()
        closed = False
        try:
            self.assertTrue(received.wait(2))
            started = time.monotonic()
            proxy.close_owned()
            closed = True
            self.assertLess(time.monotonic() - started, 2)
            client.join(2)
            serving.join(2)
            self.assertFalse(client.is_alive())
            self.assertFalse(serving.is_alive())
            self.assertTrue(result)
            self.assertEqual(proxy.live_sockets, set())
        finally:
            release.set()
            if not closed:
                proxy.close_owned()
            client.join(3)
            serving.join(3)
            stalled.join(3)
            listener.close()

    def test_guard_terminates_child_and_grandchild_when_owner_is_killed(self):
        info = pathlib.Path(self.temp.name) / 'pids.json'
        child = pathlib.Path(self.temp.name) / 'child.py'
        child.write_text('import subprocess,sys,os,json,time\n'
                         'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                         f'open({str(info)!r},"w").write(json.dumps([os.getpid(),p.pid]))\n'
                         'time.sleep(60)\n')
        owner = pathlib.Path(self.temp.name) / 'owner.py'
        source = str(pathlib.Path(__file__).resolve().parents[1])
        owner.write_text(f'import sys,time;sys.path.insert(0,{source!r})\n'
                         'from process_owner import OwnedProcess\n'
                         f'p=OwnedProcess([sys.executable,{str(child)!r}])\n'
                         'time.sleep(60)\n')
        process = subprocess.Popen([sys.executable, str(owner)])
        pids = []
        try:
            deadline = time.monotonic() + 5
            while not info.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(info.exists())
            pids = json.loads(info.read_text())
            process.kill()
            process.wait(2)
            deadline = time.monotonic() + 5
            def alive(pid):
                try:
                    os.kill(pid, 0)
                    return True
                except ProcessLookupError:
                    return False
            while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(any(alive(pid) for pid in pids))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pid in pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    def test_close_waits_for_native_process_to_finish_flushing_files(self):
        ready = pathlib.Path(self.temp.name) / 'ready'
        flushed = pathlib.Path(self.temp.name) / 'flushed'
        child = pathlib.Path(self.temp.name) / 'flush.py'
        child.write_text('import signal,time,sys,pathlib\n'
                         'def finish(*args):\n'
                         '    time.sleep(0.2)\n'
                         f'    pathlib.Path({str(flushed)!r}).write_text("done")\n'
                         '    sys.exit(0)\n'
                         'signal.signal(signal.SIGTERM, finish)\n'
                         f'pathlib.Path({str(ready)!r}).touch()\n'
                         'time.sleep(30)\n')
        owner = OwnedProcess([sys.executable, str(child)])
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
        finally:
            owner.close()
        self.assertTrue(flushed.exists())
        self.assertFalse(owner.group_alive())


if __name__ == '__main__':
    unittest.main()
