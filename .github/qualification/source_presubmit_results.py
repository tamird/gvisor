"""Project the original source-presubmit BEP into public, typed results."""
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

PRODUCT = "53c458937842fb431ef351c39781850f5a817234"
COMPILER = "f6df263f8e2ce2cbe6a497356613f6785c4800a4"
BASELINE = "949d4871550821371649ae873beab69ead0b36c1"
LABEL = re.compile(r"//[A-Za-z0-9_.+/-]*:[A-Za-z0-9_.+/-]+")
CONTROL = "//test/syscalls:uname_test_native"
TMP = Path(os.environ["RUNNER_TEMP"])
SEP = TMP / "source-presubmit-results"
CAS = re.compile(r"/blobs/([0-9a-f]{64})/([0-9]+)$")
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
    return [match[1], int(match[2])] if match else None


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
    with (TMP/"source-unit-oracle.tsv").open() as f:
        original={r["label"]:r["upstream"].split()[0] for r in csv.DictReader(f,delimiter="\t") if r["upstream"]!="-"}
    original[CONTROL]="run"
    assert len(original)==3242 and sum(v=="run" for v in original.values())==361
    sel_path=TMP/"llvm-source-selection.json"
    selection=json.loads(sel_path.read_text()) if sel_path.is_file() else {}
    assert not selection or selection["source"]==PRODUCT
    full=selection.get("all",{}); tests=selection.get("tests",{})
    assert all(LABEL.fullmatch(k) and cfg(v) for table in (full,tests) for k,v in table.items())
    exact=bool(full and tests and tests.keys()<=full.keys())
    # Even if the independent oracle fails, retain the full execution and mark its coverage provisional.
    roots={k:("run" if k in tests else "build") for k in full} if exact else dict(original)
    expected_shards=selection.get("shards",{})
    assert all(k in tests and isinstance(n,int) and 1<=n<=256 for k,n in expected_shards.items())
    old_failed={}
    for line in (TMP/"prior-unit-build-failures.tsv").read_text().splitlines():
        role,owner=line.split("\t")
        assert original[owner]==role
        old_failed[owner]=role
    assert collections.Counter(old_failed.values())=={"build":231,"run":66}
    runnable=sum(v=="run" for v in roots.values())
    result={"source":PRODUCT,"compiler":COMPILER,"baseline":BASELINE,"baseline_artifact":13517404379,
            "expected":{"runnable":runnable,"build_only":len(roots)-runnable},"command":"bazel test --config=source-presubmit",
            "complete_make_selection":exact,"make_patterns":selection.get("public_patterns",[]),
            "selection_phases":selection.get("phases",{}),"shard_oracle":bool(expected_shards),
            "jobs":64,"keep_going":True,"requested_fresh_tests":True,"target_results":{},"shards":[],
            "test_summaries":{},"failed_actions":[],"aborts":[],"console_signals":[],"unexpected_terminals":[]}
    result["selection_delta"]={
        "added":{k:roots[k] for k in sorted(roots.keys()-original.keys())},
        "removed":{k:original[k] for k in sorted(original.keys()-roots.keys())},
        "role_changed":{k:[original[k],roots[k]] for k in sorted(original.keys()&roots.keys()) if original[k]!=roots[k]},
    } if exact else None
    rcfile=TMP/"source-presubmit-exit"
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
                    result["shards"].append(dict(owner=label,config=cfg(rid.get("configuration",{}).get("id")),
                        run=int(rid.get("run",1)),shard=int(rid.get("shard",1)),attempt=int(rid.get("attempt",1)),
                        status=enums(data.get("status"),TEST_STATUS),strategy=enums(info.get("strategy"),STRATEGIES),
                        cached_locally=bool(data.get("cachedLocally")),cached_remotely=bool(info.get("cachedRemotely")),
                        log=next((digest_item(o) for o in data.get("testActionOutput",[]) if o.get("name")=="test.log"),None)))
                if "testSummary" in event and ident["testSummary"]["label"] in roots:
                    data=event["testSummary"]
                    result["test_summaries"][ident["testSummary"]["label"]]=[enums(data.get("overallStatus"),TEST_STATUS),int(data.get("totalRunCount",0))]
                if "action" in event and not event["action"].get("success",False) and len(result["failed_actions"])<160:
                    data=event["action"]; aid=ident.get("actionCompleted",{}); typ=data.get("type","")
                    # Only Bazel's action mnemonic is accepted, never the command or stdout.
                    mnemonic=typ if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,65}",typ) else "other"
                    reason=signal(str(data.get("failureDetail",{})))
                    result["failed_actions"].append(dict(owner=public_label(aid.get("label"),roots),mnemonic=mnemonic,
                        exit=int(data.get("exitCode",0)),stderr=digest_item(data.get("stderr",{})),signal=reason))
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
    result["previously_blocked"]={l:[role,current(l)] for l,role in old_failed.items()}
    result["blocked_transitions"]=dict(collections.Counter("/".join(r) for r in result["previously_blocked"].values()))
    result["expected_shards"]=sum(expected_shards.values()) if expected_shards else None
    result["missing_shards"]={l:n-by_owner[l] for l,n in expected_shards.items() if n!=by_owner[l]}
    seen_shards={(s["owner"],s["run"],s["shard"]) for s in result["shards"]}
    result["complete_pass"]=bool(exact and expected_shards and result.get("bazel_exit")==0 and result["process_exit"]==0
        and not result["missing_target_count"] and not result["missing_shards"] and not result["unexpected_terminals"]
        and result["owners_with_test_result"]==runnable and len(result["test_summaries"])==runnable
        and all(v[0]=="PASSED" for v in result["test_summaries"].values())
        and all(v[0]=="built" for v in result["target_results"].values()) and len(seen_shards)==len(result["shards"])
        and all(s["status"]=="PASSED" and s["strategy"]=="remote" and not s["cached_locally"] and not s["cached_remotely"] for s in result["shards"]))
    output=json.dumps(result,sort_keys=True,separators=(",",":")).encode()
    assert len(output)<2500000 and not re.search(rb"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",output)
    (SEP/"receipt.json").write_bytes(output+b"\n")
    print(json.dumps({k:result.get(k) for k in ("source","invocation","bazel_exit","target_counts","result_counts","owners_with_test_result","missing_target_count","complete_pass")},sort_keys=True))


if __name__=="__main__":
    main()
