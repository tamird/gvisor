"""Run two original ARM64 unit payloads under the existing isolated guest rule."""

import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "guest-report"
PRODUCT = os.environ["PRODUCT"]
GUEST = os.environ["GUEST"]
OWNERS = {
    "runsc/procumounter": ("procumounter_test", "gvisor-proc-umounter",
                           {"TestUmounterFitsInOnePage", "TestUmounterIsStatic", "TestUmounter",
                            "TestUmounter/sync=true", "TestUmounter/sync=false"}),
    "runsc/specutils/safemount_test": ("safemount_test", "safemount_runner", {"TestSafeMount"}),
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class GateError(ValueError):
    """A fixed public validation diagnostic."""


def require(condition, reason):
    if not condition:
        raise GateError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def diagnostic(path):
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    patterns = {"permission_denied": r"permission denied|operation not permitted",
                "exec_format": r"exec format error", "remote_unavailable": r"UNAVAILABLE|deadline exceeded",
                "repository_fetch": r"error downloading|error fetching|checksum was",
                "analysis": r"analysis of target .* failed", "missing_input": r"no such (file|target|package)",
                "no_platform": r"no (matching|available) (execution )?platform|no matching toolchain",
                "guest_incomplete": r"guest did not report a completed test|passing guest omitted test XML"}
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def bazel(phase, command, flags, targets):
    events = TMP / (phase + ".bep")
    cmd = ["bazel", "--bazelrc=" + str(TMP / (phase + ".bazelrc")), command, *flags,
           "--jobs=20", "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(events), *targets]
    log = TMP / (phase + ".log")
    with log.open("w") as fp:
        status = subprocess.run(cmd, stdout=fp, stderr=subprocess.STDOUT).returncode
    rows = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    started = [row["started"]["uuid"] for row in rows if "started" in row]
    report[phase] = dict(command=[command, *flags, *targets], exit=status, finished=now(),
                         invocations=started, diagnostics=diagnostic(log))
    save()
    return status, rows


def add_rule(directory, load, rule):
    path = Path(directory) / "BUILD"
    path.write_text(load + "\n" + path.read_text() + "\n" + rule + "\n")


def inspect_failure():
    from cas_inputs import blob, cas, input_file, rpc

    iid = "56be66b7-f319-41fb-8134-b116395b00b5"
    original = TMP / "original.zip"
    require(sha(original.read_bytes()) == "a3bb6135712e8f150c81f10d240c3fb4ce9a20f473659e3cd91d818bceedb626", "wrong original ZIP")
    with zipfile.ZipFile(original) as z:
        prior = json.loads(z.read("receipt.json"))
    require(prior["carrier"] == "faf46cb9484718769a2ead5eafad2697d833f100" and
            prior["product_source"] == PRODUCT and prior["guest_source"] == GUEST and
            prior["guest"]["invocations"] == [iid] and prior["guest"]["exit"] == 1 and
            prior["guest"]["diagnostics"] == ["exec_format"] and
            prior["selection"]["exit"] == 0 and all(not r["results"] for r in prior["payloads"].values()),
            "wrong original failure")
    report.update(original_run=41603799702, original_artifact=13216750553, invocation=iid,
                  original_bazel_exit=1, original_guest_attempts=0, selection=prior["selection"]["actions"])
    log = base64.b64decode(rpc("GetEventLogChunk", {"invocationId": iid, "minLines": 5000})["buffer"])
    report["original_log_sha256"] = sha(log)
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.decode(errors="replace"))
    error_lines = [line for line in text.splitlines() if "exec format" in line.lower()]
    # Report fixed symbols only. The worker paths and complete commands remain private.
    tools = {"tar", "bsdtar", "bsd_tar", "sh", "bash", "env", "build_image", "build_image.sh", "run.sh",
             "zstd", "zstd_cli", "bsdtar_tool", "qemu-system-aarch64", "ld-linux-x86-64.so.2",
             "ld-linux-aarch64.so.1", "process-wrapper", "test-setup.sh", "cp", "python3"}
    def mentions(lines):
        return sorted(tool for tool in tools if any(re.search(r"(?<![a-zA-Z0-9_.-])" +
                      re.escape(tool) + r"(?![a-zA-Z0-9_.-])", line) for line in lines))
    report["console_format_mentions"] = mentions(error_lines)
    report["console_format_count"] = len(error_lines)
    report["image_builder_lines"] = sorted({int(n) for line in error_lines for n in
        re.findall(r"build_image(?:[.]sh)?: (?:line )?(\d+)", line)})
    lookup = {"invocationId": iid}
    all_exec = rpc("GetExecution", {"executionLookup": lookup}).get("execution", [])
    failed = [x for x in all_exec if x.get("status", {}).get("code", 0) or int(x.get("exitCode", 0)) != 0]
    report.update(executions=len(all_exec), failed_executions=len(failed), failures=[])
    save()
    require(0 < len(failed) <= 20, "no bounded set of original failed actions")
    for x in failed:
        digest = x["actionDigest"]
        require(re.fullmatch(r"[0-9a-f]{64}", digest["hash"]) and int(digest["sizeBytes"]) > 0, "invalid original action digest")
        label = x.get("targetLabel", "")
        owner = next((l for l, _, _ in report["selection"] if l == label), None)
        mnemonic = x.get("actionMnemonic", "")
        row = {"action":digest, "owner":owner, "mnemonic":mnemonic if mnemonic in
            {"TcgGuestImage", "TestRunner", "Genrule", "CppLink", "CppCompile", "PackageTar", "Flatten",
             "DebExtract", "DebianExtract", "Symlink", "ExpandTreeArtifact", "Tar"} else "other",
            "exit":int(x.get("exitCode", 0)), "rpc_status":x.get("status", {}).get("code", 0)}
        report["failures"].append(row)
        save()
        act = cas(digest, "Action")
        cmd = cas(act["commandDigest"], "Command")
        argv = cmd["arguments"]
        props = {p["name"]:p["value"] for p in (act.get("platform") or cmd.get("platform") or {}).get("properties", [])}
        row["platform"] = {k:v for k,v in props.items() if (k,v) in
            {("OSFamily","Linux"), ("Arch","amd64"), ("Arch","arm64"), ("workload-isolation-type","oci"),
             ("container-network","off"), ("container-network","none")}}
        if "container-image" in props:
            row["container_image_property_sha256"] = sha(props["container-image"].encode())
        script = argv[0]
        row["argv0_known"] = Path(script).name if Path(script).name in tools else None
        obtain = rpc("GetExecution", {"executionLookup":dict(lookup, executionId=x["executionId"]),
                                      "inlineExecuteResponse":True}).get("execution", [])
        require(len(obtain) == 1 and obtain[0]["actionDigest"] == digest, "action result identity mismatch")
        result = obtain[0].get("executeResponse", {}).get("result", {})
        err = b""
        if "stderrRaw" in result:
            err = base64.b64decode(result["stderrRaw"])
        elif int(result.get("stderrDigest", {}).get("sizeBytes", 0)) in range(1, 4*1024*1024):
            err = blob(result["stderrDigest"], iid)
        action_error = [l for l in err.decode(errors="replace").splitlines() if "exec format" in l.lower()]
        status_msg = x.get("status", {}).get("message", "")
        if "exec format" in status_msg.lower():
            action_error.append(status_msg)
        row.update(stderr_sha256=sha(err), action_error_mentions=mentions(action_error),
                   format_error_in_action=bool(action_error))
        save()
        if owner != "//test/rbe/tcg:rc_guest" or mnemonic != "TcgGuestImage":
            continue
        require(len(argv) >= 12 and argv[10] == "arm64" and argv[8] == "4096", "image builder argument layout changed")
        row["tools"] = {}
        for role, n in (("image_builder", 0), ("tar", 1), ("zstd", 9)):
            arg = argv[n]
            node = input_file(act["inputRootDigest"], arg)
            data = blob(node["digest"], iid)
            role_name = Path(arg).name
            row["tools"][role] = dict(argv_index=n, sha256=sha(data), size=len(data),
                elf_machine=int.from_bytes(data[18:20], "little") if data[:6] == b"\x7fELF\x02\x01" else None,
                script=data.startswith(b"#!"), exec_path_sha256=sha(arg.encode()),
                basename=role_name if role_name in tools else None,
                argument_named_on_format_error=any(arg in l or (role_name in tools and role_name in mentions([l]))
                    for l in action_error))
            save()
        tar = row["tools"]["tar"]
        row["wrong_tar_arch"] = bool(row["platform"].get("Arch") == "amd64" and tar["elf_machine"] == 183
            and row["format_error_in_action"] and tar["argument_named_on_format_error"])
    report["status"] = "BOUND_IMAGE_TAR_MISMATCH" if any(r.get("wrong_tar_arch") for r in report["failures"]) else "ORIGINAL_DIAGNOSTIC"


def recover():
    from cas_inputs import blob, fetch, rpc

    iid = "f6b847ae-7000-4bbe-adf0-e04fc051ce01"
    response = rpc("GetEventLogChunk", {"invocationId": iid, "minLines": 4000})
    raw = base64.b64decode(response["buffer"])
    require(sha(raw) == "54de98656f2076cf9d7b522679da8ff4bfc032569ad8a3220037fbf3c7f66c85",
            "different original guest console")
    log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.decode(errors="replace"))
    failed = [line for line in log.splitlines() if re.search(r"does(?:n't| not) match this configuration", line)]
    report["original_selects"] = {repo: {attr: sum(repo + "//:packages" in line and
                                                re.search(r"attribute ['\"]?" + attr + r"['\"]? in", line) is not None
                                                for line in failed) for attr in ("srcs", "deps")}
                                  for repo in ("tcg_host_tools", "tcg_arm64_host_tools", "tcg_arm64_guest")}
    report["original_failure_invocation"] = iid
    save()
    require(report["original_selects"]["tcg_host_tools"]["srcs"] > 0, "original was not the AMD64 host-tools srcs select")

    # Recover outputs of the successful original build, without another build.
    original = TMP / "original.zip"
    require(sha(original.read_bytes()) == "99a944ccc9039c43175dd69067afc0f6699e4e3f1f978b41ed953fad4590b8c3",
            "different original ZIP")
    with zipfile.ZipFile(original) as z:
        prior = json.loads(z.read("receipt.json"))
    invocation = "1ce072c4-b169-40a4-b62b-23b52f86b647"
    require(prior["carrier"] == "9c3aa589b0ef6195bd203d26ac730a8cc24c7f3d"
            and prior["product_source"] == PRODUCT and prior["guest_source"] == GUEST
            and prior["status"] == "INCOMPLETE" and prior["reason"].endswith("runsc/procumounter/private_guest_capsule.tar'")
            and prior["product"]["exit"] == 0 and prior["product"]["invocations"] == [invocation],
            "original build or guest failure differs")
    report.update(product=prior["product"], payloads=prior["payloads"],
                  original_artifact="13214038729", original_guest_failure=prior["reason"])
    save()
    for directory, (name, _, _) in OWNERS.items():
        payload = report["payloads"][name]
        require(payload["owner"] == f"//{directory}:{name}", "wrong source owner")
        label = f"//{directory}:private_guest_capsule"
        try:
            target = rpc("GetTarget", {"invocationId": invocation, "targetLabel": label, "status": 0})
        except urllib.error.HTTPError as exc:
            payload["target_lookup_http"] = exc.code
            target = {}
        files = [f for group in target.get("targetGroups", []) for t in group.get("targets", [])
                 if t.get("metadata", {}).get("label") == label for f in t.get("files", [])]
        pattern = r"bytestream://[^/]+/(?:[^/]+/)*blobs/" + payload["sha256"] + r"/([1-9][0-9]*)"
        def sizes(files):
            found = set()
            for f in files:
                if not f.get("name", "").endswith("private_guest_capsule.tar"):
                    continue
                m = re.fullmatch(pattern, f.get("uri", ""))
                if m:
                    found.add(int(m.group(1)))
                if f.get("digest", f.get("hash")) == payload["sha256"]:
                    length = f.get("length", f.get("sizeBytes", f.get("size_bytes", 0)))
                    if int(length) > 0:
                        found.add(int(length))
            return found
        matches = sizes(files)
        if not matches:
            original_inv = json.loads(fetch("api/v1/GetInvocation", {
                "selector": {"invocationId": invocation}, "includeArtifacts": True}))
            row, = original_inv["invocation"]
            require(row["id"].get("invocationId", row["id"].get("invocation_id")) == invocation,
                    "wrong original artifact list")
            matches = sizes(row.get("artifacts", []))
        require(len(matches) == 1, "could not resolve original capsule size")
        size, = matches
        payload["size"] = size
        save()
        require(0 < size <= 64 * 1024 ** 2, "original capsule exceeds 64 MiB")
        data = blob({"hash": payload["sha256"], "sizeBytes": str(size)}, invocation)
        require(len(data) == size and sha(data) == payload["sha256"], "different original capsule bytes")
        (TMP / (name + ".tar")).write_bytes(data)


