"""Project the original source-presubmit BEP into public, typed results."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

PRODUCT = "a39f38e89dc7110a5bc35cd70821388d2d22f62a"
COMPILER = "f6df263f8e2ce2cbe6a497356613f6785c4800a4"
BASELINE = "127967d3179491381f2a85e4804ae8236fc89e35"
LABEL = re.compile(r"//[A-Za-z0-9_.+/-]*:[A-Za-z0-9_.+/-]+")
CONTROL = "//test/syscalls:uname_test_native"
TMP = Path(os.environ["RUNNER_TEMP"])
SEP = TMP / "source-presubmit-results"
CAS = re.compile(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/([0-9]+)$")
SUFFIX = "_presubmit_arm64"
CMD = "//runsc/cmd:cmd_test"
NET = "//runsc/gofer/net:netgofer_test"
ABI = "//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test"
LIVE = "//pkg/sentry/devices/nvproxy:nvproxy_driver_checksums_test"
DOC = "//examples/sandboxexec/go/docprocessor:docprocessor_test"
PYTHON = "//sandboxexec/sandbox/python:sandbox_py_test"
TEST_STATUS = {"NO_STATUS", "PASSED", "FLAKY", "TIMEOUT", "FAILED", "INCOMPLETE", "REMOTE_FAILURE",
               "FAILED_TO_BUILD", "TOOL_HALTED_BEFORE_TESTING"}
STRATEGIES = {"remote", "remote cache hit", "local", "processwrapper-sandbox", "linux-sandbox", "worker"}
REASONS = {"UNKNOWN", "USER_INTERRUPTED", "NO_ANALYZE", "NO_BUILD", "TIME_OUT", "REMOTE_ENVIRONMENT_FAILURE",
           "INTERNAL", "LOADING_FAILURE", "ANALYSIS_FAILURE", "SKIPPED", "INCOMPLETE", "OUT_OF_MEMORY"}
MECHANISMS = {
    "missing-header": r"fatal error: .{1,210}(?:file not found|No such file or directory)",
    "undefined-reference": r"undefined (reference|symbol)",
    "strict-Go-dependency": r"missing strict dependencies",
    "tool-permission-denied": r"exec(?:ute|vp)?.{0,150}permission denied",
    "operation-not-permitted": r"operation not permitted",
    "file-not-found": r"no such file or directory|executable file not found",
    "illegal-instruction": r"illegal instruction",
    "toolchain-resolution": r"no matching (?:toolchain|execution platform)|no matching toolchains found",
    "action-command-failed": r"(?:error executing|exit status|returned non-zero)",
    "namespace-unavailable": r"(?:unshare|namespace|clone).{0,120}(?:denied|not permitted)",
    "remote-capacity": r"resource_exhausted|resource exhausted|no registered executors",
    "oom": r"out of memory|outofmemoryerror|OOMKilled",
    "credential-denied": r"unauthenticated|permission_denied",
    "deadline": r"deadline_exceeded|deadline exceeded|timed out",
    "repository-download": r"error downloading|PKIX path building failed",
    "invalid-visibility": r"visibility error|is not visible from",
    "local-or-remote-strategy": r"no usable spawn strategy|no spawn strategy|not a valid strategy",
    "disk-exhausted": r"no space left on device|disk quota exceeded",
    "killed": r"killed by signal|killed \(signal [0-9]+\)|signal: killed",
    "linker-not-found": r"cannot find (?:-l|linker)|unable to find library",
    "undefined-unwind": r"_Unwind_|__gcc_personality",
    "unsupported-clang-option": r"unsupported option|unknown argument|unused command-line argument",
    "compiler-cannot-execute": r"unable to execute command|failed to execute.*(?:clang|gcc)",
    "wrong-executable-format": r"exec format error|wrong ELF class",
    "compiler-error": r"error: (?:undeclared|use of|incompatible|cannot|unknown|unexpected|invalid|implicit declaration)",
    "build-aborted": r"analysis of target .{1,180} failed",
}
KNOWN_TOOLS = ["bash", "sh", "python", "python3", "ld", "gcc", "g++", "clang", "ar", "ld.lld",
               "git", "env", "dirname", "realpath", "grep", "as", "gcc-13", "cc", "cat", "ls", "cp"]
KNOWN_HEADERS = ["stdio.h", "stdint.h", "stddef.h", "stdlib.h", "string.h", "sys/types.h", "unistd.h",
                 "features.h", "pthread.h", "libc-header-start.h", "limits.h", "errno.h", "asm/types.h",
                 "asm/poll.h", "libunwind.h", "asm/unistd.h", "bits/wordsize.h", "linux/types.h", "bits/libc-header-start.h"]


def digest_item(loc):
    uri = loc.get("uri", "")
    match = CAS.search(uri)
    if match: return [match[1], int(match[2])]
    h = loc.get("digest")
    return [h, int(loc.get("length",0))] if isinstance(h,str) and re.fullmatch("[a-f0-9]{64}",h) else None


def enums(value, allowed):
    return value if value in allowed else "other"


def cfg(value):
    return value if isinstance(value,str) and re.fullmatch("[0-9a-f]{64}",value) else None


def public_label(value, roots):
    if value in roots:
        return value
    # A transitive failure can belong to another repository. Its label is not
    # in the exact-source root oracle. Keep its identity as a digest only.
    if value:
        return "sha256-" + hashlib.sha256(value.encode()).hexdigest()
    return None


def signal(line):
    known = [k for k, pattern in MECHANISMS.items() if re.search(pattern, line, re.I)]
    if not known:
        return None
    out = {"kind": known}
    # Never emit arbitrary excerpts from console, command lines or test output.
    libs = [s for s in ("-lpthread", "-ldl", "-lunwind", "-lc++", "-lstdc++") if s in line]
    if libs:
        out["libraries"] = libs
    if "missing-header" in known or "file-not-found" in known:
        out["headers"] = [h for h in KNOWN_HEADERS if h in line]
        out["tools"] = [t for t in KNOWN_TOOLS if re.search(r"(?:^|[/ '\"(])"+re.escape(t)+r"(?=[: '\"),]|$)",line)]
    return out


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == PRODUCT
    SEP.mkdir(exist_ok=True)
    sel_path=TMP/"llvm-source-selection.json"
    selection=json.loads(sel_path.read_text()) if sel_path.is_file() else {}
    assert not selection or selection["source"]==PRODUCT
    full=selection.get("all",{}); tests=selection.get("tests",{})
    assert all(LABEL.fullmatch(k) and cfg(v) for table in (full,tests) for k,v in table.items())
    exact=bool(full and tests and tests.keys()<=full.keys())
    # Even if the independent oracle fails, retain the full execution and mark its coverage provisional.
    roots={k:("run" if k in tests else "build") for k in full} if exact else {}
    expected_shards=selection.get("shards",{})
    assert all(k in tests and isinstance(n,int) and 1<=n<=256 for k,n in expected_shards.items())
    runnable=sum(v=="run" for v in roots.values())
    result={"source":PRODUCT,"compiler":COMPILER,"baseline":BASELINE,"analysis_baseline_artifact":13553917910,
            "expected":{"runnable":runnable,"build_only":len(roots)-runnable},"command":"bazel test --config=source-presubmit-mixed",
            "complete_make_selection":exact,"make_patterns":selection.get("public_patterns",[]),
            "selection_phases":selection.get("phases",{}),"shard_oracle":bool(expected_shards),
            "jobs":64,"keep_going":True,"requested_fresh_tests":True,"target_results":{},"shards":[],
            "test_summaries":{},"failed_actions":[],"aborts":[],"console_signals":[],"unexpected_terminals":[]}
    result["expected_per_owner_shards"]=expected_shards
    result["configured_firecracker_owners"]=selection.get("firecracker_owners",[])
    workers = selection.get("test_workers", {})
    assert all(k in tests and v in ("amd64", "arm64") for k,v in workers.items())
    result["configured_test_workers"]=workers
    result["configured_test_checksums"]=selection.get("test_configurations",{})
    locations_by_row=[]
    rcfile=TMP/"source-presubmit-exit"
    mem = TMP/"source-presubmit-memory.json"
    result["coordinator_memory"] = json.loads(mem.read_text()) if mem.is_file() else None
    result["process_exit"] = int(rcfile.read_text()) if rcfile.exists() else None
    bep=TMP/"source-presubmit.bep"
    if bep.is_file():
        raw_digest=hashlib.sha256()
        with bep.open("rb") as events:
            for raw in events:
                raw_digest.update(raw)
                try:
                    event=json.loads(raw)
                except json.JSONDecodeError:
                    result["incomplete_bep_line"]=True
                    break
                ident=event.get("id",{})
                if "started" in event and event["started"].get("uuid"):
                    result["invocation"]=str(uuid.UUID(event["started"]["uuid"]))
                if "finished" in event:
                    exit_info=event["finished"].get("exitCode",{})
                    result["bazel_exit"]=int(exit_info.get("code",0))
                    result["bazel_failure_codes"]=failure_codes(event["finished"])
                completed=ident.get("targetCompleted")
                if completed is not None and not completed.get("aspect"):
                    label=completed["label"]
                    if label not in roots and event.get("aborted",{}).get("reason")!="SKIPPED":
                        if LABEL.fullmatch(label) and len(result["unexpected_terminals"])<512:
                            result["unexpected_terminals"].append(label)
                        if not exact and LABEL.fullmatch(label):
                            roots[label]="unknown"
                if completed is not None and not completed.get("aspect") and completed["label"] in roots:
                    label=completed["label"]
                    if "completed" in event:
                        status="built" if event["completed"].get("success",False) else "failed"
                    else:
                        status=enums(event.get("aborted",{}).get("reason","UNKNOWN"),REASONS)
                    result["target_results"][label]=[status,cfg(completed.get("configuration",{}).get("id")),roots[label]]
                if "testResult" in event:
                    rid=ident["testResult"]
                    label=rid["label"]
                    assert LABEL.fullmatch(label)
                    if roots.get(label)!="run" and label not in result["unexpected_terminals"]:
                        result["unexpected_terminals"].append(label)
                    data=event["testResult"]
                    info=data.get("executionInfo",{})
                    locations={o["name"]:o for o in data.get("testActionOutput",[]) if o.get("name") in ("test.xml","test.log")}
                    locations_by_row.append(locations)
                    result["shards"].append(dict(owner=label,config=cfg(rid.get("configuration",{}).get("id")),
                        run=int(rid.get("run",1)),shard=int(rid.get("shard",1)),attempt=int(rid.get("attempt",1)),
                        status=enums(data.get("status"),TEST_STATUS),strategy=enums(info.get("strategy"),STRATEGIES),
                        cached_locally=bool(data.get("cachedLocally")),cached_remotely=bool(info.get("cachedRemotely")),
                        action_exit=info.get("exitCode",0) if info else None,
                        status_signal=signal(data.get("statusDetails","")),
                        xml=digest_item(locations.get("test.xml",{})),log=digest_item(locations.get("test.log",{}))))
                if "testSummary" in event and ident["testSummary"]["label"] in roots:
                    data=event["testSummary"]
                    result["test_summaries"][ident["testSummary"]["label"]]=[enums(data.get("overallStatus"),TEST_STATUS),int(data.get("totalRunCount",0))]
                if "action" in event and not event["action"].get("success",False) and len(result["failed_actions"])<160:
                    data=event["action"]; aid=ident.get("actionCompleted",{}); typ=data.get("type","")
                    # Only Bazel's action mnemonic is accepted, never the command or stdout.
                    mnemonic=typ if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,65}",typ) else "other"
                    reason=signal(str(data.get("failureDetail",{})))
                    result["failed_actions"].append(dict(owner=public_label(aid.get("label"),roots),mnemonic=mnemonic,
                        exit=int(data.get("exitCode",0)),stderr=digest_item(data.get("stderr",{})),signal=reason,
                        config=cfg(aid.get("configuration",{}).get("id")),codes=failure_codes(data)))
                if "aborted" in event and len(result["aborts"])<100:
                    diag=event["aborted"];k=next(iter(ident),"");sub=ident.get(k,{})
                    row=[public_label(sub.get("label") if isinstance(sub,dict) else None,roots),
                         enums(diag.get("reason","UNKNOWN"),REASONS),signal(diag.get("description",""))]
                    if row not in result["aborts"]:
                        result["aborts"].append(row)
        result["raw_bep_sha256"]=raw_digest.hexdigest()
    log=TMP/"source-presubmit.log"
    if log.is_file():
        tracked = set(subprocess.check_output(["git","ls-files","-z"]).decode().split("\0"))
        locations = []
        with log.open(errors="replace") as lines:
            for line in lines:
                s=signal(line)
                if (s or "ERROR:" in line or "error:" in line) and len(locations) < 24:
                    for match in re.finditer(r"([-A-Za-z0-9_.+/]+):([0-9]{1,8})(?::([0-9]{1,6}))?",line):
                        parts=match[1].strip("/").split("/")
                        path=next(("/".join(parts[i:]) for i in range(len(parts)) if "/".join(parts[i:]) in tracked),None)
                        if path:
                            row=[path,int(match[2]),int(match[3]) if match[3] else None,s]
                            if row not in locations:
                                locations.append(row)
                if s and s not in result["console_signals"] and len(result["console_signals"])<48:
                    result["console_signals"].append(s)
        result["diagnostic_locations"]=locations
    result["target_counts"]=dict(collections.Counter("/".join([r[2],r[0]]) for r in result["target_results"].values()))
    result["result_counts"]=dict(collections.Counter("/".join((s["status"],s["strategy"], "cached" if s["cached_locally"] or s["cached_remotely"] else "fresh")) for s in result["shards"]))
    result["missing_target_count"]=len(roots.keys()-result["target_results"].keys())
    result["owners_with_test_result"]=len({s["owner"] for s in result["shards"]})
    by_owner=collections.Counter(s["owner"] for s in result["shards"])
    def current(owner):
        if exact and owner not in roots: return "removed"
        terminal=result["target_results"].get(owner)
        if not terminal: return "no-terminal"
        if terminal[0]!="built": return terminal[0]
        if roots.get(owner)!="run": return "built"
        if not by_owner[owner]: return "built-no-test"
        return result["test_summaries"].get(owner,["ran-no-summary"])[0]
    result["test_dispositions"]={l:current(l) for l in roots if roots[l]=="run"}
    result["unbuilt_or_unreported"]={l:(result["target_results"].get(l) or ["no-terminal"])[0] for l in roots
        if (result["target_results"].get(l) or ["no-terminal"])[0]!="built"}
    checksums=result["configured_test_checksums"]
    result["matching_shard_configurations"]=sum(bool(checksums.get(r["owner"])) and r["config"]==checksums[r["owner"]] for r in result["shards"])
    collect_outputs(result, locations_by_row)
    result["expected_shards"]=sum(expected_shards.values()) if expected_shards else None
    result["missing_shards"]={l:n-by_owner[l] for l,n in expected_shards.items() if n!=by_owner[l]}
    coverage = ("//runsc:runsc_coverage", "//runsc:runsc_race_coverage",
                "//runsc/cmd/sentry:gvisor_sentry_coverage", "//runsc/cmd/sentry:gvisor_sentry_race_coverage")
    sandbox_tests = ("//examples/sandboxexec/go/docprocessor:docprocessor_test",
                     "//sandboxexec/sandbox/python:sandbox_py_test")
    result["previous_six_failures"] = {label: current(label) for label in (*coverage, *sandbox_tests)}
    seen_shards={(s["owner"],s["run"],s["shard"]) for s in result["shards"]}
    result["all_fresh_remote"]=bool(result["shards"]) and all(
        s["strategy"]=="remote" and not s["cached_locally"] and not s["cached_remotely"] for s in result["shards"])
    result["all_shard_configs_joined"]=bool(result["shards"]) and result["matching_shard_configurations"]==len(result["shards"])
    expected_ids={(owner,1,i) for owner,n in expected_shards.items() for i in range(1,n+1)}
    result["missing_shard_ids"] = sorted(expected_ids - seen_shards)
    result["unexpected_shard_ids"] = sorted(seen_shards - expected_ids) if expected_ids else []
    result["duplicate_test_results"] = len(result["shards"]) - len(seen_shards)
    result["configuration_mismatches"] = [owner for owner,terminal in result["target_results"].items()
                                          if owner in full and terminal[1] != full[owner]]
    result["complete_execution_receipts"] = bool(exact and tests.keys() == expected_shards.keys() == workers.keys()
        and len(tests)==704 and len(expected_ids)==724 and seen_shards==expected_ids
        and result["all_shard_configs_joined"] and result["all_fresh_remote"]
        and not result["configuration_mismatches"] and not result["missing_target_count"]
        and not result.get("incomplete_bep_line") and "bazel_exit" in result)
    result["complete_pass"]=bool(result["complete_execution_receipts"] and result["named_cases"].get("verified") and result.get("bazel_exit")==0 and result["process_exit"]==0
        and not result["missing_target_count"] and not result["missing_shards"] and not result["unexpected_terminals"]
        and result["owners_with_test_result"]==runnable and len(result["test_summaries"])==runnable
        and all(v[0]=="PASSED" for v in result["test_summaries"].values())
        and all(v[0]=="built" for v in result["target_results"].values()) and len(seen_shards)==len(result["shards"])
        and all(s["status"]=="PASSED" and s["strategy"]=="remote" and not s["cached_locally"] and not s["cached_remotely"] for s in result["shards"]))
    output=json.dumps(result,sort_keys=True,separators=(",",":")).encode()
    assert len(output)<3000000 and not re.search(rb"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",output)
    (SEP/"receipt.json").write_bytes(output+b"\n")
    print(json.dumps({k:result.get(k) for k in ("source","invocation","bazel_exit","target_counts","result_counts","owners_with_test_result","missing_target_count","complete_pass")},sort_keys=True))


def failure_codes(data):
    detail = data.get("failureDetail", {})
    return sorted({v["code"] for v in detail.values() if isinstance(v, dict)
                   and isinstance(v.get("code"), str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", v["code"])})


def source_cases():
    def go_functions(path):
        return re.findall(r"^func (Test\w+)\(t \*testing.T\)", Path(path).read_text(), re.M)

    versions = dict(re.findall(r'^\s*"(\d+[.]\d+[.]\d+)": (None|\()',
                               Path("tools/nvidia_driver_differ/driver_pins.bzl").read_text(), re.M))
    return {
        NET: {n:"PASS" for n in go_functions("runsc/gofer/net/netgofer_test.go")},
        ABI: {"TestStructDefinitionParity/"+v: "SKIP" if entry == "None" else "PASS" for v, entry in versions.items()},
        LIVE: {"TestDriverChecksums/"+v: "PASS" for v in versions},
        CONTROL: {n:"PASS" for n in ("Sanity", "SetNames", "UnprivilegedSetNames", "UnshareUTS")},
        DOC: {n:None for n in go_functions("examples/sandboxexec/go/docprocessor/docprocessor_test.go")
              + ["TestProcessDocuments_NetworkModes/host", "TestProcessDocuments_NetworkModes/none"]},
    }


POSTURE_CASE = re.compile(
    r"TestBootPosture(?:/(?:default|hostinet|no_directfs|no_directfs_hostinet|fdlimit|kvm|coretags)(?:/(?:sentry|gofer))?)?")
POSTURE_PHASES = {
    "feature-unavailable":"kernel cannot report", "uid-unmapped":"UID 65534 is not mapped",
    "sentry-not-traversable":"cannot be executed by an unprivileged user",
    "kvm-unavailable":"Cannot open /dev/kvm", "coretags-unavailable":"does not support core scheduling",
    "namespace-required":"network namespace of its own", "setup-container":"setting up container",
    "create-container":"creating container", "start-container":"starting container",
    "describe-reference":"describing the reference process", "collect-posture":"collecting posture",
}


def posture_phases(message):
    return [k for k, fragment in POSTURE_PHASES.items() if fragment.lower() in message.lower()]


def safe_name(name):
    if (re.fullmatch(r"[A-Za-z0-9_.:/=,+@#~%-]{1,220}", name)
            and not re.search(r"(?i)gh[pousr]_|github_pat_|x-buildbuddy-api-key|authorization:|key=|password=|token=|secret=", name)):
        return name
    return "sha256-" + hashlib.sha256(name.encode()).hexdigest()


def collect_outputs(result, locations_by_row):
    outputs = [p.resolve() for p in Path("bazel-out").glob("*/testlogs") if p.is_dir()]
    candidates = {}
    expected = source_cases()
    # Bound public name bytes, retaining counts and immutable hashes for every shard.
    budget = {"failed": 96000, "skipped": 300000}
    named = {}
    xml_rows = 0

    def original(row, locations, name, limit):
        expected_digest = row["xml" if name == "test.xml" else "log"]
        if not expected_digest or not 0 < expected_digest[1] <= limit:
            return None
        owner = row["owner"]
        key = owner, name
        relative = owner[2:].replace(":", "/")
        if key not in candidates:
            paths = []
            for tree in outputs:
                base = tree / relative
                if base.is_dir():
                    for prefix in ("", "shard_*_of_*/", "run_*_of_*/", "shard_*_of_*/run_*_of_*/",
                                   "run_*_of_*/shard_*_of_*/"):
                        paths.extend(base.glob(prefix + name))
                        paths.extend(base.glob(prefix + "test_attempts/attempt_*" + Path(name).suffix))
            candidates[key] = paths
        paths = list(candidates[key])
        uri = urlparse(locations.get(name, {}).get("uri", ""))
        if uri.scheme == "file" and not uri.netloc:
            path = Path(unquote(uri.path)).resolve()
            if any(path.is_relative_to(tree / relative) for tree in outputs):
                paths.insert(0, path)
        for path in dict.fromkeys(paths):
            try:
                if not path.is_file() or path.stat().st_size != expected_digest[1]:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(raw).hexdigest() == expected_digest[0]:
                return raw
        return None

    for row, locations in zip(result["shards"], locations_by_row):
        owner = row["owner"]
        base = owner.removesuffix(SUFFIX)
        special = base in expected or base in (CMD, PYTHON)
        evidence = {}
        observed = {}
        outcomes = {}
        xml = original(row, locations, "test.xml", 10 * 1024 * 1024)
        if xml:
            try:
                document = ET.fromstring(xml)
            except ET.ParseError:
                row["xml_case_counts"] = {"parsed": False, "original_digest_matched": True}
            else:
                counts = collections.Counter()
                names = {"failed": [], "skipped": []}
                for case in document.iter("testcase"):
                    state = "passed"
                    if case.find("error") is not None:
                        state = "error"
                    elif case.find("failure") is not None:
                        state = "failed"
                    elif (case.find("skipped") is not None or case.get("status") in ("notrun", "skipped")
                          or case.get("result") in ("skipped", "suppressed")):
                        state = "skipped"
                    counts[state] += 1
                    name = case.get("name", "")
                    class_name = case.get("classname", "")
                    if state != "passed":
                        bucket = "skipped" if state == "skipped" else "failed"
                        ident = safe_name((class_name + "." + name).lstrip("."))
                        if len(names[bucket]) < 128 and budget[bucket] >= len(ident):
                            names[bucket].append(ident)
                            budget[bucket] -= len(ident)
                    status = {"passed":"PASS","skipped":"SKIP","error":"FAIL","failed":"FAIL"}[state]
                    short = name.removeprefix("cmd.")
                    if base == CMD and POSTURE_CASE.fullmatch(short):
                        outcomes[short] = {"state": status}
                        if status != "PASS":
                            phases = set(posture_phases(" ".join(case.itertext())))
                            for node in case:
                                phases.update(posture_phases(node.get("message", "")))
                            outcomes[short]["phases"] = sorted(phases)
                    if base == CONTROL:
                        if "UnameTest" in class_name + name:
                            short = re.split(r"[./]", name)[-1]
                        else:
                            continue
                    if short in expected.get(base, {}):
                        observed[short] = status
                xml_rows += 1
                row["xml_case_counts"] = {
                    "original_digest_matched": True, "counts": dict(counts),
                    "failed_names": names["failed"], "skipped_names": names["skipped"],
                    "omitted_failed_names": counts["error"] + counts["failed"] - len(names["failed"]),
                    "omitted_skipped_names": counts["skipped"] - len(names["skipped"]),
                }
        if not special:
            continue
        raw = original(row, locations, "test.log", 16 * 1024 * 1024)
        if raw:
            text = raw.decode(errors="replace")
            for status, name in re.findall(r"(?m)^\s*--- (PASS|SKIP|FAIL): (\S+?) \(", text):
                if name in expected.get(base, {}) and observed.get(name, status) != status:
                    observed[name] = "CONFLICT"
                elif name in expected.get(base, {}):
                    observed[name] = status
                if base == CMD and POSTURE_CASE.fullmatch(name):
                    prior = outcomes.setdefault(name, {"state":status})
                    if prior["state"] != status:
                        prior["state"] = "CONFLICT"
            if base == CMD:
                evidence["package_markers"] = re.findall(r"(?m)^\x16?(PASS|FAIL)\r?$", text)[:32]
                evidence["diagnostic_phases"] = posture_phases(text)
            if base == PYTHON:
                ran = re.search(r"(?m)^Ran (\d+) tests? in ", text)
                ok = re.search(r"(?m)^OK(?: \(skipped=(\d+)\))?\s*$", text)
                evidence.update(ran=int(ran[1]) if ran else None, ok=bool(ok),
                                skipped=int(ok[1] or 0) if ok else None)
            if base == NET:
                runs = re.findall(r"(?m)^=== RUN\s+(\S+)", text)
                evidence["last_started_test"] = next((n for n in reversed(runs) if n in expected[NET]), None)
            evidence["log_digest_matched"] = True
        if base in expected:
            required = expected[base]
            evidence.update(observed=observed, missing=sorted(required.keys() - observed.keys()),
                            verified=bool(required) and required.keys() == observed.keys()
                            and all(observed[k] in ("PASS", "SKIP") if v is None else observed[k]==v for k,v in required.items()))
        if base == CMD:
            evidence["posture"] = outcomes
        evidence.update(run=row["run"], shard=row["shard"], attempt=row["attempt"])
        named.setdefault(owner, []).append(evidence)
    required_owners = {b+s for b in (NET, ABI, LIVE, CONTROL) for s in ("", SUFFIX)}
    result["named_cases"] = {"owners": named, "verified": all(
        len(named.get(owner, [])) == 1 and named[owner][0].get("verified") for owner in required_owners)}
    result["shards_with_verified_xml"] = xml_rows


if __name__ == "__main__":
    main()
