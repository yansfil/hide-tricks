"""Provider response latency must not be mistaken for a rejected API key."""
import contextlib
import http.client
import http.server
import io
import pathlib
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from credit_proxy import KeyPool, Proxy
from test_failover import ONE, SUCCESS


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='credit-latency-'))
        self.path = pathlib.Path(self.directory) / 'keys.env'
        self.path.write_text(f'ANTHROPIC_API_KEY_1={ONE}\n')
        self.path.chmod(0o600)
        self.logs = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.stack.close()

    def start(self, before_headers=0, stream_pause=0, request_seconds=300, trickle_headers=False):
        release = self.release
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                release.wait(before_headers)
                first = b'event: ping\ndata: {"type":"ping"}\n\n'
                body = first + SUCCESS
                try:
                    if trickle_headers:
                        for byte in b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n':
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            release.wait(0.05)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(first)
                    self.wfile.flush()
                    release.wait(stream_pause)
                    self.wfile.write(SUCCESS)
                    self.wfile.flush()
                except (OSError, BrokenPipeError):
                    pass
        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=upstream.serve_forever)
        thread.start()
        self.stack.callback(thread.join, 3)
        self.stack.callback(upstream.server_close)
        self.stack.callback(upstream.shutdown)
        proxy = Proxy(KeyPool(self.path), 'local-latency-token', ('127.0.0.1', upstream.server_port, False),
                      request_seconds=request_seconds)
        serving = threading.Thread(target=proxy.serve_forever)
        serving.start()
        self.stack.callback(serving.join, 3)
        self.stack.callback(proxy.close_owned)
        return proxy

    def request(self, proxy):
        connection = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=25)
        try:
            connection.request('POST', '/v1/messages', b'{"stream":true}', {'x-api-key': 'local-latency-token'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_headers_after_eleven_seconds_complete_instead_of_502(self):
        proxy = self.start(before_headers=11)
        status, body = self.request(proxy)
        self.assertEqual(status, 200, self.logs.getvalue())
        self.assertIn(SUCCESS, body)

    def test_sixteen_second_stream_pause_preserves_complete_response(self):
        proxy = self.start(stream_pause=16)
        status, body = self.request(proxy)
        self.assertEqual(status, 200)
        self.assertIn(SUCCESS, body, self.logs.getvalue())

    def test_total_deadline_stops_even_continuous_header_bytes_without_replay(self):
        proxy = self.start(request_seconds=0.25, trickle_headers=True)
        started = time.monotonic()
        status, _ = self.request(proxy)
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('request_deadline', self.logs.getvalue())
        self.assertIn('response_headers', self.logs.getvalue())

    def test_deadline_closes_started_stream_and_records_its_phase(self):
        proxy = self.start(request_seconds=0.25, stream_pause=3)
        started = time.monotonic()
        status, body = self.request(proxy)
        self.assertEqual(status, 200)
        self.assertNotIn(SUCCESS, body)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('request_deadline', self.logs.getvalue())
        self.assertIn('"phase": "stream"', self.logs.getvalue())


if __name__ == '__main__':
    unittest.main()
