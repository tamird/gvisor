from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from qualification_report import xml_cases


def main(records):
    d = records / "nginx"
    source = os.environ["GITHUB_SHA"]
    def read(name):
        p = d / name
        return p.read_text(errors="replace").strip() if p.is_file() else ""
    out = {"source": source, "instrumentation": os.environ["COUNTERS"], "base": os.environ["BASE"],
           "target": "//test/rbe/window_nginx:checkpoint", "execution_finished_at": read("end"), "bazel_exit": read("exit")}
    problems = []
    def need(ok, label):
        if not ok:
            problems.append(label)
    log = read("nginx.test.log")
    out["invocation"] = re.findall(r"Invocation ID: ([0-9a-f-]{36})", read("bazel-diagnostics"))[:1]
    need(read("source") == read("source-after") == source and read("parent") == os.environ["COUNTERS"], "source")
    need((d / "source-before-status").exists() and (d / "source-after-status").exists() and not read("source-before-status") and not read("source-after-status"), "tracked changes")
    need(out["bazel_exit"] == "0" and log.count("CLEAR-DONE pairs=16") == 1, "benchmark or Bazel exit")
    expected_id = "sha256:5f9e05caa37420ad50b7ccf873ea117fcbd8cff24b3fcb689cb904bcab624cf5"
    out["nginx_image"] = read("nginx-image")
    need(out["nginx_image"] == expected_id, "image")
    need("--- SKIP:" not in log, "skipped workload")
    try:
        x = xml_cases(d / "nginx.test.xml")
        need(x["passed"] > 0 and x["failed"] == 0 and x["skipped"] == 0, "original XML")
    except (OSError, ValueError, SyntaxError, KeyError) as e:
        problems.append("original XML: "+str(e))
    captures=re.findall(r"STREAM-CAPTURE bytes=(\d+) digest=([a-f0-9]{64})",log)
    rows=[]
    cur=None
    for line in log.splitlines():
        trial=re.search(r"CLEAR-TRIAL pair=(\d+) mode=(capacity|used)",line)
        if trial:
            cur=(int(trial[1]),trial[2]);continue
        match=re.search(r"RESTORE-CLEAR pass=(\d+) mode=(capacity|used) digest=([a-f0-9]{64}) wall=(\d+) cpu=(\d+) featuresUsed=(\d+) featuresCap=(\d+) kernelUsed=(\d+) kernelCap=(\d+)",line)
        if match:
            need(cur is not None and cur[1]==match[2],"current arm")
            if cur:
                rows.append(dict(pair=cur[0],mode=match[2],phase=int(match[1]),digest=match[3],**dict(zip(("wall","cpu","featuresUsed","featuresCap","kernelUsed","kernelCap"),map(int,match.groups()[3:])))))
    expected=[(i,m,j) for i in range(16) for m in (("used","capacity") if i%2 else ("capacity","used")) for j in range(3)]
    need([(r["pair"],r["mode"],r["phase"]) for r in rows]==expected,"executed 96 restores in planned order")
    need(len(captures)==1 and int(captures[0][0])>30000 and all(r["digest"]==captures[0][1] for r in rows),"single original input")
    need(all(0<r["featuresUsed"]<=r["featuresCap"] and 0<r["kernelUsed"]<=r["kernelCap"] and r["kernelUsed"]>r["featuresUsed"] and min(r["cpu"],r["wall"])>0 for r in rows),"positive CPU, wall and consistent frame sizes")
    out.update(expected_restores=len(expected),executed_restores=len(rows),skipped=log.count("--- SKIP:"),measurements=rows,failures=problems,capture=captures)
    out.update(status="FAIL" if problems else "PASS", missing=problems, verdict_available_at=datetime.now(timezone.utc).isoformat())
    (records/"verdict.json").write_text(json.dumps(out, indent=2)+"\n")
    print(json.dumps(out, sort_keys=True))
    return bool(problems)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
