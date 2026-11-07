"""Compare top-level wildcard requests and inspect one real ARM64 archive."""

import collections
import gzip
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
import re
import struct
import subprocess
import time
from urllib.parse import unquote, urlparse

SOURCE = "f6d6788e84731673a155c9fff6f28b8624e34c50"
WITNESS = "//pkg/metric:metric_cc_proto"
ARCHIVE = "pkg/metric/libmetric_proto.a"
MIXED_PROJECTION = "9bceba8959a2a79af1a4fba2a2173f9946f91876c4748bc2c3eeace8f01a49da"
WORK = Path(os.environ["RUNNER_TEMP"]) / "wildcard-products"
OUT = WORK / "out"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx5g"]
COMMON = ["--jobs=3", "--loading_phase_threads=3", "--lockfile_mode=off", "--remote_cache=",
          "--remote_executor=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox"]
REPORT = {"source": SOURCE, "commands": {}, "selections": {}, "failures": {}}
PROJECTIONS = {}
SCOPE = ("//pkg:", "//pkg/", "//runsc:", "//runsc/")
OPTIONS = {"cpu", "platforms", "build_tag_filters", "test_tag_filters", "host_platform", "define",
           "spawn_strategy", "strategy", "extra_execution_platforms", "remote_default_exec_properties", "target_pattern_file"}
def save():
    dest = OUT / "wildcard.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(REPORT, sort_keys=True, indent=2) + "\n")
    tmp.replace(dest)


def checked(name, action, *args):
    stdout, stderr = WORK / (name + ".stdout"), WORK / (name + ".stderr")
    start = time.monotonic()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        rc = subprocess.run([*BAZEL, action, *args], stdout=out, stderr=err).returncode
    info = {"exit": rc, "elapsed_s": round(time.monotonic() - start, 2)}
    REPORT["commands"][name] = info
    if rc:
        lines = stderr.read_text(errors="replace").splitlines()
        chosen = [line for line in lines if "ERROR:" in line][:6] or lines[-5:]
        info["diagnostic"] = [re.sub(r"https?://[^\s)]+", "<public download>", line)
                              .replace(str(WORK), "<work>")[:300] for line in chosen
                              if not re.search(r"(?i)authorization:|credential|token=", line)]
    save()
    if rc == 2:
        REPORT["failures"][name] = "Bazel command-line or strategy initialization failed"
        save()
        raise SystemExit(2)
    if rc:
        raise ValueError(f"{name} exited {rc}")
    return stdout


def attempt(name, function):
    try:
        return function()
    except Exception as exc:
        REPORT["failures"][name] = str(exc)[:480] or type(exc).__name__
        save()


def records(name, flags, expression):
    query = WORK / (name + ".query")
    query.write_text(expression)
    out = checked(name, "cquery", *flags, *COMMON, "--query_file=" + str(query),
                  "--implicit_deps", "--tool_deps", "--include_aspects", "--output=starlark", "--starlark:file=" + str(WORK / "products.star"))
    seen = set()
    with out.open() as f:
        for line in f:
            if not line.strip():
                continue
            label, cpu, platforms, is_exec, files = json.loads(line)
            assert label.startswith(("//", "@")), label
            # short_path identifies the declared relative output, independently of output configuration.
            # Keep source files distinct and exclude them from the generated default-product comparison.
            row = (label, cpu, tuple(platforms), is_exec, tuple(sorted(tuple(v) for v in files)))
            seen.add(row)
    return seen



