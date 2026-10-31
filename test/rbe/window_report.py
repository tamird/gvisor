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
    need(out["bazel_exit"] == "0" and log.count("ASSEMBLY-BENCH exit=0") == 1, "benchmark or Bazel exit")
    expected_id = "sha256:5f9e05caa37420ad50b7ccf873ea117fcbd8cff24b3fcb689cb904bcab624cf5"
    out["nginx_image"] = read("nginx-image")
    need(out["nginx_image"] == expected_id, "image")
    need("BenchmarkCheckpointFile/EncodeStatefile" in log and "--- SKIP:" not in log and re.search(r"^PASS$",log,re.M), "benchmark output")
    comp = re.findall(r"^.*?\b1\s+[0-9.]+ ns/op[^\n]*?\s([0-9]+) encoded-kernel-B\b",log,re.M)
    need(len(comp)==1, "one outer statefile measurement")
    if len(comp)==1:
        out["encoded_statefile_bytes"]=int(comp[0])
    try:
        x = xml_cases(d / "nginx.test.xml")
        need(x["passed"] > 0 and x["failed"] == 0 and x["skipped"] == 0, "original XML")
    except (OSError, ValueError, SyntaxError, KeyError) as e:
        problems.append("original XML: "+str(e))
    lines = re.findall(r"GRAPH-STREAM root=\*kernel\.Kernel magic=([a-f0-9]+) ((?:\w+=\d+ ?)+)", log)
    need(len(lines)==1, "one graph save")
    if len(lines)==1:
        magic, fields = lines[0]
        m = {k: int(v) for k,v in re.findall(r"(\w+)=(\d+)", fields)}
        out["graph"] = dict(magic=magic, **m)
        need(magic=="32534776", "current graph stream format")
        fields = set("bytes cap header types objects targets payload staged stagedcap copied tablescap typecap subcount subbytes subcap submax objectcount targetcount typecount".split())
        need(m.keys()==fields, "metric coverage")
        if fields == m.keys():
            need(m["header"]==12 and m["header"]+m["types"]+m["objects"]+m["targets"]+m["payload"]==m["bytes"], "stream byte accounting")
            need(0<m["copied"]<=m["payload"] and m["staged"]<=m["stagedcap"] and m["bytes"]<=m["cap"] and m["objects"]+m["targets"]<=m["tablescap"], "buffer sizes")
            need(m["subbytes"]<=m["subcap"] and m["submax"]<=m["subcap"] and m["subbytes"]<=m["payload"], "array buffers")
            need(min(m["objectcount"],m["targetcount"],m["typecount"])>0, "empty kernel")
    out.update(status="FAIL" if problems else "PASS", missing=problems, verdict_available_at=datetime.now(timezone.utc).isoformat())
    (records/"verdict.json").write_text(json.dumps(out, indent=2)+"\n")
    print(json.dumps(out, sort_keys=True))
    return bool(problems)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
