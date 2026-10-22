"""Regression cases for the Rust selected-input evidence boundary."""
import json
import unittest

from image_workload import native_trace_lines

PATH = "bazel-out/k8-opt/bin/external/rust_tools/lib/rustlib/x86_64-unknown-linux-musl/lib/self-contained/libc.a"


def diagnostic(message: str, code: str = "linker_messages") -> str:
    return json.dumps({"code": {"code": code}, "level": "warning", "message": message})


class TraceTest(unittest.TestCase):
    def test_actual_archive_member(self):
        line = PATH + "(memcpy.o)"
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + line)), [{"path": PATH, "line": line}])

    def test_prospective_command_is_not_trace(self):
        self.assertEqual(native_trace_lines('"rust-lld" "' + PATH + '"'), [])

    def test_unrelated_diagnostic_is_not_trace(self):
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + PATH, "unused")), [])

    def test_prospective_command_inside_diagnostic_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported native link trace path"):
            native_trace_lines(diagnostic('linker stdout: "rust-lld" "' + PATH + '"'))

    def test_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Noncanonical native link trace path"):
            native_trace_lines(diagnostic("linker stdout: " + PATH.replace("/bin/", "/../")))

    def test_unexpected_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected native link trace target"):
            native_trace_lines(diagnostic("linker stdout: " + PATH.replace("x86_64-unknown-linux-musl", "unknown-triple")))

    def test_absolute_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported native link trace path"):
            native_trace_lines(diagnostic("linker stdout: /unknown/" + PATH))


if __name__ == "__main__":
    unittest.main()
