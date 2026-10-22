"""Guard against successful wrappers that hide missing Linux selftest results."""
import unittest

from pidfd_tap import reconcile


PROGRAMS = ["pidfd_open_test", "pidfd_wait"]
COMPLETE = """    integration_runtime_test.go:328: docker run succeeded; output: TAP version 13
1..2
# TAP version 13
# 1..2
# ok 1 test_pidfd_open
# ok 2 unsupported_case # SKIP unavailable feature
ok 1 selftests: pidfd: pidfd_open_test
# ok 1 test_pidfd_wait
ok 2 selftests: pidfd: pidfd_wait
--- PASS: TestPIDFDSelftests (0.02s)
"""


class ReconcileTest(unittest.TestCase):
    def test_complete_retains_child_skip(self):
        result = reconcile(COMPLETE, PROGRAMS)
        self.assertEqual(len(result["executed_programs"]), 2)
        self.assertEqual(result["nested_skip_lines"],
                         ["# ok 2 unsupported_case # SKIP unavailable feature"])

    def test_child_skip_all_plan_is_retained(self):
        result = reconcile(COMPLETE.replace("# ok 1 test_pidfd_wait", "# 1..0 # SKIP unavailable feature"), PROGRAMS)
        self.assertIn("# 1..0 # SKIP unavailable feature", result["nested_skip_lines"])

    def test_failed_incomplete_and_empty_streams(self):
        streams = {
            "wrapper_without_test_output": "--- PASS: TestPIDFDSelftests (0.02s)",
            "empty_plan": "TAP version 13\n1..0\n",
            "truncated": COMPLETE.partition("ok 2 selftests:")[0],
            "wrong_program": COMPLETE.replace("selftests: pidfd: pidfd_wait", "selftests: pidfd: unexpected"),
            "duplicate_program": COMPLETE.replace("ok 2 selftests: pidfd: pidfd_wait", "ok 1 selftests: pidfd: pidfd_open_test"),
            "outer_failure": COMPLETE.replace("ok 2 selftests:", "not ok 2 selftests:"),
            "child_failure": COMPLETE.replace("# ok 1 test_pidfd_wait", "# not ok 1 test_pidfd_wait"),
            "outer_skip": COMPLETE.replace("pidfd: pidfd_wait\n", "pidfd: pidfd_wait # SKIP\n"),
            "timeout": COMPLETE.replace("ok 2 selftests: pidfd: pidfd_wait", "not ok 2 selftests: pidfd: pidfd_wait # TIMEOUT 45 seconds"),
            "bailout": COMPLETE.replace("# ok 1 test_pidfd_wait", "# Bail out! fixture failed"),
            "duplicate_stream": COMPLETE + COMPLETE,
        }
        for name, stream in streams.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                reconcile(stream, PROGRAMS)


if __name__ == "__main__":
    unittest.main()
