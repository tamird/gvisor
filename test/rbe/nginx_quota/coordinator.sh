#!/bin/bash
set -euo pipefail
tmp=$1
out=$tmp/nginx-public
target=//test/rbe/nginx_quota:readers
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
rc=0
bazel --bazelrc="$tmp/quota.bazelrc" test --config=x86_64 -c opt --jobs=48 --nocache_test_results '--modify_execution_info=^TestRunner$=+no-remote-cache' --test_timeout=3600 --zip_undeclared_test_outputs --remote_download_outputs=toplevel '--remote_download_regex=.*(test[.](log|xml)|outputs[.]zip|quota-(summary|samples)[.]json)' --build_event_json_file="$tmp/quota-bep" "$target" > "$tmp/quota-private-log" 2>&1 || rc=$?
python3 - "$rc" "$tmp" <<'PY'
import datetime, json, pathlib, re, sys, zipfile
rc=int(sys.argv[1]); tmp=pathlib.Path(sys.argv[2]); out=tmp/'nginx-public'
bep=tmp/'quota-bep'
events=[json.loads(line) for line in bep.open()] if bep.exists() else []
summaries={e["id"]["testSummary"]["label"]:e["testSummary"]["overallStatus"] for e in events if "testSummary" in e["id"]}
attempts=[e["testResult"] for e in events if "testResult" in e]
starts=[e["started"] for e in events if "started" in e]
stamp=lambda:datetime.datetime.now(datetime.timezone.utc).isoformat()
data={"native_bazel_exit":rc,"summaries":summaries,"attempts":[{"status":v.get("status"),"start":v.get("testAttemptStart"),"duration":v.get("testAttemptDuration"),"local_cache":v.get("cachedLocally",False),"execution":v.get("executionInfo",{}).get("strategy")} for v in attempts],"invocations":[x.get("uuid") for x in starts],"build_finished":[e["finished"].get("finishTime") for e in events if "finished" in e],"collected_at":stamp()}
(out/'coordinator.json').write_text(json.dumps(data,indent=2))
base=pathlib.Path("bazel-testlogs/test/rbe/nginx_quota/readers")
for name in ("quota-summary.json","quota-samples.json"):
    bundle=base/"test.outputs/outputs.zip"
    if bundle.exists():
        with zipfile.ZipFile(bundle) as z:
            if name in z.namelist(): (out/name).write_bytes(z.read(name))
    elif (f:=base/"test.outputs"/name).exists(): (out/name).write_bytes(f.read_bytes())
if rc or not (out/"quota-summary.json").is_file():
    pat=re.compile(r"^(?:INFO: Streaming|ERROR:|FAIL:|AssertionError|RuntimeError|OSError|PermissionError|FileNotFoundError|TimeoutError|ValueError|KeyError)")
    lines=[]
    for p in (tmp/"quota-private-log",base/"test.log"):
        if p.exists(): lines.extend(v[:450] for v in p.read_text(errors="replace").splitlines() if pat.search(v) and not re.search(r"(?i)api.key|remote.header|token=|authorization",v))
    (out/"failure.txt").write_text("\n".join(lines[-20:])+"\n")
    print("\n".join(lines[-10:]),flush=True)
s=json.loads((out/"quota-summary.json").read_text()) if (out/"quota-summary.json").exists() else {}
assert rc==0 and summaries=={"//test/rbe/nginx_quota:readers":"PASSED"} and s.get("result")=="PASS" and s["completed"]==s["expected"]==216 and s["observed_http"]==3672, (rc,summaries,s.get("error"))
PY
