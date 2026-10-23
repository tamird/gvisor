"""Verify configured image input ownership."""
import unittest

from image_archive import configured_inputs


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
