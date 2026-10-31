"""Reconcile the live Sentry/cgroup observations without assuming checkpoint outcomes."""
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
def read(file):
    path = out / file
    return path.read_text(errors="replace").strip() if path.is_file() else ""

def one(pattern, text):
    hits = re.findall(pattern, text)
    return hits[0] if len(hits) == 1 else None

def verify(size):
    log = read(size+"/test.log")
    arms = {}
    for label, start in (("born_low", 200 << 20), ("lowered", 2 << 30)):
        errors = []
        # Go emits each subtest's output after its === RUN marker.
        marker = "=== RUN   TestCheckpointCgroupResize/"+label
        block = log.partition(marker)[2].split("=== RUN   ", 1)[0]
        header = one(r"GCSAVE kernel initial=(\d+) final=(\d+) pid=(\d+) membership=(\S+) per_level=(\d+)", block)
        observed = one(r"GCSAVE soft=(\d+) startup=(\d+) mf=(\d+) handle=(true|false) go_total=(\d+) go_released=(\d+) gc_cycles=(\d+)", block)
        population = one(r"GCSAVE population success=(true|false)", block)
        save = one(r"GCSAVE checkpoint success=(true|false) image_bytes=(-?\d+) elapsed_ms=(\d+) err=(.*?) image_err=([^\n]+)", block)
        stages = {}
        for phase, current, values in re.findall(r"GCSAVE phase=(\w+) memory=(\d+) events=([^\n]+)", block):
            stages[phase] = {"current":int(current), "events":dict((k, int(v)) for k, v in re.findall(r"(\w+) (\d+)", values))}
        row = {"kernel":header, "population_succeeded":population,
               "save":save, "go":observed, "phases":stages, "errors":errors}
        arms[label] = row
        if not header or int(header[0]) != start or int(header[1]) != 200<<20 or int(header[2])<=0 or not header[3].startswith("0::/gvisor-test-cg-") or int(header[4])!=(1000 if size=="large" else 200):
            errors.append("missing matched kernel limit, Sentry membership, or graph")
        if f"--- PASS: TestCheckpointCgroupResize/{label} " not in log:
            errors.append("named subtest did not pass")
        if population != "true" or save is None or observed is None or len(stages) != 3:
            errors.append("missing completed graph/save observation")
            continue
        if any("oom_kill" not in stage["events"] or "oom" not in stage["events"] for stage in stages.values()):
            errors.append("missing kernel memory.events counters")
            continue
        before, after = stages["after_population"], stages["after_checkpoint"]
        row["save_oom_kill_delta"] = after["events"]["oom_kill"]-before["events"]["oom_kill"]
        row["save_oom_delta"] = after["events"]["oom"]-before["events"]["oom"]
        selected, initial, mf, handle = observed[:4]
        if int(initial) != start or int(mf) >= 200<<20:
            errors.append("unexpected startup/MemoryFile charge")
        if save[0]=="true" and (int(save[1])<=0 or save[3]!="<nil>"):
            errors.append("no complete checkpoint image for successful save")
        if save[0]=="false" and save[3]=="<nil>":
            errors.append("missing failed checkpoint error")
        row["startup_budget"] = min(1<<30, (start-int(mf))//2)
        row["current_budget"] = ((200<<20)-int(mf))//2
        row["handle_used"] = handle == "true"
        if handle=="true" and int(selected)==row["startup_budget"] and int(selected)!=row["current_budget"]:
            row["selected_stale_budget"] = True
        else:
            row["selected_stale_budget"] = False
    errors = [f"{k}: {e}" for k,r in arms.items() for e in r["errors"]]
    if read(size+"/bazel-exit") != "0":
        errors.append("Bazel did not complete with the named observation")
    return {"arms":arms, "errors":errors, "exit":read(size+"/bazel-exit"),
            "start":read(size+"/start"), "end":read(size+"/end")}

runs = {size:verify(size) for size in ("large","small") if (out/size).is_dir()}
errors = [] if any(not row["errors"] for row in runs.values()) else ["no complete matched pair"]
result = {"product":read("product-source"), "fixture":read("fixture-source"),
          "runs":runs, "errors":errors, "criteria":"Both Sentry PIDs in verified 200 MiB v2 cgroups and same populated tmpfs graph. A stale active limit must equal the startup equation for the measured MemoryFile and exceed its current-limit equivalent. OOM is reported only from the kernel delta during Checkpoint."}
(out/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result))
sys.exit(bool(errors))
