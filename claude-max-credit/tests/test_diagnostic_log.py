"""Several live terminals must retain readable, bounded diagnostics together."""
import concurrent.futures
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


class DiagnosticTests(unittest.TestCase):
    def test_four_processes_preserve_all_records_during_rotation(self):
        source = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='credit-diagnostics-') as directory:
            path = pathlib.Path(directory)
            journal = path / 'proxy.jsonl'
            journal.write_text('{"old":true}\n' * 180000)
            journal.chmod(0o600)
            def writer(worker):
                script = (f'import sys;sys.path.insert(0,{str(source)!r});'
                          'from diagnostic_log import configure,emit;'
                          f'configure({directory!r});'
                          f'[emit("test.record",worker={worker},sequence=i) for i in range(50)]')
                return subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=10)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(writer, range(4)))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            actual = {(r['worker'], r['sequence']) for r in records if r['event'] == 'test.record'}
            self.assertEqual(actual, {(worker, seq) for worker in range(4) for seq in range(50)})
            self.assertTrue((path / 'proxy.jsonl.1').exists())
            self.assertLessEqual(sum(p.stat().st_size for p in path.glob('proxy.jsonl*')), 6 * 1024 * 1024)
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
