"""Verify image hardlinks and configured input ownership."""
import copy
import unittest

from image_archive import configured_inputs, executable_aliases

# Exact two members from retry1 archive inventory, before verifier correction.
FILES = {
    "usr/bin/syscallbench": {
        "type": "0", "mode": 493, "uid": 0, "gid": 0, "link": "",
        "bytes": 36832,
        "sha256": "3080c08b244464b891c247e66120dd332daaae9bbb405dec67b842115a480b21",
    },
    "usr/bin/syscallbench.runfiles/_main/images/benchmarks/syscallbench": {
        "type": "1", "mode": 493, "uid": 0, "gid": 0,
        "link": "usr/bin/syscallbench", "bytes": 36832,
        "sha256": "3080c08b244464b891c247e66120dd332daaae9bbb405dec67b842115a480b21",
    },
}
CANONICAL = "usr/bin/syscallbench"
ALIAS = "usr/bin/syscallbench.runfiles/_main/images/benchmarks/syscallbench"


class ExecutableAliasesTest(unittest.TestCase):
    def test_retained_hardlink(self) -> None:
        self.assertEqual(executable_aliases(FILES, FILES, [CANONICAL]), {ALIAS: CANONICAL})

    def test_unexpected_executable(self) -> None:
        for field, value in (("type", "0"), ("link", "usr/bin/foreign"),
                             ("sha256", "0" * 64), ("bytes", 1)):
            with self.subTest(field=field):
                changed = copy.deepcopy(FILES)
                changed[ALIAS][field] = value
                with self.assertRaisesRegex(ValueError, "unexpected ELF"):
                    executable_aliases(changed, changed, [CANONICAL])

    def test_missing_canonical(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing an expected ELF"):
            executable_aliases(FILES, [ALIAS], [CANONICAL])


class ConfiguredInputsTest(unittest.TestCase):
    def test_exact_owners(self) -> None:
        rows = [
            ["@@//images:busybox_binary", "busybox_binary", ["bazel-out/k8-opt/bin/images/busybox"]],
            ["@@+_repo_rules+linux_pidfd//:COPYING", "COPYING", ["external/+_repo_rules+linux_pidfd/COPYING"]],
        ]
        self.assertEqual(configured_inputs(rows, ["//images:busybox_binary"], ["COPYING"]), {
            "busybox_binary": "bazel-out/k8-opt/bin/images/busybox",
            "COPYING": "external/+_repo_rules+linux_pidfd/COPYING",
        })

    def test_mismatched_or_missing_owners(self) -> None:
        local = ["//images:busybox_binary", "busybox_binary", ["bazel-out/k8-opt/bin/images/busybox"]]
        source = ["@@+_repo_rules+linux_pidfd//:COPYING", "COPYING", ["external/+_repo_rules+linux_pidfd/COPYING"]]
        foreign = ["@@+_repo_rules+coreutils//:COPYING", "COPYING", ["external/+_repo_rules+coreutils/COPYING"]]
        for rows in ([local, source, foreign], [local], [source],
                     [["//foreign:busybox_binary", *local[1:]], source],
                     [local, [foreign[0], "COPYING", source[2]]],
                     [local, [source[0], "COPYING", ["external/../COPYING"]]],
                     [local, [source[0], "COPYING", ["/external/linux/COPYING"]]],
                     [local, [source[0], "COPYING", []]]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    configured_inputs(rows, ["//images:busybox_binary"], ["COPYING"])


if __name__ == "__main__":
    unittest.main()
