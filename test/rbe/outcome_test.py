"""Check final JSON, printed verdict and exit status using retained native rows."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

private, evidence = map(Path, sys.argv[1:])
del sys.argv[1:]
scripts = Path(__file__).parent
fixtures = scripts / "outcome_fixtures"
environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(scripts), str(private)))}
child = """import json, sys
from pathlib import Path
from image_result import write_outcome
native = json.loads(Path(sys.argv[1]).read_text())
raise SystemExit(write_outcome(Path(sys.argv[2]), native, native['status'], sys.argv[3]))
"""
records = []


class OutcomeTest(unittest.TestCase):
    def test_retained_native_and_projection_verdicts(self) -> None:
        for reference in json.loads((fixtures / "sources.json").read_text()):
            source = fixtures / reference["file"]
            raw = source.read_bytes()
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()),
                             (reference["bytes"], reference["sha256"]))
            native = json.loads(raw)
            for projection in ("PASS", "FAIL", "INCOMPLETE"):
                with self.subTest(native=native["status"], projection=projection), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "outcome.json"
                    result = subprocess.run([sys.executable, "-c", child, str(source), str(output), projection],
                                            env=environment, capture_output=True, text=True, timeout=20)
                    expected = native["status"] if native["status"] != "PASS" else projection
                    self.assertEqual(result.returncode, int(expected != "PASS"), result.stderr)
                    written, printed = json.loads(output.read_text()), json.loads(result.stdout)
                    self.assertEqual(written, {**native, "status": expected, "native_status": native["status"],
                                               "projection_status": projection})
                    self.assertEqual(printed["status"], expected)
                    self.assertEqual(printed["native_status"], native["status"])
                    self.assertEqual(printed["projection_status"], projection)
                    self.assertEqual(printed["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
                    records.append({"native": native["status"], "projection": projection,
                                    "expected": expected, "exit": result.returncode, "printed": printed})

    def test_unknown_projection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outcome.json"
            result = subprocess.run([sys.executable, "-c", child, str(fixtures / "pass.json"), str(output), "unknown"],
                                    env=environment, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertIn("Unknown qualification verdicts", result.stderr)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OutcomeTest))
    (evidence / "outcome-controls.json").write_text(json.dumps({
        "status": "PASS" if result.wasSuccessful() else "FAIL", "records": records,
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
    }, indent=2) + "\n")
    raise SystemExit(not result.wasSuccessful())
