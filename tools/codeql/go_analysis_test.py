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
import xml.etree.ElementTree as ET

from tools.codeql import go_analysis
from tools.codeql import update_go_roots


class SourceInventoryTest(unittest.TestCase):
    def query(self) -> ET.Element:
        return ET.fromstring('''<query>
          <rule class="alias" name="//pkg:ordinary_codeql">
            <label name="actual" value="//pkg:ordinary"/>
            <list name="tags"><string value="manual"/><string value="codeql-go-owner://pkg:ordinary"/></list>
          </rule>
          <rule class="filegroup" name="//pkg:configured_codeql">
            <list name="srcs"><label value="//pkg:main.go"/></list>
            <list name="tags"><string value="manual"/><string value="codeql-go-owner://pkg:configured"/>
              <string value="codeql-go-excluded:coverage"/><string value="codeql-go-excluded:race"/></list>
          </rule>
        </query>''')

    def test_inventory_preserves_configured_owners(self) -> None:
        self.assertEqual(update_go_roots.roots_from_query(self.query()), (
            {"//pkg:ordinary_codeql": "//pkg:ordinary"},
            {"//pkg:configured_codeql": "//pkg:configured"},
            {"//pkg:configured": ["coverage", "race"]},
        ))

    def test_disposition_requires_known_mode_and_sources(self) -> None:
        for mutation in ("unknown mode", "missing sources", "alias mode", "wrong owner"):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                query = self.query()
                configured = query.findall("rule")[1]
                if mutation == "unknown mode":
                    configured.find("list[@name='tags']/string[@value='codeql-go-excluded:race']").set(
                        "value", "codeql-go-excluded:unknown")
                elif mutation == "missing sources":
                    configured.find("list[@name='srcs']").clear()
                elif mutation == "alias mode":
                    configured.set("class", "alias")
                else:
                    query.find("rule/label[@name='actual']").set("value", "//pkg:other")
                update_go_roots.roots_from_query(query)


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

    def original(self, name: str, content: str = "package main\n") -> str:
        path = self.root / "pkg" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
        self.inputs.append({"path": str(path.relative_to(self.root)), "owner": "@@//pkg:" + name,
                            "short_path": "pkg/" + name, "is_source": True, "is_directory": False})
        return str(path)

    def test_default_tag_fallback_keeps_unique_sources(self) -> None:
        main = self.original("main.go")
        tagged = self.original("tagged.go", "//go:build feature\n\npackage main\n")
        fallback = self.original("untagged.go", "//go:build !feature\n\npackage main\n")
        raw = self.record([main, tagged, fallback])
        filtered = {**self.record([main, fallback]), "IgnoredFiles": [tagged]}
        owner = {"owner": "//pkg:tagged", "package_id": raw["ID"], "cgo": False, "original_sources": [main, tagged, fallback]}
        inputs = go_analysis.source_files(self.inputs, self.root)
        selection = go_analysis.default_roots([owner], {raw["ID"]: raw}, [filtered], inputs, self.root)
        self.assertEqual((selection.labels, selection.empty_roots), ([raw["ID"]], []))
        excluded = [{"owner": "//pkg:race", "modes": ["race"], "sources": [main, tagged, fallback]}]
        parity = go_analysis.configured_source_parity(excluded, {raw["ID"]: raw}, {main, fallback}, inputs, self.root)
        self.assertEqual([item["default_eligible"] for item in parity[0]["sources"]], [True, False, True])

    def test_configured_owner_cannot_lose_unique_original(self) -> None:
        common, unique = self.original("common.go"), self.original("unique.go")
        raw = self.record([common])
        excluded = [{"owner": "//pkg:race", "modes": ["race"], "sources": [common, unique]}]
        with self.assertRaisesRegex(ValueError, "loses a baseline source.*unique.go"):
            go_analysis.configured_source_parity(excluded, {raw["ID"]: raw}, {common},
                                                go_analysis.source_files(self.inputs, self.root), self.root)

    def test_empty_default_root_requires_complete_original_membership(self) -> None:
        main = self.original("main.go")
        plugin = self.original("plugin.go", "//go:build network_plugins\n\npackage main\n")
        normal = self.record([main])
        raw = self.record([plugin], identity="@@//pkg:plugin")
        empty = {**self.record([], identity=raw["ID"]), "GoFiles": [plugin], "IgnoredFiles": [plugin]}
        normal_owner = {"owner": "//pkg:main", "package_id": normal["ID"], "cgo": False, "original_sources": [main]}
        owner = {"owner": "//pkg:plugin", "package_id": raw["ID"], "cgo": False, "original_sources": [plugin]}
        inputs = go_analysis.source_files(self.inputs, self.root)
        # The canonical Go JSON serializer omits empty slices; an explicit
        # empty list is equivalent, while malformed present values stay invalid.
        for filtered in (empty, {key: value for key, value in empty.items() if key != "CompiledGoFiles"}):
            with self.subTest(filtered=filtered):
                selection = go_analysis.default_roots([normal_owner, owner], {normal["ID"]: normal, raw["ID"]: raw},
                                                     [normal, filtered], inputs, self.root)
                self.assertEqual(selection.labels, [normal["ID"]])
                self.assertEqual(selection.empty_roots, [{"owner": owner, "reason": "no-default-sources", "sources": [plugin],
                                             "raw_go_files": [plugin], "raw_compiled_go_files": [plugin],
                                             "constraints": {plugin: ["//go:build network_plugins"]}}])
        for files in (None, "", [None]):
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, "invalid root source paths"):
                go_analysis.default_roots([normal_owner, owner], {normal["ID"]: normal, raw["ID"]: raw},
                                          [normal, {**empty, "CompiledGoFiles": files}], inputs, self.root)
        for originals, raw_files in [([], []), ([plugin], []), ([plugin], [plugin, main])]:
            with self.subTest(originals=originals, raw_files=raw_files), self.assertRaises(ValueError):
                go_analysis.default_roots([normal_owner, {**owner, "original_sources": originals}],
                                          {normal["ID"]: normal, raw["ID"]: self.record(raw_files, identity=raw["ID"])},
                                          [normal, empty], inputs, self.root)
        for compiled in ([], [plugin]):
            with self.subTest(compiled=compiled), self.assertRaisesRegex(ValueError, "prefilter metadata omitted original sources"):
                go_analysis.default_roots([normal_owner, {**owner, "original_sources": [plugin, main]}],
                                          {normal["ID"]: normal, raw["ID"]: {
                                              **self.record([plugin, main], identity=raw["ID"]), "CompiledGoFiles": compiled}},
                                          [normal, empty], inputs, self.root)

    def test_root_metadata_cannot_drop_default_source(self) -> None:
        main, fallback = self.original("main.go"), self.original("untagged.go")
        raw = self.record([main])
        owner = {"owner": "//pkg:tagged", "package_id": raw["ID"], "cgo": False, "original_sources": [main, fallback]}
        with self.assertRaisesRegex(ValueError, "omitted original sources.*untagged.go"):
            go_analysis.default_roots([owner], {raw["ID"]: raw}, [raw],
                                      go_analysis.source_files(self.inputs, self.root), self.root)

    def test_nonempty_root_requires_original_prefilter_sources(self) -> None:
        main, other = self.original("main.go"), self.original("other.go")
        raw = {**self.record([main, other]), "CompiledGoFiles": [main]}
        owner = {"owner": "//pkg:main", "package_id": raw["ID"], "cgo": False, "original_sources": [main, other]}
        with self.assertRaisesRegex(ValueError, "prefilter metadata omitted original sources.*other.go"):
            go_analysis.default_roots([owner], {raw["ID"]: raw}, [self.record([main])],
                                      go_analysis.source_files(self.inputs, self.root), self.root)

    def test_cgo_root_retains_transformed_source_contract(self) -> None:
        original = self.original("cgo.go", 'package p\nimport "C"\n')
        generated = self.source("cgo", "_cgo_gotypes.go")
        raw = {**self.record([original]), "CompiledGoFiles": [generated]}
        owner = {"owner": "//pkg:cgo", "package_id": raw["ID"], "cgo": True, "original_sources": [original]}
        selection = go_analysis.default_roots([owner], {raw["ID"]: raw}, [raw],
                                            go_analysis.source_files(self.inputs, self.root), self.root)
        self.assertEqual((selection.labels, selection.empty_roots), ([raw["ID"]], []))

    def test_missing_prefilter_original_requires_canonical_exclusion(self) -> None:
        main = self.original("main.go")
        cgo = self.original("cgo.go", '//go:build network_plugins\n\npackage main\nimport "C"\n')
        raw = {**self.record([main, cgo]), "CompiledGoFiles": [main]}
        loaded = {**raw, "IgnoredFiles": [cgo]}
        owner = {"owner": "//pkg:main", "package_id": raw["ID"], "cgo": False, "original_sources": [main, cgo]}
        inputs = go_analysis.source_files(self.inputs, self.root)
        selection = go_analysis.default_roots([owner], {raw["ID"]: raw}, [loaded], inputs, self.root)
        self.assertEqual(selection.labels, [raw["ID"]])
        evidence = selection.original_exclusions[0]["sources"][0]
        self.assertEqual(evidence["path"], cgo)
        self.assertEqual(evidence["facts"]["sha256"], inputs[cgo].sha256)
        self.assertFalse(evidence["present_in_compiled_prefilter"])
        self.assertEqual(evidence["constraints"], ["//go:build network_plugins"])
        for ignored in ([], None, "", [None], [str(self.root / "undeclared.go")], [main, cgo]):
            with self.subTest(ignored=ignored), self.assertRaises(ValueError):
                go_analysis.default_roots([owner], {raw["ID"]: raw}, [{**loaded, "IgnoredFiles": ignored}], inputs, self.root)

    def test_empty_root_requires_actual_original_constraint_evaluation(self) -> None:
        main = self.original("main.go")
        plugin = self.original("plugin.go", '//go:build network_plugins\n\npackage main\nimport "C"\n')
        normal = self.record([main])
        raw = {**self.record([plugin], identity="@@//pkg:plugin"), "CompiledGoFiles": []}
        owners = [{"owner": "//pkg:main", "package_id": normal["ID"], "cgo": False, "original_sources": [main]},
                  {"owner": "//pkg:plugin", "package_id": raw["ID"], "cgo": False, "original_sources": [plugin]}]
        inputs = go_analysis.source_files(self.inputs, self.root)
        selection = go_analysis.default_roots(owners, {normal["ID"]: normal, raw["ID"]: raw},
                                             [normal, {**raw, "IgnoredFiles": [plugin]}], inputs, self.root)
        self.assertEqual(selection.labels, [normal["ID"]])
        self.assertEqual(selection.empty_roots[0]["raw_compiled_go_files"], [])
        with self.assertRaisesRegex(ValueError, "prefilter metadata omitted original sources"):
            go_analysis.default_roots(owners, {normal["ID"]: normal, raw["ID"]: raw}, [normal, raw], inputs, self.root)

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