def pack(name, obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    payload = gzip.compress(raw, mtime=0)
    assert len(payload) <= 2 << 20, name
    (OUT / (name + ".json.gz")).write_bytes(payload)


def selection(name, cmd, args, patterns, tag_filter):
    bep = WORK / (name + ".bep")
    local_test = ["--strategy=TestRunner=processwrapper-sandbox"] if cmd == "test" else []
    checked(name + "-selection", cmd, *args, *COMMON, *local_test, "--nobuild",
            "--build_event_json_file=" + str(bep), *patterns)
    selected, suites, opts, kinds = {}, set(), {}, {}
    for line in bep.open():
        event = json.loads(line)
        assert "testResult" not in event
        canonical = event.get("structuredCommandLine", {})
        if canonical.get("commandLineLabel") == "canonical":
            for section in canonical.get("sections", []):
                for opt in section.get("optionList", {}).get("option", []):
                    key = opt.get("optionName")
                    if key in OPTIONS:
                        opts.setdefault(key, []).append(opt.get("optionValue"))
        if "configured" in event:
            label = event["id"]["targetConfigured"]["label"]
            kinds[label] = event["configured"].get("targetKind")
            if kinds[label] == "test_suite rule":
                suites.add(label)
        target = event["id"].get("targetCompleted", {})
        if target and not target.get("aspect") and (event.get("aborted", {}).get("reason") == "NO_BUILD"
                                                    or event.get("completed", {}).get("success")):
            label, checksum = target["label"], target["configuration"]["id"]
            if label.startswith(SCOPE):
                assert re.fullmatch("[a-f0-9]{64}|none|null", checksum), checksum
                selected.setdefault(label, set()).add(checksum)
    selected = {k: sorted(v) for k, v in selected.items() if k not in suites}
    assert selected, name
    info = REPORT["selections"][name] = {"effective": opts, "raw_args": [cmd, *args, *patterns],
                                         "selected": len(selected), "suites": sorted(suites)}
    pack(name + "-owners", {k: [v, kinds.get(k)] for k, v in selected.items()})
    save()
    configs = collections.defaultdict(set)
    for label, hashes in selected.items():
        for cfg in hashes:
            configs["null" if cfg in ("none", "null") else cfg].add(label)
    def expression(labels, cfg):
        return "config(set(" + " ".join(json.dumps(k) for k in sorted(labels)) + "), " + cfg + ")"
    # cquery needs identical build options. Test-only rc expansions do not apply to cquery.
    flags = list(args)
    if cmd == "test":
        flags += ["--build_tag_filters=" + tag_filter]
    result = {}
    for i, (cfg, labels) in enumerate(configs.items()):
        ident = name + "-cfg" + str(i)
        rows = records(ident, flags, expression(labels, cfg))
        # Keep all successful rows before resolving aliases or asserting membership.
        pack(ident + "-rows", sorted(rows))
        by_label = collections.defaultdict(list)
        for row in sorted(rows):
            by_label[row[0]].append(row)
        missing, extra = sorted(labels - by_label.keys()), sorted(by_label.keys() - labels)
        info.setdefault("join", []).append({"config": cfg, "selected_minus_projected": missing,
                                            "projected_minus_selected": extra,
                                            "missing_kinds": {k: kinds.get(k) for k in missing},
                                            "returned_rows": len(rows)})
        save()
        for label in sorted(labels):
            returned = by_label[label]
            if not returned:
                # AliasConfiguredTarget reports its underlying label in Starlark.
                # Ask for this exact original owner/config independently before assigning its files.
                returned = sorted(records(ident + "-resolve" + str(missing.index(label)), flags,
                                          expression([label], cfg)))
                info.setdefault("resolved", {})[label] = {"kind": kinds.get(label),
                                                          "returned": [r[0] for r in returned]}
            if returned:
                result.setdefault(label, []).extend(returned)
        PROJECTIONS[name] = result
        pack(name + "-joined", result)
        save()
    info["unresolved"] = sorted(selected.keys() - result.keys())
    info["effective_tag_filter_matches"] = opts.get("build_tag_filters", [])[-1:] == [tag_filter]
    save()
    assert not info["unresolved"], info["unresolved"][:8]
    assert info["effective_tag_filter_matches"], opts


def arch_of(row):
    platforms = {p.rsplit(":", 1)[-1] for p in row[2]}
    known = {"linux_amd64": "amd64", "linux_arm64": "arm64",
             "linux_x86_64_musl": "amd64", "linux_aarch64_musl": "arm64"}
    matches = {known[p] for p in platforms & known.keys()}
    return next(iter(matches)) if len(matches) == 1 and not row[3] else None


def compare(name, baseline, candidate):
    reference, proposed = PROJECTIONS[baseline], PROJECTIONS[candidate]
    def products(rows):
        # Include transitioned outputs of the ordinary native wildcard. Keep the
        # returned File owner and its output architecture even when the outer rule
        # forwards a File from a different configuration.
        items = set()
        for r in rows:
            for short, source, directory, path, owner in r[4]:
                if source:
                    continue
                match = re.match(r"(?:bazel-out|blaze-out)/([^/]+)/", path)
                assert match, path
                mnemonic = match.group(1)
                role = "exec" if "-exec-" in mnemonic else "target"
                items.add((short, directory, owner, mnemonic.split("-", 1)[0], role))
        return items
    union = set().union(*(products(rows) for rows in proposed.values()))
    differences = []
    no_output, by_other, matched = [], [], 0
    for label, rows in reference.items():
        needed = products(rows)
        if not needed:
            no_output.append(label)
            continue
        if missing := needed - union:
            differences.append([label, sorted(missing)])
        elif needed - products(proposed.get(label, [])):
            by_other.append(label)
        else:
            matched += 1
    area = "//" + baseline.split("-")[1]
    proposed_labels = {k for k in proposed if k.startswith((area + ":", area + "/"))}
    desc = {"native_owners": len(reference), "candidate_owners": len(proposed),
            "owners_with_missing_generated_defaults": len(differences), "same_owner_defaults": matched,
            "supplied_by_another_root": len(by_other), "no_generated_target_default": len(no_output),
            "missing_examples": differences[:8], "missing_selected_labels": sorted(reference.keys() - proposed.keys())[:16],
            "extra_selected_owner_count": len(proposed_labels - reference.keys())}
    REPORT.setdefault("comparisons", {})[name] = desc
    pack(name + "-comparison", {"missing_products": differences, "no_generated_target_default": no_output,
                                "different_owner_same_files": by_other, "missing_labels": sorted(reference.keys() - proposed.keys()),
                                "extra_labels": sorted(proposed_labels - reference.keys())})
    save()


def physical():
    assert platform.machine() == "x86_64"
    bep = WORK / "archive.bep"
    # Use the native AMD64 host toolchain to cross-compile the same ordinary ARM64 target.
    checked("materialize-arm64-archive", "build", "--config=aarch64", *COMMON,
            "--strategy=GoStandardLibraryAnalysis=local", "--strategy=GoStaticAnalysis=local",
            "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), WITNESS)
    targets, groups, actions = [], {}, []
    for line in bep.open():
        e = json.loads(line)
        assert "testResult" not in e
        key = e["id"]
        if "namedSet" in key:
            groups[key["namedSet"]["id"]] = e.get("namedSetOfFiles", {})
        if key.get("targetCompleted", {}).get("label") == WITNESS and not key["targetCompleted"].get("aspect"):
            targets.append(e)
        if e.get("action", {}).get("type") == "CppArchive":
            a = key["actionCompleted"]
            actions.append([a["primaryOutput"], a["label"], a.get("configuration", {}).get("id"),
                            e["action"].get("success", False)])
    target, = targets
    assert target["completed"]["success"]
    default, = [g for g in target["completed"]["outputGroup"] if g["name"] == "default"]
    pending = [v["id"] for v in default["fileSets"]]
    files, seen = [], set()
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            files.extend(groups[key].get("files", []))
            pending.extend(v["id"] for v in groups[key].get("fileSets", []))
    f, = [v for v in files if v["name"].endswith("libmetric_proto.a")]
    assert urlparse(f["uri"]).scheme == "file"
    path = Path(unquote(urlparse(f["uri"]).path))
    execpath = "/".join([*f.get("pathPrefix", []), f["name"]])
    cfg = target["id"]["targetCompleted"]["configuration"]["id"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    info = REPORT["physical"] = {"label": WITNESS, "configuration": cfg, "execpath": execpath,
                                 "defaults": [v["name"] for v in files], "actual_sha256": digest,
                                 "advertised_sha256": f.get("digest"), "bytes": path.stat().st_size,
                                 "executing_host": platform.machine(), "compiler_target": "arm64"}
    save()
    assert execpath.endswith("/" + ARCHIVE)
    if f.get("digest"):
        assert f["digest"] == digest
    assert int(f.get("length", path.stat().st_size)) == path.stat().st_size
    producers = [a for a in actions if a[0] == execpath and a[3]]
    info["archive_actions"] = producers
    members = subprocess.check_output(["ar", "t", path], text=True).splitlines()
    assert members and len(members) == len(set(members)), members
    for member in members:
        data = subprocess.check_output(["ar", "p", path, member])
        assert data[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H", data, 16)[0] == 1
        machine = struct.unpack_from("<H", data, 18)[0]
        info.setdefault("members", []).append([member, machine, len(data), hashlib.sha256(data).hexdigest()])
        save()
        assert machine == 183
    assert len(producers) == 1 and producers[0][2] == cfg, producers


def main():
    original = (WORK / "mixed-products.jsonl.gz").read_bytes()
    assert hashlib.sha256(original).hexdigest() == MIXED_PROJECTION
    rows = [json.loads(s) for s in gzip.decompress(original).splitlines()]
    assert len(rows) == 16709 and all(r[0] == "mixed" for r in rows)
    found = [r for r in rows if any(f[0] == ARCHIVE for f in r[-1])]
    assert len(found) == 1 and found[0][1] == WITNESS and arch_of(found[0][1:]) == "amd64"
    REPORT["original"] = {"artifact": 13591670976, "rows": len(rows), "all_archive_default_rows": found,
                          "projection_sha256": MIXED_PROJECTION}
    save()
    attempt("physical", physical)
    for cpu, native, host in [("amd64", "x86_64", "linux_amd64"), ("arm64", "aarch64", "linux_arm64")]:
        for area, tags in [("pkg", "-nogo"), ("runsc", "-network_plugins")]:
            args = ["--config=" + native, "--host_platform=//tools/bazeldefs:" + host]
            if area == "runsc":
                args += ["--build_tag_filters=" + tags]
            name = cpu + "-" + area
            attempt(name, lambda: selection(name, "build", args, ["//" + area + "/..."], tags))
    name = "arm64-union"
    attempt(name, lambda: selection(name, "build", ["--config=source-presubmit-arm64", "--build_tag_filters=-network_plugins"],
                                    ["//pkg/...", "//runsc/..."], "-network_plugins"))
    for name, profile in [("mixed", "source-presubmit-mixed"), ("amd64-test", "source-presubmit"),
                          ("arm64-test", "source-presubmit-arm64")]:
        attempt(name, lambda: selection(name, "test", ["--config=" + profile], [], "-network_plugins"))
    for cpu in ("amd64", "arm64"):
        for area in ("pkg", "runsc"):
            base = cpu + "-" + area
            for proposed in ("mixed" if cpu == "amd64" else "arm64-union", cpu + "-test"):
                name = base + "-vs-" + proposed
                attempt(name, lambda: compare(name, base, proposed))
    REPORT["complete"] = not REPORT["failures"]
    save()
    return 0 if REPORT["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
