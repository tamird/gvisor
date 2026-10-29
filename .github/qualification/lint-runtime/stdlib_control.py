"""Check whether isolating just GoStdlib fixes the two original Nogo failures."""
import json
import os
from pathlib import Path
import subprocess
import uuid

import check as harness

source = "eaf429600f44b9cb375fb8fed2000739faf87c99"
targets = ["//pkg/bpf:bpf", "//pkg/ebpf:ebpf"]
out = {"source":source,"started":harness.utc(),"invocation":str(uuid.uuid4()),
       "expected":targets,"completed":{},"library":[], "analysis":[],"errors":[], "verdict":"INCOMPLETE"}
report=harness.ROOT/"stdlib-control.json"
def save():
    report.write_text(json.dumps(out, indent=2))
save()
try:
    harness.check(harness.git("rev-parse","HEAD").decode().strip() == source,"wrong product checkout")
    elog=harness.ROOT/"stdlib.exec"
    bep=harness.ROOT/"stdlib.bep"
    cmd=["bazel","--bazelrc="+os.environ["QA_RC"],"build","--config=x86_64","--jobs=48","--keep_going",
         "--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
         "--spawn_strategy=local","--strategy=GoStdlib=linux-sandbox","--strategy=GoStateify=remote",
         "--noremote_upload_local_results","--modify_execution_info=^(GoStdlib|GoStandardLibraryAnalysis)$=+no-cache",
         "--remote_default_exec_properties=Arch=amd64","--remote_default_exec_properties=OSFamily=linux",
         "--remote_default_exec_properties=workload-isolation-type=oci",
         "--remote_default_exec_properties=container-image="+harness.IMAGE,
         "--invocation_id="+out["invocation"],"--execution_log_json_file="+str(elog),"--build_event_json_file="+str(bep),*targets]
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    out["exit"]=p.returncode
    out["diagnostics"]=[l[:600] for l in p.stdout.splitlines() if any(s in l for s in ("ERROR:", "error filtering packages:", "FAILED:", "present in stdlib", "sandbox", "Analyzing Go Standard Library")) and not any(s in l.lower() for s in ("credential","key=","header","api-key","token"))][-45:]
    for event in harness.rows(bep):
        label=event.get("id",{}).get("targetCompleted",{}).get("label")
        if label in targets:
            out["completed"][label]=event.get("completed",{}).get("success",False)
    for spawn in harness.rows(elog):
        mn=spawn.get("mnemonic")
        if mn not in ("GoStdlib","GoStandardLibraryAnalysis"): continue
        row={"runner":spawn.get("runner"),"cached":spawn.get("cacheHit",False),"exit":spawn.get("exitCode",0),"label":spawn.get("targetLabel")}
        if mn=="GoStandardLibraryAnalysis":
            row["target_arch"]=[arg for arg in spawn.get("commandArgs",[]) if arg.startswith("-GOARCH=")]
            out["analysis"].append(row)
        else:
            objs=[obj.get("path","") for obj in spawn.get("actualOutputs",[])]
            row["output_count"]=len(objs)
            # Execution logs expand tree artifacts to the files actually produced.
            row["fmt"]=[p for p in objs if p.endswith("/fmt.a")]
            row["extra_archives"]=[p for p in objs if p.endswith("/reflect/internal/example1.a") or p.endswith("/crypto/internal/sysrand/internal/seccomp.a")]
            out["library"].append(row)
    save()
    assert out["analysis"] and {v for row in out["analysis"] for v in row["target_arch"]} == {"-GOARCH=amd64","-GOARCH=arm64"}, "missing architecture analysis"
    assert all(row["runner"]=="local" and not row["cached"] and not row["exit"] for row in out["analysis"]), out["analysis"]
    assert out["library"] and all(row["runner"]=="linux-sandbox" and not row["cached"] and not row["exit"] and not row["extra_archives"] for row in out["library"]), out["library"]
    assert out["completed"]==dict.fromkeys(targets,True) and p.returncode==0, "owning packages did not both build"
    out["verdict"]="PASS"
except Exception as e:
    out["errors"].append(repr(e))
    out["verdict"]="FAIL"
finally:
    out["finished"]=harness.utc();save()
print(json.dumps(out,sort_keys=True))
raise SystemExit(out["verdict"]!="PASS")
