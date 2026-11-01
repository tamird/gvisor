"""Verify the joined coverage + musl transition without rebuilding the binaries."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "coverage-actions.json"
source = os.environ["PRODUCT"]
architectures = ("x86_64", "aarch64")
bins = ("runsc_race_cov", "gvisor_sentry_race_cov")
closure = "deps(set(//runsc:runsc_race_coverage //runsc/cmd/sentry:gvisor_sentry_race_coverage))"
packages = 'filter("//pkg/(tcpip:tcpip|sync:sync)$", ' + closure + ')'
aquery = 'mnemonic("GoLink", ' + closure + ') union mnemonic("GoCompilePkg", ' + packages + ')'
started = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
summary = {"source": source, "expected": list(architectures), "status": "INCOMPLETE", "architectures": {}}
def save():
    out.write_text(json.dumps(summary,indent=2)+"\n")
save()
if subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()!=source:
    raise ValueError("source mismatch")

for arch in architectures:
    row = {"status": "INCOMPLETE", "started": started()}
    summary["architectures"][arch] = row
    save()
    try:
        terms = ["--config="+arch, "--noshow_progress"]
        data = json.loads(subprocess.check_output(["bazel","aquery",*terms,"--output=jsonproto","--include_artifacts=false","--include_param_files=true",aquery]))
        labels = {x["id"]: x["label"] for x in data["targets"]}
        compiles = {"tcpip": [], "sync": []}
        links = {}
        for action in data["actions"]:
            args = action.get("arguments",[]) + [s for p in action.get("paramFiles",[]) for s in p["arguments"]]
            label = labels[action["targetId"]]
            tokens = [t for s in args for t in s.split()]
            if action["mnemonic"] == "GoLink":
                names = {Path(arg).name for i,arg in enumerate(args) if i and args[i-1] == "-o"}
                for name in names.intersection(bins):
                    links[name]={"label":label,"race":"-race" in tokens,"static":"-static" in tokens,
                                 "external":"external" in tokens and "-linkmode" in tokens}
            elif action["mnemonic"] == "GoCompilePkg" and "-race" in tokens:
                pkg=label.rsplit(":",1)[-1]
                if pkg in compiles:
                    compiles[pkg].append({"label":label,"atomic":"-cover_mode" in args and "atomic" in args,"cover_files":args.count("-cover")})
        row.update(links=links,compiles=compiles)
        expr='str(target.label)+" "+str(build_options(target).get("//command_line_option:platforms"))+" "+str(build_options(target).get("//command_line_option:collect_code_coverage"))'
        platform_lines=subprocess.check_output(["bazel","cquery",*terms,"--output=starlark","--starlark:expr="+expr,packages],text=True).splitlines()
        row["platforms"]=platform_lines
        row["checks"]={
            "both_static_race_links":set(links)==set(bins) and all(r["race"] and r["static"] and r["external"] for r in links.values()),
            "included_instrumented":any(r["atomic"] and r["cover_files"]>0 for r in compiles["tcpip"]),
            "excluded_uninstrumented":bool(compiles["sync"]) and all(not r["atomic"] and not r["cover_files"] for r in compiles["sync"]),
            "both_on_covered_musl_platform":all(any("//pkg/"+pkg+":"+pkg+" " in s and "linux_"+arch+"_musl" in s and s.endswith(" True") for s in platform_lines) for pkg in compiles),
        }
        row["status"]="PASS" if all(row["checks"].values()) else "FAIL"
    except (subprocess.CalledProcessError,ValueError,KeyError) as ex:
        row["error"]=str(ex)[:1200]
    row["finished"]=started()
    save()
summary["status"]="PASS" if all(r["status"]=="PASS" for r in summary["architectures"].values()) else "INCOMPLETE"
summary["verified_at"]=started()
save()
print(json.dumps(summary,indent=2))
sys.exit(summary["status"]!="PASS")
