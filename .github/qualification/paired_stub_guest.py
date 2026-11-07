"""Bind one ARM64 runtime release to two original TCG test results."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

SOURCE = "c157b4d8f448ac3796fd113ad85139623ae461cb"
HARNESS = "ec4ced798422d27e533369b02d41b06f21b798c1"
ORIGINAL_BLOB = "d67bbbc2e4dc8ce9eb96f3bd0410887062bbc1cea5708dcf4b0008f45d5b7d12"
WORK = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest"
PKG = "test/rbe/paired_stub_guest"
ROOTS = ["//:release", "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary", "//" + PKG + ":pagesize_probe"]
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + n for n in (
    "gvisor_sentry", "runsc-netgofer", "runsc-fd-parking", "gvisor-sentry-prewarmer", "gvisor-proc-umounter",
    "runsc-metric-server", "checkpointgofer")}
GUESTS = {"page4k": ("7.3.0-070300rc3-generic", 4096), "page64k": ("6.8.0-138-generic-64k", 65536)}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(name, value):
    (WORK / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def file_bytes(item, fallback=None):
    uri = urlparse(item["uri"])
    if uri.scheme == "file":
        assert not uri.netloc
        path = Path(unquote(uri.path))
    else:
        assert uri.scheme == "bytestream" and fallback is not None
        path = fallback
    assert path.stat().st_size <= 128 << 20, path.name
    data = path.read_bytes()
    digest = sha(data)
    if item.get("digest"):
        assert digest == item["digest"]
    if "length" in item:
        assert len(data) == int(item["length"])
    if uri.scheme == "bytestream":
        assert uri.path.endswith("/blobs/" + digest + "/" + str(len(data)))
    return path, data


def outputs(group, filesets):
    result = []
    seen = set()
    pending = [item["id"] for item in group["fileSets"]]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            row = filesets[key]
            result.extend(row.get("files", []))
            pending.extend(item["id"] for item in row.get("fileSets", []))
    return result


def compiler_evidence(executed):
    owner = "//pkg/sentry/platform/systrap/sysmsg:sighandler.built-in.bin.o"
    args = [WORK / "bazel9", "--nosystem_rc", "--nohome_rc", "aquery", "--config=aarch64", "-c", "fastbuild",
            "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--output=jsonproto", "--include_param_files", 'mnemonic("CppCompile|CppLink", ' + owner + ")"]
    result = subprocess.run(args, capture_output=True, check=True)
    graph = json.loads(result.stdout)
    configs = {str(c["id"]): c["checksum"] for c in graph["configuration"]}
    rows = []
    for action in graph["actions"]:
        config = configs[str(action["configurationId"])]
        argv = list(action["arguments"])
        argv.extend(a for param in action.get("paramFiles", []) for a in param.get("arguments", []))
        mnemonic = action["mnemonic"]
        assert (config, mnemonic) in executed
        rows.append({"mnemonic": mnemonic, "config": config, "driver": argv[0],
                     "options": [v for v in argv if v.startswith(("-O", "-g", "-march", "-mcpu", "-mno-", "-DPAGE_SIZE",
                                  "-fno-", "-ffreestanding", "-fpie", "--target", "-nostdlib", "-Wl,"))][:55]})
    assert len(rows) == 5
    return rows


def build():
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == SOURCE
    bep = WORK / "build.bep"
    command = [WORK / "bazel9", "--nosystem_rc", "--nohome_rc", "build", "--config=aarch64", "-c", "fastbuild",
               "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=", "--bes_backend=",
               "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *ROOTS]
    with (WORK / "build.log").open("w") as log:
        rc = subprocess.run(command, stdout=log, stderr=log).returncode
    summary = {"source": SOURCE, "harness": HARNESS, "exit": rc, "members": {}, "roots": {},
               "original_blob_sha256": ORIGINAL_BLOB}
    save("release", summary)
    sets, targets, executed = {}, {}, set()
    for line in bep.open():
        e = json.loads(line)
        ident = e["id"]
        if "started" in e:
            summary["invocation"] = e["started"].get("uuid")
        if "namedSetOfFiles" in e:
            sets[ident["namedSet"]["id"]] = e["namedSetOfFiles"]
        a = ident.get("actionCompleted", {})
        if a.get("label") == "//pkg/sentry/platform/systrap/sysmsg:sighandler.built-in.bin.o" and e.get("action", {}).get("success"):
            executed.add((a["configuration"]["id"], e["action"]["type"]))
        target = ident.get("targetCompleted", {})
        if target.get("label") in ROOTS and not target.get("aspect"):
            label = target["label"]
            summary["roots"][label] = {"success": e.get("completed", {}).get("success", False),
                                       "config": target.get("configuration", {}).get("id")}
            if summary["roots"][label]["success"]:
                group, = [g for g in e["completed"]["outputGroup"] if g["name"] == "default"]
                targets[label] = group
        assert "testResult" not in e
    if rc:
        summary["diagnostics"] = [s[:450] for s in (WORK / "build.log").read_text(errors="replace").splitlines()
                                  if "ERROR:" in s or "fatal error:" in s][:8]
    save("release", summary)
    assert rc == 0 and set(targets) == set(ROOTS)
    inputs = WORK / "inputs"
    inputs.mkdir()
    blob_item, = outputs(targets[ROOTS[1]], sets)
    _, blob = file_bytes(blob_item)
    summary["actual_blob_sha256"] = sha(blob)
    summary["same_as_original_blob"] = sha(blob) == ORIGINAL_BLOB
    summary["compilation"] = "Bazel --config=aarch64 -c fastbuild"
    if not summary["same_as_original_blob"]:
        save("release", summary)
        summary["stub_compiler"] = compiler_evidence(executed)
    for label in (ROOTS[0], ROOTS[2]):
        for item in outputs(targets[label], sets):
            path, data = file_bytes(item)
            assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 183
            if label == ROOTS[0]:
                name = ("gvisor-bin/" if path.parent.name == "gvisor-bin" else "") + path.name
                assert name in MEMBERS and name not in summary["members"]
                rel = "release/" + name
                summary["members"][name] = {"sha256": sha(data), "bytes": len(data)}
                if name == "gvisor-bin/gvisor_sentry":
                    summary["blob_copies_in_sentry"] = data.count(blob)
            else:
                assert path.name == "pagesize_probe"
                rel = "probe"
                summary["probe_sha256"] = sha(data)
            target = inputs / rel
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(path, target)
            target.chmod(0o755)
    assert set(summary["members"]) == MEMBERS and summary["blob_copies_in_sentry"] == 1 and summary["probe_sha256"]
    manifest = inputs / "manifest.sha256"
    manifest.write_text("".join(f"{sha(p.read_bytes())}  {p.relative_to(inputs)}\n" for p in sorted(inputs.rglob("*")) if p.is_file()))
    summary["manifest_sha256"] = sha(manifest.read_bytes())
    summary["verified"] = True
    save("release", summary)


def collect():
    release = json.loads((WORK / "release.json").read_bytes())
    assert release["verified"] and subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == HARNESS
    bep = WORK / "guests.bep"
    report = {"source": SOURCE, "harness": HARNESS, "manifest_sha256": release["manifest_sha256"],
              "original_invocation_exit": int((WORK / "remote.exit").read_text()), "results": {}, "errors": []}
    save("guests", report)
    for line in bep.open():
        e = json.loads(line)
        ident = e["id"]
        if "started" in e:
            report["invocation"] = e["started"].get("uuid")
        action = ident.get("actionCompleted", {})
        if "action" in e and not e["action"].get("success") and len(report["errors"]) < 7:
            report["errors"].append({"owner": action.get("label"), "mnemonic": e["action"].get("type"),
                                     "exit": e["action"].get("exitCode")})
        if "testResult" not in e:
            continue
        label = ident["testResult"]["label"]
        name = label.split(":")[-1]
        assert label == "//" + PKG + ":" + name and name in GUESTS and name not in report["results"]
        t = e["testResult"]
        info = t.get("executionInfo", {})
        row = {"status": t["status"], "cached": bool(t.get("cachedLocally") or info.get("cachedRemotely")),
               "strategy": info.get("strategy"), "duration_ms": t.get("testAttemptDurationMillis"), "label": label}
        report["results"][name] = row
        save("guests", report)
        for item in t["testActionOutput"]:
            if item["name"] not in ("test.log", "test.xml"):
                continue
            _, data = file_bytes(item, Path("bazel-testlogs", PKG, name, item["name"]))
            row[item["name"] + "_sha256"] = sha(data)
            if item["name"] == "test.xml":
                xml = ET.fromstring(data)
                cases = list(xml.iter("testcase"))
                row["xml_cases"] = len(cases)
                row["xml_pass"] = (bool(cases) and not any(
                    node.tag in ("failure", "error", "skipped")
                    or any(int(node.get(k, 0)) for k in ("failures", "errors", "skipped"))
                    or node.get("status") in ("notrun", "skipped") or node.get("result") in ("skipped", "suppressed")
                    for node in xml.iter()))
            else:
                log = data.decode(errors="replace").replace("\r", "")
                row["inputs"] = re.findall(r"PAIRED_INPUTS manifest=([0-9a-f]{64}) arch=(\w+) kernel=([\w.-]+) pages=(\d+)", log)
                row["probe"] = re.findall(r"SYSTRAP_PROBE arch=(\w+) auxv=(\d+) sysconf=(\d+) pid=(\d+)", log)
                row["sentries"] = re.findall(r"PAIRED_SENTRY sha256=([0-9a-f]{64})", log)
                row["exit"] = re.findall(r"PAIRED_EXIT status=(\d+)", log)
                row["guest_pages"] = re.findall(r"Guest page size: (\d+)", log)
                row["signals"] = [s for s in ("mount", "permission denied", "timed out", "No space left on device",
                                  "exec format error", "unexpected EOF") if s in log and t["status"] != "PASSED"]
        kernel, pages = GUESTS[name]
        row["verified"] = (row["status"] == "PASSED" and not row["cached"] and row["strategy"] == "remote"
                           and row.get("inputs") == [(release["manifest_sha256"], "aarch64", kernel, str(pages))]
                           and row.get("probe") == [("aarch64", str(pages), str(pages), "1")]
                           and row.get("sentries") == [release["members"]["gvisor-bin/gvisor_sentry"]["sha256"]]
                           and row.get("exit") == ["0"] and str(pages) in row.get("guest_pages", [])
                           and row.get("xml_pass") is True)
        save("guests", report)
    report["verified"] = (report["original_invocation_exit"] == 0 and set(report["results"]) == set(GUESTS)
                          and all(r["verified"] for r in report["results"].values()))
    if not report["verified"]:
        log = (WORK / "remote.log").read_text(errors="replace")
        report["diagnostic_classes"] = [s for s in ("no matching execution platform", "no registered executors",
            "no such package", "no such target", "not visible", "could not resolve", "not found", "timed out",
            "does not exist", "repository mapping", "status unavailable", "disk full") if s in log.lower()]
    save("guests", report)
    return not report["verified"]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["release"]:
        build()
    elif args == ["collect"]:
        raise SystemExit(collect())
    else:
        raise SystemExit("expected release or collect")
