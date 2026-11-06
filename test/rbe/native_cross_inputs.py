"""Compare current native C consumers with their original shell producers."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import struct
import subprocess
import sys

BASE = "90d4e2253f004cdc73a35dbc4050eab3e9a8495d"
NATIVE = "5fa51a2fc3eee86b3ecf12abb8c043481672daff"
NO_LEGACY = "105abcdc9d2d7a00000e6fe5dc2dd71af7987254"
TASK = Path(os.environ["RUNNER_TEMP"]) / "native-cross-inputs"
BAZEL = str(TASK / "bazel")
SYSMSG = "//pkg/sentry/platform/systrap/sysmsg:"
ROOTS = ["//pkg/sentry/loader/vdsodata:vdso_arch", SYSMSG + "sighandler_binary_arch", SYSMSG + "sighandler_go_arch"]
FLAGS = ["--extra_toolchains=@llvm//toolchain:all", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
QUERY = 'mnemonic("CppCompile|CppLink|Genrule", deps(set(' + ' '.join(ROOTS) + ')))'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(args, timeout=120):
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
            return 124, out, err


def diagnostics(data):
    lines = []
    for line in data.decode(errors="replace").splitlines():
        if re.search(r"error|fatal|failed|cannot|could not|undefined|unable to|no such|not found|unrecognized|unknown argument", line, re.I):
            line = re.sub(r"https?://\S+", "<url>", line)
            line = re.sub(r"/(?:home|tmp|private|opt)/[^\s:]+", "<host-path>", line)
            lines.append(line[:500])
    return {"bytes": len(data), "sha256": digest(data), "lines": lines[:10]}


def owned(label):
    return label.startswith(SYSMSG) or label.startswith("//vdso:")


def graph(raw):
    g = json.loads(raw)
    fragments = {int(x["id"]): x for x in g["pathFragments"]}
    def path(i):
        x = fragments[int(i)]
        return (path(x["parentId"]) + "/" if x.get("parentId") else "") + x["label"]
    artifacts = {int(x["id"]): path(x["pathFragmentId"]) for x in g["artifacts"]}
    owners = {int(x["id"]): x["label"] for x in g["targets"]}
    configs = {int(x["id"]): x for x in g.get("configuration", [])}
    rows = []
    for a in g.get("actions", []):
        label = owners[int(a["targetId"])]
        if not owned(label):
            continue
        args = list(a.get("arguments", []))
        args.extend(v for p in a.get("paramFiles", []) for v in p.get("arguments", []))
        c = configs.get(int(a["configurationId"]), {})
        triples = []
        for i, arg in enumerate(args):
            if arg.startswith(("--target=", "-target=")):
                triples.append(arg.split("=", 1)[1])
            elif arg in ("--target", "-target") and i + 1 < len(args):
                triples.append(args[i + 1])
        arches = {t.split("-", 1)[0] for t in triples if re.fullmatch(r"(?:aarch64|x86_64)(?:-[a-z0-9_]+)*-linux-(?:gnu|musl)", t)}
        target = next(iter(arches)) if len(arches) == 1 else None
        outputs = [artifacts[int(x)] for x in a["outputIds"]]
        # Preserve the command of these fixed public C/assembly producers only.
        assert len(args) < 500 and sum(map(len, args)) < 50000
        def normalize(s):
            return re.sub(r"bazel-out/[^/]+/", "bazel-out/<config>/", s)
        signature = [label, a["mnemonic"], target, [normalize(s) for s in args], [normalize(s) for s in outputs]]
        rows.append({"owner": label, "mnemonic": a["mnemonic"], "target_arch": target, "target_triples": sorted(set(triples)), "configuration": c.get("checksum"), "outputs": outputs, "argv": args, "normalized_sha256": digest(json.dumps(signature).encode())})
    assert len(rows) <= 64
    return rows


def events(path):
    r = {"actions": [], "targets": [], "aborted": [], "test_events": 0}
    if not path.exists():
        return r
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        key = e.get("id", {})
        if "started" in key:
            r["invocation"] = e.get("started", {}).get("uuid")
        if "buildFinished" in key:
            r["exit"] = e.get("finished", {}).get("exitCode")
        if "testResult" in key or "testSummary" in key:
            r["test_events"] += 1
        a = key.get("actionCompleted", {})
        result = e.get("action", {})
        if a and (owned(a.get("label", "")) or not result.get("success")) and len(r["actions"]) < 64:
            r["actions"].append({"owner": a.get("label"), "output": a.get("primaryOutput"), "configuration": a.get("configuration", {}).get("id"), "type": result.get("type"), "success": result.get("success", False), "exit": result.get("exitCode", 0), "failure_codes": {k: v.get("code") for k, v in result.get("failureDetail", {}).items() if isinstance(v, dict) and "code" in v}})
        t = key.get("targetCompleted", {})
        if t.get("label") in ROOTS and not t.get("aspect"):
            r["targets"].append({"owner": t["label"], "configuration": t.get("configuration", {}).get("id"), "success": e.get("completed", {}).get("success", False)})
        if "aborted" in e and len(r["aborted"]) < 8:
            r["aborted"].append({"reason": e["aborted"].get("reason"), "description": diagnostics(e["aborted"].get("description", "").encode())})
    return r


def read_file(root, path):
    rel = Path(path)
    assert not rel.is_absolute() and ".." not in rel.parts and rel.parts[0] == "bazel-out"
    p = root / rel
    assert p.stat().st_size < 10000000
    return p.read_bytes()


def elf(raw):
    assert raw[:6] == b"\x7fELF\x02\x01"
    phoff = struct.unpack_from("<Q", raw, 32)[0]
    phsize, phcount = struct.unpack_from("<HH", raw, 54)
    types = [struct.unpack_from("<I", raw, phoff + i * phsize)[0] for i in range(phcount)]
    shoff = struct.unpack_from("<Q", raw, 40)[0]
    shsize, shcount, shstr = struct.unpack_from("<HHH", raw, 58)
    sections = [struct.unpack_from("<IIQQQQIIQQ", raw, shoff + i * shsize) for i in range(shcount)]
    names = sections[shstr]
    strings = raw[names[4]:names[4] + names[5]]
    def cstring(data, offset):
        return data[offset:].split(b"\0", 1)[0].decode()
    by_name = {cstring(strings, s[0]): s for s in sections}
    result = {"bytes": len(raw), "sha256": digest(raw), "machine": struct.unpack_from("<H", raw, 18)[0], "type": struct.unpack_from("<H", raw, 16)[0], "interpreter": 3 in types}
    exports = {}
    for s in sections:
        if s[1] != 2:
            continue
        strs = sections[s[6]]
        table = raw[strs[4]:strs[4] + strs[5]]
        for off in range(s[4], s[4] + s[5], s[9]):
            name, info, other, section, value, size = struct.unpack_from("<IBBHQQ", raw, off)
            name = cstring(table, name)
            if section and name.startswith("__export_"):
                exports[name.replace(".", "_")] = value
    blob = None
    if ".crblob" in by_name:
        s = by_name[".crblob"]
        assert s[3] == 0 and s[2] & 2
        assert not any(x != s and x[2] & 2 and x[5] for x in sections)
        blob = raw[s[4]:s[4] + s[5]]
    return result, blob, exports


def output_proof(root, paths, rows, ev):
    result = {"linked": [], "staged": [], "consumer_matches": {}}
    stubs = {}
    for row in rows:
        if row["mnemonic"] != "CppLink":
            continue
        for path in row["outputs"]:
            if not path.endswith(("/vdso.so", "/sighandler.built-in.bin.o")):
                continue
            item = {"owner": row["owner"], "target_arch": row["target_arch"], "path": path}
            result["linked"].append(item)
            try:
                raw = read_file(root, path)
                meta, blob, exports = elf(raw)
                item.update(meta)
                item["fresh_action"] = any(a["success"] and a["owner"] == row["owner"] and a["type"] == "CppLink" and a["output"] == path and a["configuration"] == row["configuration"] for a in ev["actions"])
                assert meta["machine"] == {"x86_64": 62, "aarch64": 183}[row["target_arch"]] and not meta["interpreter"]
                if path.endswith("/vdso.so"):
                    assert meta["type"] == 3
                if blob is not None:
                    stubs[meta["machine"]] = (blob, exports)
            except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
                item["read_error"] = type(ex).__name__ + ": " + str(ex)[:180]
    for path in paths:
        item = {"path": path}
        result["staged"].append(item)
        try:
            raw = read_file(root, path)
            item.update(bytes=len(raw), sha256=digest(raw))
            arch = "amd64" if ("amd64" in Path(path).name) else "arm64" if "arm64" in Path(path).name else None
            machine = {"amd64": 62, "arm64": 183}[arch]
            if path.endswith(".so"):
                meta, _, _ = elf(raw)
                item.update(meta)
                assert meta["type"] == 3 and meta["machine"] == machine and not meta["interpreter"]
                item["linked_match"] = any(x.get("sha256") == meta["sha256"] and x.get("machine") == machine for x in result["linked"])
            elif path.endswith(".bin"):
                item["linked_blob_match"] = raw == stubs[machine][0]
            elif path.endswith(".go"):
                names = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"var Sighandler_blob_offset__(\w+) = (0x[0-9a-fA-F]+)", raw.decode())}
                item["export_values"] = names
                item["linked_exports_match"] = bool(names) and names == stubs[machine][1]
            else:
                raise ValueError("unexpected staged filename")
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            item["read_error"] = type(ex).__name__ + ": " + str(ex)[:180]
    return result


def source(sha):
    rc, _, err = command(["git", "checkout", "--quiet", "--detach", sha])
    assert rc == 0, diagnostics(err)
    assert command(["git", "diff", "--name-only", "HEAD", "--"])[1] == b""
    assert command(["git", "rev-parse", "HEAD"])[1].decode().strip() == sha


def phase(name, sha, rec, build=True, legacy=True):
    source(sha)
    flags = (["--config=aarch64"] if legacy else ["--cpu=aarch64", "--platforms=//tools/bazeldefs:linux_arm64"]) + FLAGS
    rec["flags"] = flags
    rc, out, err = command([BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", QUERY], 420)
    rec.update(aquery_exit=rc, aquery_diagnostics=diagnostics(err), actions=[])
    if rc == 0:
        try:
            rec["actions"] = graph(out)
        except (KeyError, ValueError, AssertionError) as ex:
            rec["graph_error"] = type(ex).__name__ + ": " + str(ex)[:200]
    if not build:
        return rec
    bep = TASK / (name + ".bep")
    rc, out, err = command([BAZEL, "build", *flags, "--keep_going", "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *ROOTS], 900)
    rec.update(build_exit=rc, build_diagnostics=diagnostics(err), events=events(bep))
    qr, paths, qe = command([BAZEL, "cquery", *flags, "--output=files", "set(" + " ".join(ROOTS) + ")"], 180)
    rec.update(output_query_exit=qr, output_query_diagnostics=diagnostics(qe))
    ir, root, ie = command([BAZEL, "info", "execution_root"])
    rec["execution_root_exit"] = ir
    if qr == 0 and ir == 0:
        rec["outputs"] = output_proof(Path(root.decode().strip()), paths.decode().splitlines(), rec["actions"], rec["events"])
    return rec


def main():
    assert platform.machine() == "x86_64"
    assert command([BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    result = {"worker": platform.machine(), "bazel": "9.3.1", "llvm": "0.8.24", "roots": ROOTS, "scope": "build only; no guest or TestRunner", "phases": {}}
    for name, sha, build, legacy in (("stock", BASE, True, True), ("native", NATIVE, True, True), ("without_legacy", NO_LEGACY, False, False)):
        rec = result["phases"][name] = {"source": sha, "build_requested": build}
        try:
            phase(name, sha, rec, build, legacy)
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            rec["reader_error"] = type(ex).__name__ + ": " + str(ex)[:250]
        write(result)
    native = result["phases"]["native"]
    other = result["phases"]["without_legacy"]
    result["legacy_action_comparison"] = {"both_queries_succeeded": native.get("aquery_exit") == other.get("aquery_exit") == 0, "same_normalized_actions": bool(native.get("actions")) and sorted(x["normalized_sha256"] for x in native.get("actions", [])) == sorted(x["normalized_sha256"] for x in other.get("actions", []))}
    compiles = [a for a in native.get("actions", []) if a["mnemonic"] == "CppCompile"]
    native["fresh_llvm_compiles_verified"] = bool(compiles) and {a["target_arch"] for a in compiles} == {"x86_64", "aarch64"} and all("llvm" in a["argv"][0] and any(e["success"] and e["type"] == "CppCompile" and e["owner"] == a["owner"] and e["configuration"] == a["configuration"] and e["output"] in a["outputs"] for e in native.get("events", {}).get("actions", [])) for a in compiles)
    outputs = native.get("outputs", {})
    linked = outputs.get("linked", [])
    staged = outputs.get("staged", [])
    result["native_inputs_verified"] = native.get("build_exit") == 0 and native["fresh_llvm_compiles_verified"] and len(linked) == 4 and all(x.get("fresh_action") and not x.get("read_error") for x in linked) and {x.get("machine") for x in linked} == {62, 183} and len(staged) == 6 and all(any(x.get(k) for k in ("linked_match", "linked_blob_match", "linked_exports_match")) and not x.get("read_error") for x in staged) and len(native.get("events", {}).get("targets", [])) == 3 and all(x["success"] for x in native["events"]["targets"]) and native["events"]["test_events"] == 0
    write(result)
    return 0 if result["native_inputs_verified"] else 1


def write(result):
    data = json.dumps(result, indent=2).encode()
    assert len(data) < 262144
    (TASK / "verdict.json").write_bytes(data)


if __name__ == "__main__":
    sys.exit(main())
