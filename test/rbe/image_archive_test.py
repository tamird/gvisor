"""Verify ELF hardlink classification using the retained syscallbench inventory."""
import copy
import unittest

from image_archive import executable_aliases

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


if __name__ == "__main__":
    unittest.main()
