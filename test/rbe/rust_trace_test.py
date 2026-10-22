"""Regression cases for the Rust selected-input evidence boundary."""
import json
import unittest

from image_workload import native_trace_lines

OWNER = "bazel-out/k8-opt/bin/images/basic"
OWNERS = {OWNER}
PATH = "bazel-out/k8-opt/bin/external/rust_tools/lib/rustlib/x86_64-unknown-linux-musl/lib/self-contained/libc.a"


def diagnostic(message: str, code: str = "linker_messages") -> str:
    return json.dumps({"code": {"code": code}, "level": "warning", "message": message})


class TraceTest(unittest.TestCase):
    def test_actual_archive_member(self):
        line = PATH + "(memcpy.o)"
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + line), OWNERS), [{"path": PATH, "line": line}])

    def test_prospective_command_is_not_trace(self):
        self.assertEqual(native_trace_lines('"rust-lld" "' + PATH + '"', OWNERS), [])

    def test_unrelated_diagnostic_is_not_trace(self):
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + PATH, "unused"), OWNERS), [])

    def test_prospective_command_inside_diagnostic_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported native link trace path"):
            native_trace_lines(diagnostic('linker stdout: "rust-lld" "' + PATH + '"'), OWNERS)

    def test_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Noncanonical native link trace path"):
            native_trace_lines(diagnostic("linker stdout: " + PATH.replace("/bin/", "/../")), OWNERS)

    def test_unexpected_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unexpected native link trace target"):
            native_trace_lines(diagnostic("linker stdout: " + PATH.replace("x86_64-unknown-linux-musl", "unknown-triple")), OWNERS)

    def test_absolute_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported native link trace path"):
            native_trace_lines(diagnostic("linker stdout: /unknown/" + PATH), OWNERS)

    def test_foreign_native_inputs_are_rejected(self):
        for trace in (
            "bazel-out/k8-opt/bin/external/llvm/libunwind/liblibunwind.static.a(UnwindLevel1.pic.o)",
            "external/toolchain/crtbegin.o",
            "/usr/lib/libunwind.a(UnwindLevel1.o)",
        ):
            with self.subTest(trace=trace), self.assertRaisesRegex(ValueError, "outside the pinned Rust sysroot"):
                native_trace_lines(diagnostic("linker stdout: " + trace), OWNERS)

    def test_absolute_owning_symbols_object_is_not_a_foreign_runtime(self):
        trace = "/buildbuddy-execroot/" + OWNER + "/rustcVaWIOD/symbols.o"
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + trace), OWNERS), [])

    def test_owning_symbols_lookalikes_are_rejected(self):
        for owner in (OWNER.replace("k8-opt", "other-config"), OWNER + "/../basic"):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, "outside the pinned Rust sysroot"):
                native_trace_lines(diagnostic("linker stdout: /buildbuddy-execroot/" + owner + "/rustcVaWIOD/symbols.o"), OWNERS)

    def test_owning_rust_object_is_not_a_foreign_runtime(self):
        trace = "bazel-out/k8-opt/bin/images/basic/rust_hello_world.rcgu.o"
        self.assertEqual(native_trace_lines(diagnostic("linker stdout: " + trace), OWNERS), [])


if __name__ == "__main__":
    unittest.main()
