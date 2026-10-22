"""Regressions for archive path resolution and conservative ELF lookup."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from image_archive import elf_closure, resolve


class ArchiveResolutionTest(unittest.TestCase):
    def test_chained_symlink_before_parent(self):
        files = {"a": {"type": "2", "link": "b"},
                 "b": {"type": "2", "link": "c/d"},
                 "c/d": {"type": "5"}, "c/x": {"type": "0"},
                 "x": {"type": "0"}}
        self.assertEqual(resolve(files, "/a/../x"), "c/x")

    def test_expanding_cycle_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "Too many image symlinks"):
            resolve({"a": {"type": "2", "link": "a/a"}}, "/a")

    def test_implicit_directory(self):
        self.assertEqual(resolve({"lib/triplet/libc.so": {"type": "0"}},
                                 "/lib/triplet/libc.so"), "lib/triplet/libc.so")

    def test_file_cannot_be_parent(self):
        with self.assertRaisesRegex(RuntimeError, "not a directory"):
            resolve({"a": {"type": "0"}, "x": {"type": "0"}}, "/a/../x")

    def test_absolute_symlink_stays_in_image(self):
        files = {"bin": {"type": "2", "link": "/usr/bin"},
                 "usr/bin/sh": {"type": "0"}}
        self.assertEqual(resolve(files, "/bin/sh"), "usr/bin/sh")

    def test_empty_symlink_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Empty image symlink"):
            resolve({"a": {"type": "2", "link": ""}, "x": {"type": "0"}}, "/a/x")

    def test_escape_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "escapes root"):
            resolve({"a": {"type": "2", "link": "../x"}}, "/a")


class ElfLookupTest(unittest.TestCase):
    def check(self, library_path, extra="", soname="libfoo.so.1", symlink=None):
        reports = {
            "opt/app": "Machine: Advanced Micro Devices X86-64\n"
                       "[Requesting program interpreter: /lib/ld.so]\n"
                       "(NEEDED) Shared library: [libfoo.so.1]\n" + extra,
            "lib/ld.so": "Machine: Advanced Micro Devices X86-64\n",
            library_path: "Machine: Advanced Micro Devices X86-64\n"
                          "(SONAME) Library soname: [" + soname + "]\n",
        }
        elf_files = {name: name.encode() for name in reports}
        files = {name: {"type": "0", "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in elf_files.items()}
        if symlink:
            files[symlink[0]] = {"type": "2", "link": symlink[1]}
        with tempfile.TemporaryDirectory(prefix="image-archive-test-") as temporary:
            destination = Path(temporary) / "readelf.txt"
            try:
                return elf_closure(
                    files, elf_files, "Advanced Micro Devices X86-64",
                    ["/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib", "/usr/lib"],
                    lambda argv: reports[Path(argv[-1]).read_text()], destination)
            finally:
                self.assertTrue(destination.is_file())

    def test_soname_outside_search_path_is_insufficient(self):
        with self.assertRaisesRegex(ValueError, "Unresolved installed dependency"):
            self.check("unrelated/libfoo.so.1")

    def test_soname_without_needed_filename_is_insufficient(self):
        with self.assertRaisesRegex(ValueError, "Unresolved installed dependency"):
            self.check("lib/x86_64-linux-gnu/libfoo.so.1.2")

    def test_declared_symlink_resolves(self):
        result = self.check("lib/x86_64-linux-gnu/libfoo.so.1.2",
                            symlink=("lib/x86_64-linux-gnu/libfoo.so.1", "libfoo.so.1.2"))
        self.assertEqual(result["resolved_dependencies"]["opt/app"]["libfoo.so.1"]["resolved"],
                         "lib/x86_64-linux-gnu/libfoo.so.1.2")

    def test_origin_runpath_resolves(self):
        result = self.check("opt/lib/libfoo.so.1", "(RUNPATH) Library runpath: [$ORIGIN/lib]\n")
        self.assertEqual(result["resolved_dependencies"]["opt/app"]["libfoo.so.1"]["resolved"],
                         "opt/lib/libfoo.so.1")

    def test_runpath_supersedes_rpath(self):
        with self.assertRaisesRegex(ValueError, "Unresolved installed dependency"):
            self.check("opt/lib/libfoo.so.1", "(RPATH) Library rpath: [$ORIGIN/lib]\n"
                       "(RUNPATH) Library runpath: [/missing]\n")

    def test_unsupported_token_is_rejected(self):
        for token in ("$LIB", "$ORIGINsuffix"):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "Unsupported loader search path"):
                self.check("lib/libfoo.so.1", "(RUNPATH) Library runpath: [" + token + "]\n")

    def test_nodeflib_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported NODEFLIB"):
            self.check("lib/libfoo.so.1", "(FLAGS_1) Flags: NODEFLIB PIE\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
