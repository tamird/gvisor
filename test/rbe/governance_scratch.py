"""Compare the real governance targets on ordinary and shell-free Linux workers."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["RUNNER_TEMP"])
out = root / "governance-receipts"
out.mkdir(exist_ok=True)
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
formats = ["CODEOWNERS", "MAINTAINERS.md"]
labels = ["//governance:generated_" + name for name in formats]
rule = Path("governance/tools/maintainers/defs.bzl")

def now():
    return datetime.now(timezone.utc).isoformat()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def rows(path):
    with subprocess.Popen([root / "bb", "print", "--compact_execution_log=" + str(path)], stdout=subprocess.PIPE) as reader:
        selected = subprocess.run(["jq", "-c", 'select((.targetLabel // "") | startswith("//governance:generated_"))'], stdin=reader.stdout, capture_output=True, check=True)
        assert reader.wait() == 0
    return [json.loads(x) for x in selected.stdout.splitlines()]

def measure(variant, arch):
    ref = os.environ["BASE" if variant == "stock" else "SOURCE"]
    subprocess.run(["git", "checkout", "--quiet", "--detach", ref], check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==ref
    original = rule.read_bytes()
    anchor = b'            visibility = ["//:__pkg__"],'
    assert original.count(anchor)==1
    rule.write_bytes(original.replace(anchor, ('            exec_properties = {"container-image": "' + image + '"},\n            tags = ["no-remote-cache"],\n').encode() + anchor))
    row = {"source":ref,"arch":arch,"expected":labels,"actions":{},"failures":[],"skips":[]}
    compact = root / (variant + "-" + arch + ".binpb")
    bep = compact.with_suffix(".bep")
    log = compact.with_suffix(".log")
    plat = "@governance_workers//:" + arch
    try:
        with log.open("wb") as stream:
            result = subprocess.run(["bazelisk","--bazelrc="+str(root/"governance.bazelrc"),"build","--keep_going","--host_platform="+plat,"--extra_execution_platforms="+plat,"--platforms=//tools/bazeldefs:linux_"+arch,"--remote_download_outputs=toplevel","--execution_log_compact_file="+str(compact),"--build_event_json_file="+str(bep),*labels],stdout=stream,stderr=subprocess.STDOUT)
        row["execution_finished_at"] = now()
        row["exit"] = result.returncode
        events = (json.loads(line) for line in bep.read_text().splitlines())
        row["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
        text = log.read_text(errors="replace")
        matches = [line.strip()[:450] for line in text.splitlines() if ("no such file" in line.lower() or "not found" in line.lower() or "ERROR:" in line) and "header" not in line.lower() and "api-key" not in line.lower()]
        row["diagnostics"] = matches[-12:]
        for r in rows(compact):
            label = r["targetLabel"]
            if label not in labels: continue
            props = {p["name"]:p["value"] for p in r.get("platform",{}).get("properties",[])}
            args = r.get("commandArgs",[])
            record = row["actions"][label] = {"tool":args[0],"image":props.get("container-image"),"arch":props.get("Arch"),"runner":r.get("runner"),"cached":r.get("cacheHit",False),"exit":r.get("exitCode",0),"status":r.get("status","")}
            assert props.get("Arch")==arch and props.get("container-image")==image and r.get("runner")=="remote" and not record["cached"], record
            if variant=="stock":
                assert result.returncode and Path(args[0]).name in ("bash","sh") and (record["exit"] or record["status"]), record
                assert any(("no such file" in s.lower() or "not found" in s.lower()) and ("bash" in s or "/sh" in s) for s in matches), matches[-3:]
            else:
                name = formats[labels.index(label)]
                expected = sha(Path(name))
                path = Path("bazel-bin/governance/generated")/name
                assert not result.returncode and not record["exit"] and Path(args[0]).name=="maintainers_gen", record
                produced = next(v["digest"]["hash"] for v in r["actualOutputs"] if v["path"].endswith("/governance/generated/"+name))
                record["output_sha256"] = produced
                assert produced == sha(path) == expected, (name, produced, expected)
                sources={v["path"] for v in r["inputs"]}
                assert args[0] in sources and all(next(arg.split("=",1)[1] for arg in args if arg.startswith(flag)) in sources for flag in ("-input=","-areas=")), args
    except Exception as exc:
        row["failures"].append(repr(exc)[:1100])
    finally:
        rule.write_bytes(original)
        row["missing"]=sorted(set(labels)-set(row["actions"]))
        row["status"]="PASS" if not row["missing"] and not row["failures"] else "FAIL"
        row["verdict_available_at"]=now()
        (out/(variant+"-"+arch+".json")).write_text(json.dumps(row,indent=2)+"\n")
        print(variant,arch,row["status"],row.get("invocation"),row["failures"],row["missing"],flush=True)
    return row

arms=[measure(arm,arch) for arm in ("stock","fixed") for arch in ("amd64","arm64")]
verdict={"expected":8,"recorded":sum(len(arm["actions"]) for arm in arms),"failures":[(a["arch"],a["source"]) for a in arms if a["status"]!="PASS"],"execution_finished_at":max((a["execution_finished_at"] for a in arms if "execution_finished_at" in a),default=None),"verdict_available_at":now(),"arms":arms}
(out/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
sys.exit(bool(verdict["failures"] or verdict["recorded"]!=8))