def guest():
    git("checkout", "--quiet", "--detach", GUEST)
    require(git("rev-parse", "HEAD") == GUEST and not git("diff", "--name-only", "HEAD"),
            "unclean guest source")
    image_rule = Path("test/rbe/tcg/defs.bzl")
    before = '        "host_tools": attr.label(allow_single_file = True, mandatory = True),'
    old_rule = image_rule.read_text()
    require(old_rule.count(before) == 1, "image tool declaration changed")
    image_rule.write_text(old_rule.replace(before, before[:-2] + ', cfg = "exec"),'))
    # Match the Go test XML settings in the native unit run. The donor exports
    # selected test-harness variables into the private guest at this point.
    launch = Path("test/rbe/tcg/run.sh")
    old = "  # Keep the canonical harness's outputs on ext4 until the payload has exited."
    source = launch.read_text()
    require(source.count(old) == 1, "guest environment anchor changed")
    launch.write_text(source.replace(old, "  printf 'export GO_TEST_RUN_FROM_BAZEL=1 GO_TEST_WRAP_TESTV=true\\n'\n" + old))
    targets = []
    for directory, (name, _, _) in OWNERS.items():
        # This donor predates the procumounter package. Supply only its real
        # executable path to the test rule, which runs the archived binary.
        # Any attempt to build a donor substitute fails deliberately.
        if directory == "runsc/procumounter":
            path, = (p for p in report["payloads"][name]["elfs"][name] if ".runfiles/" not in p)
            relative = Path(path).relative_to(directory).as_posix()
            require(relative == "procumounter_test_/procumounter_test", "unexpected original payload path")
            Path(directory).mkdir()
            (Path(directory) / "BUILD").write_text(
                f'genrule(name = "{name}", outs = ["{relative}"], executable = True, cmd = "false")\n')
        capsule = Path(directory) / "private_guest_capsule.tar"
        shutil.copyfile(TMP / (name + ".tar"), capsule)
        require(sha(capsule.read_bytes()) == report["payloads"][name]["sha256"], "capsule substitution")
        add_rule(directory, 'load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")', f"""
arm64_tcg_test(
    name = "private_guest_test",
    payload = ":{name}",
    archive = ":private_guest_capsule.tar",
    image = "//test/rbe/tcg:rc_guest",
    host_architecture = "arm64",
    tags = [],
    size = "large",
)
""")
        targets.append(f"//{directory}:private_guest_test")
    # Read Bazel's actual configuration of the image and both test actions.
    query = 'mnemonic("TcgGuestImage|TestRunner", deps(set(' + " ".join(targets) + ')))'
    cmd = ["bazel", "--bazelrc=" + str(TMP / "guest.bazelrc"), "aquery", "--config=rbe-arm64",
           "--config=aarch64", "--include_commandline=false", "--include_artifacts=false", "--output=jsonproto", query]
    with (TMP / "selection.aquery").open("wb") as out, (TMP / "selection.log").open("wb") as err:
        rc = subprocess.run(cmd, stdout=out, stderr=err).returncode
    report["selection"] = {"exit": rc, "diagnostics": diagnostic(TMP / "selection.log")}
    save()
    require(rc == 0, "configured guest actions unavailable after image tool fix")
    graph = json.loads((TMP / "selection.aquery").read_bytes())
    labels = {str(t["id"]): "//" + t["label"].split("//")[-1] for t in graph.get("targets", [])}
    expected = {("//test/rbe/tcg:rc_guest", "TcgGuestImage"): "//tools/bazeldefs:rbe_linux_amd64",
                **{(target, "TestRunner"): "//tools/bazeldefs:rbe_linux_arm64" for target in targets}}
    actual = {(labels[str(a["targetId"])], a["mnemonic"], "//" + a.get("executionPlatform", "").split("//")[-1])
              for a in graph.get("actions", []) if (labels[str(a["targetId"])], a["mnemonic"]) in expected}
    report["selection"]["actions"] = sorted(actual)
    save()
    require(actual == {(l, m, p) for (l, m), p in expected.items()}, "unexpected configured image or test executor")
    status, events = bazel("guest", "test", ["--config=rbe-arm64", "--config=aarch64", "--keep_going",
                           "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
                           "--strategy=TestRunner=remote", "--noremote_local_fallback",
                           "--zip_undeclared_test_outputs", "--test_output=errors"], targets)
    for directory, (name, _, expected) in OWNERS.items():
        label = f"//{directory}:private_guest_test"
        records = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == label
                   and "testResult" in e]
        row = report["payloads"][name]
        row["results"] = [dict(status=r.get("status"), cachedLocally=r.get("cachedLocally", False),
                               cachedRemotely=r.get("executionInfo", {}).get("cachedRemotely", False),
                               strategy=r.get("executionInfo", {}).get("strategy")) for r in records]
        outputs = Path("bazel-testlogs") / directory / "private_guest_test/test.outputs/outputs.zip"
        if outputs.exists():
            with zipfile.ZipFile(outputs) as z:
                files = set(z.namelist())
                for path in ("host-architecture.txt", "qemu-version.txt", "guest/kernel.txt",
                             "guest/page-size.txt", "guest/exit_status"):
                    if path in files:
                        row[path] = z.read(path).decode().strip()[:500]
                if "guest/test.xml" in files:
                    xml = z.read("guest/test.xml")
                    require(len(xml) < 16000, "unexpected guest XML size")
                    (OUT / (name + ".xml")).write_bytes(xml)
                    row["xml_sha256"] = sha(xml)
                    row["cases"] = [[case.get("name"), bool(case.find("failure") is not None or
                                    case.find("error") is not None or case.find("skipped") is not None
                                    or case.get("status") in {"notrun", "skipped"})]
                                    for case in ET.fromstring(xml).iter("testcase")]
        results = row["results"]
        row["verified"] = (len(results) == 1 and results[0]["status"] == "PASSED" and
            not results[0]["cachedLocally"] and not results[0]["cachedRemotely"] and
            results[0]["strategy"] in (None, "remote") and row.get("host-architecture.txt") == "aarch64" and
            "QEMU emulator version" in row.get("qemu-version.txt", "") and
            "7.3.0-070300rc3-generic" in row.get("guest/kernel.txt", "") and "aarch64" in row.get("guest/kernel.txt", "") and
            row.get("guest/page-size.txt") == "Guest page size: 4096" and row.get("guest/exit_status") == "0" and
            sorted(row.get("cases", [])) == sorted([x, False] for x in expected))
        save()
    require(status == 0 and all(v["verified"] for v in report["payloads"].values()), "guest gates incomplete")


OUT.mkdir(exist_ok=True)
report = dict(status="INCOMPLETE", carrier=os.environ["GITHUB_SHA"], product_source=PRODUCT,
              guest_source=GUEST, prior_host_run="41589548481", started=now())
try:
    if sys.argv[1:] == ["inspect-failure"]:
        inspect_failure()
    elif sys.argv[1:] == ["recover"]:
        save()
        recover()
    else:
        require(sys.argv[1:] == ["guest"], "unexpected phase")
        data = json.loads((OUT / "receipt.json").read_text())
        require(data["carrier"] == report["carrier"] and data["original_artifact"] == "13214038729",
                "wrong recovery receipt")
        report.update(data)
        guest()
        report["status"] = "PASS"
except Exception as exc:
    report["reason"] = str(exc) if isinstance(exc, GateError) else type(exc).__name__
    if isinstance(exc, urllib.error.HTTPError):
        report["http_status"] = exc.code
    print(report["reason"])
    raise SystemExit(1) from None
finally:
    report["verified_at"] = now()
    save()
