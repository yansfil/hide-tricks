"""Run the installed native terminal against a local provider fixture only."""
import contextlib
import fcntl
import json
import os
import pathlib
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from test_failover import Upstream, ONE, TWO, BALANCE
from credit_proxy import claude_executable, configured_keys

SOURCE = pathlib.Path(__file__).resolve().parents[1]
NATIVE = claude_executable()


def response(answer):
    events = [
        {'type': 'message_start', 'message': {'id': 'msg_local_fixture', 'type': 'message',
         'role': 'assistant', 'model': 'claude-haiku-5-5', 'content': [], 'stop_reason': None,
         'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': answer}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
         'usage': {'output_tokens': 3}},
        {'type': 'message_stop'},
    ]
    return ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()


def main(live=False):
    turns = []
    def scenario(key):
        if key == ONE:
            return 400, BALANCE, {'Content-Type': 'application/json'}
        answer = '437' if not turns else '442'
        turns.append(answer)
        return 200, response(answer), {'Content-Type': 'text/event-stream'}

    upstream = None if live else Upstream(scenario)
    thread = None if live else threading.Thread(target=upstream.serve_forever)
    if thread:
        thread.start()
    provider = ('api.anthropic.com', 443, True) if live else ('127.0.0.1', upstream.server_port, False)
    live_keys = configured_keys()
    live_logs = pathlib.Path.home() / '.local/state/claude-api-credit'
    process = None
    master = slave = None
    with tempfile.TemporaryDirectory(prefix='credit-native-smoke-') as directory:
        temp = pathlib.Path(directory)
        try:
            work = temp / 'work'
            config = temp / 'config'
            work.mkdir()
            config.mkdir()
            (config / '.claude.json').write_text(json.dumps({
                'theme': 'dark', 'hasCompletedOnboarding': True,
                'projects': {str(work.resolve()): {'hasTrustDialogAccepted': True}},
            }))
            keys = temp / 'keys.env'
            keys.write_text(f'ANTHROPIC_API_KEY_1={ONE}\nANTHROPIC_API_KEY_2={TWO}\n')
            keys.chmod(0o600)
            if live:
                keys = live_keys
            entry = temp / 'entry.py'
            args = ['--model', 'haiku', '--tools', '', '--permission-mode', 'default',
                    '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--',
                    'Compute 19 * 23. Respond with only the result.']
            entry.write_text(
                f'import sys,pathlib;sys.path.insert(0,{str(SOURCE)!r})\n'
                'from credit_proxy import KeyPool,run_terminal\n'
                + (f'from diagnostic_log import configure;configure({str(live_logs)!r})\n' if live else '') +
                f'sys.exit(run_terminal(KeyPool(pathlib.Path({str(keys)!r})),{args!r},'
                f'cli=pathlib.Path({str(NATIVE)!r}),upstream={provider!r}))\n')
            env = dict(os.environ, HOME=str(temp), CLAUDE_CONFIG_DIR=str(config),
                       DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', TERM='xterm-256color')
            env.pop('CLAUDECODE', None)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
            process = subprocess.Popen([sys.executable, str(entry)], env=env, cwd=work,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            output = bytearray()
            confirmed = second = False
            deadline = time.monotonic() + (210 if live else 45)
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        block = os.read(master, 16384)
                    except OSError:
                        break
                    if not block:
                        break
                    output.extend(block)
                    if len(output) > 256 * 1024:
                        raise RuntimeError('Terminal output cap reached.')
                text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output.decode(errors='replace'))
                flat = re.sub(r'\s+', '', text).lower()
                if not confirmed and 'doyouwanttousethisapikey' in flat:
                    os.write(master, b'\x1b[A')
                    time.sleep(0.2)
                    os.write(master, b'\r')
                    confirmed = True
                if not second and '437' in text:
                    time.sleep(0.5)
                    os.write(master, b'Now add 5 to that result. Respond with only the result.')
                    time.sleep(0.6)
                    os.write(master, b'\r')
                    second = True
                if second and '442' in text:
                    if not live and [call[0] for call in upstream.calls] != [ONE, TWO, TWO]:
                        raise RuntimeError('Unexpected provider key sequence.')
                    print(json.dumps({'native_terminal': 'PASS', 'first_answer': '437',
                                      'second_answer': '442',
                                      'keys_used': 'see diagnostic log' if live else ['1 rejected', '2 success', '2 success'],
                                      'provider': 'real API' if live else 'local fixture; no real credits charged'}))
                    return
                if process.poll() is not None:
                    break
            # Contains dummy credentials only; useful for diagnosing onboarding changes.
            raise RuntimeError('Native terminal did not complete both turns.\n' +
                               ('Inspect the private diagnostic log.' if live else text[-12000:]))
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(2)
                else:
                    process.wait()
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
            if upstream:
                upstream.shutdown()
                upstream.server_close()
                thread.join(2)


if __name__ == '__main__':
    main(live=sys.argv[1:] == ['--live'])
