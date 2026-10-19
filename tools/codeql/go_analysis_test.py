# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Configured source equivalence at the declared-package selection boundary."""

import json
from pathlib import Path
import tempfile
import unittest

from tools.codeql import go_analysis


class ConfiguredPackagesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs: list[go_analysis.DeclaredInput] = []

    def source(self, config: str, name: str = "p.go", content: bytes = b"package p\n") -> str:
        relative = f"bazel-out/{config}/bin/pkg/{name}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.inputs.append({"path": relative, "owner": "@@//pkg:generate",
                            "short_path": f"pkg/{name}", "is_source": False,
                            "is_directory": False})
        return "__BAZEL_EXECROOT__/" + relative

    def record(self, files: list[str], *, identity: str = "@@//pkg:p") -> go_analysis.Package:
        return {"ID": identity, "PkgPath": "example.test/pkg", "Dir": "__BAZEL_WORKSPACE__/pkg",
                "GoFiles": files, "CompiledGoFiles": files, "OtherFiles": []}

    def select(self, first: go_analysis.Package, second: go_analysis.Package) -> go_analysis.PackageSelection:
        paths = []
        for name, record in [("a.json", first), ("b.json", second)]:
            path = self.root / name
            path.write_text(json.dumps(record))
            paths.append(str(path))
        records = [(name, record) for name in reversed(paths) for record in go_analysis.json_records(Path(name))]
        return go_analysis.selected_packages(records, go_analysis.source_files(self.inputs, self.root), self.root,
                                             group_by="PkgPath")

    def test_equal_generated_copies_preserve_records(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two")])
        first["ExportFile"] = "first.a"
        second["ExportFile"] = "second.a"
        selection = self.select(first, second)
        self.assertEqual(selection.records, {first["ID"]: first})
        self.assertEqual(selection.equivalents, {
            str(self.root / "bazel-out/two/bin/pkg/p.go"): str(self.root / "bazel-out/one/bin/pkg/p.go"),
        })
        match selection.variants:
            case [{"selected": selected, "record": variant}]:
                self.assertEqual(selected, first)
                self.assertEqual(variant, second)
            case observed:
                self.fail(f"Missing original configured records: {observed}")

    def test_distinct_ids_keep_original_records_and_imports(self) -> None:
        first = self.record([self.source("one")], identity="@@//pkg:first")
        second = self.record([self.source("two")], identity="@@//pkg:second")
        first["Imports"] = second["Imports"] = {"dep": "@@//dep:dep"}
        selection = self.select(first, second)
        self.assertEqual(selection.records, {first["ID"]: first, second["ID"]: second})
        selection.records[first["ID"]]["Module"] = {"Path": "later augmentation"}
        selection.records[second["ID"]]["Imports"]["dep"] = "@@//changed:dep"
        match selection.variants:
            case [{"selected": original, "record": variant}]:
                self.assertEqual(original, first)
                self.assertEqual(variant, second)
            case observed:
                self.fail(f"Missing immutable variant provenance: {observed}")

    def test_distinct_ids_with_different_imports_fail(self) -> None:
        first = self.record([self.source("one")], identity="@@//pkg:first")
        second = self.record([self.source("two")], identity="@@//pkg:second")
        first["Imports"], second["Imports"] = {"dep": "@@//dep:first"}, {"dep": "@@//dep:second"}
        with self.assertRaisesRegex(ValueError, "Imports"):
            self.select(first, second)

    def test_same_id_cannot_change_package_path(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two")])
        second["PkgPath"] = "different.test/pkg"
        with self.assertRaisesRegex(ValueError, "package_paths"):
            self.select(first, second)

    def test_archive_accepts_only_proven_copies(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two")])
        selection = self.select(first, second)
        old_path = str(go_analysis.resolve_input(first["CompiledGoFiles"][0], self.root))
        new_path = str(go_analysis.resolve_input(second["CompiledGoFiles"][0], self.root))
        facts = go_analysis.source_files(self.inputs, self.root)
        expected, actual = {old_path: facts[old_path].sha256}, {new_path: facts[new_path].sha256}
        coverage = go_analysis.archive_coverage(expected, actual, selection.equivalents)
        self.assertEqual(coverage, go_analysis.ArchiveCoverage(expected, expected, [], [], []))
        self.assertEqual(actual, {new_path: facts[new_path].sha256})
        for description, observed, missing, unexpected, mismatched in [
            ("missing source class", {}, [old_path], [], []),
            ("unexpected same-content different owner", {"/other-owner/p.go": facts[old_path].sha256},
             [old_path], ["/other-owner/p.go"], []),
            ("changed archived bytes", {new_path: "changed"}, [], [], [old_path]),
            ("conflicting archived copies", {**expected, new_path: "changed"}, [], [], [old_path]),
        ]:
            with self.subTest(description=description):
                coverage = go_analysis.archive_coverage(expected, observed, selection.equivalents)
                self.assertEqual((coverage.missing, coverage.unexpected, coverage.mismatched),
                                 (missing, unexpected, mismatched))

    def test_changed_generated_bytes_fail(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two", content=b"package p\nconst changed = 1\n")])
        with self.assertRaisesRegex(ValueError, "CompiledGoFiles.*sha256"):
            self.select(first, second)

    def test_logical_identity_and_generated_requirement(self) -> None:
        for field, value in [("owner", "@@//pkg:other"), ("short_path", "pkg/other.go"), ("is_source", True)]:
            with self.subTest(field=field):
                self.inputs.clear()
                first = self.record([self.source("one")])
                second = self.record([self.source("two")])
                self.inputs[-1][field] = value
                with self.assertRaisesRegex(ValueError, "Conflicting configured package records"):
                    self.select(first, second)

    def test_ordered_membership_is_preserved(self) -> None:
        first = self.record([self.source("one", "p.go"), self.source("one", "q.go")])
        second = self.record([self.source("two", "q.go"), self.source("two", "p.go")])
        with self.assertRaisesRegex(ValueError, "short_path"):
            self.select(first, second)

    def test_other_metadata_remains_strict(self) -> None:
        for field, first_value, second_value in [
            ("Imports", {"dep": "@@//dep:first"}, {"dep": "@@//dep:second"}),
            ("Module", {"Path": "example.test"}, {"Path": "different.test"}),
            ("FutureField", 1, 2),
            ("FutureField", {"nested": [True]}, {"nested": [1]}),
            ("FutureField", {"nested": [1]}, {"nested": [1.0]}),
        ]:
            with self.subTest(field=field):
                self.inputs.clear()
                first = self.record([self.source("one")])
                second = self.record([self.source("two")])
                first[field], second[field] = first_value, second_value
                with self.assertRaisesRegex(ValueError, field):
                    self.select(first, second)

    def test_missing_and_null_fields_differ(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two")])
        second["FutureField"] = None
        with self.assertRaisesRegex(ValueError, r'"previous": \{\}'):
            self.select(first, second)

    def test_tree_members_use_declared_relative_identity(self) -> None:
        records = []
        for config in ("one", "two"):
            relative = f"bazel-out/{config}/bin/pkg/cgo"
            directory = self.root / relative
            directory.mkdir(parents=True)
            (directory / "nested").mkdir()
            (directory / "nested/_cgo_gotypes.go").write_text("package p\n")
            self.inputs.append({"path": relative, "owner": "@@//pkg:cgo", "short_path": "pkg/cgo",
                                "is_source": False, "is_directory": True})
            records.append(self.record([f"__BAZEL_EXECROOT__/{relative}/nested/_cgo_gotypes.go"]))
        first, second = records
        selection = self.select(first, second)
        self.assertEqual(len(selection.equivalents), 1)
        facts = go_analysis.source_files(self.inputs, self.root)
        self.assertEqual({value.short_path for value in facts.values()}, {"pkg/cgo/nested/_cgo_gotypes.go"})

    def test_existing_undeclared_source_fails(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two")])
        self.inputs.pop()
        with self.assertRaisesRegex(ValueError, "undeclared_sources"):
            self.select(first, second)

    def test_loaded_scope_does_not_promote_embedded_fragment(self) -> None:
        generated = self.source("one", "generated.go")
        fragment = self.record([generated], identity="@@//pkg:proto")
        container = self.record([generated, self.source("one", "gen.go")], identity="@@//pkg:library")
        records = [("fragment.json", fragment), ("container.json", container)]
        inputs = go_analysis.source_files(self.inputs, self.root)
        configured = go_analysis.selected_packages(records, inputs, self.root, group_by="ID")
        self.assertEqual(configured.records, {fragment["ID"]: fragment, container["ID"]: container})
        loaded = go_analysis.loaded_packages(records, {container["ID"]}, inputs, self.root)
        self.assertEqual(loaded.records, {container["ID"]: container})
        self.assertEqual(loaded.variants, [])
        # An explicit root or import edge makes both packages genuinely loaded.
        with self.assertRaisesRegex(ValueError, "CompiledGoFiles"):
            go_analysis.loaded_packages(records, {fragment["ID"], container["ID"]}, inputs, self.root)

    def test_loaded_scope_rejects_unknown_identity(self) -> None:
        record = self.record([self.source("one")])
        with self.assertRaisesRegex(ValueError, "undeclared package identities.*@@//other:package"):
            go_analysis.loaded_packages([("package.json", record)], {"@@//other:package"},
                                        go_analysis.source_files(self.inputs, self.root), self.root)

    def test_loaded_scope_preserves_unknown_raw_metadata(self) -> None:
        first = self.record([self.source("one")], identity="@@//pkg:first")
        second = self.record([self.source("two")], identity="@@//pkg:second")
        first["FutureField"], second["FutureField"] = {"nested": True}, {"nested": 1}
        # The driver need only return IDs; its serialization cannot erase raw facts.
        with self.assertRaisesRegex(ValueError, "FutureField"):
            go_analysis.loaded_packages([("first.json", first), ("second.json", second)],
                                        {first["ID"], second["ID"]},
                                        go_analysis.source_files(self.inputs, self.root), self.root)

    def test_reports_all_package_conflicts(self) -> None:
        first = self.record([self.source("one")])
        second = self.record([self.source("two", content=b"package changed\n")])
        paths = []
        for name, record in [("a.json", first), ("b.json", second)]:
            path = self.root / name
            path.write_text("\n".join(json.dumps({**record, "ID": identity}) for identity in ("@@//pkg:first", "@@//pkg:second")))
            paths.append(str(path))
        with self.assertRaises(ValueError) as caught:
            records = [(name, record) for name in paths for record in go_analysis.json_records(Path(name))]
            go_analysis.selected_packages(records, go_analysis.source_files(self.inputs, self.root), self.root,
                                          group_by="ID")
        self.assertIn("@@//pkg:first", str(caught.exception))
        self.assertIn("@@//pkg:second", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
