import hashlib
import json
import sys
from pathlib import Path

dest, log, events, executor, exit_code, mode, architecture, tag = sys.argv[1:]
dest, executor = Path(dest), Path(executor)
negative = mode == "stock"
actions = []
parser = json.JSONDecoder()
with Path(log).open() as stream:
    data = ""
    while chunk := stream.read(1048576):
        data += chunk
        data = data.lstrip()
        while data:
            try:
                record, offset = parser.raw_decode(data)
            except json.JSONDecodeError:
                break
            data = data[offset:].lstrip()
            if record.get("targetLabel") == "//tools/bazeldefs:version":
                actions.append(record)
    assert not data.strip(), data[:100]
rows = [r for r in actions if r.get("mnemonic") == ("Genrule" if negative else "PackageVersion")]
assert len(rows) == 1, [(r.get("mnemonic"),r.get("runner")) for r in actions]
r, = rows
props = {p["name"]:p["value"] for p in r.get("platform",{}).get("properties",[])}
inputs = [v["path"] for v in r.get("inputs",[])]
source = (dest/"source").read_text().strip()
record = dict(source=source, build_exit=int(exit_code), mode=mode, target_architecture=architecture, executor=props,
              runner=r.get("runner"), cache_hit=r.get("cacheHit", False), action_exit=r.get("exitCode", 0),
              action_digest=r.get("digest"), command=r.get("commandArgs"), metrics=r.get("metrics"),
              status_inputs=[f for f in inputs if f.endswith("-status.txt")], cause=r.get("status"))
for line in Path(events).open():
    event=json.loads(line)
    if "started" in event: record["invocation"]=event["started"]["uuid"]
    if "finished" in event: record["bazel_exit"]=event["finished"]["exitCode"]["code"]
(dest/"action.json").write_text(json.dumps(record,indent=2)+"\n")
assert props.get("container-image") == "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",record
assert props.get("Arch")=="amd64" and record["runner"]=="remote" and not record["cache_hit"],record
if negative:
    assert int(exit_code) and record["command"][0].endswith("/bash"),record
    text=(dest/"build.log").read_text()
    assert ("No such file" in text or "not found" in text or "no such file" in text) and "/bin/bash" in text,text[-1400:]
else:
    assert int(exit_code)==0 and record["action_exit"]==0,record
    stable,=[f for f in inputs if f.endswith("/stable-status.txt")]
    assert [e["value"] for e in r["environmentVariables"] if e["name"]=="BAZEL_STABLE_STATUS_FILE"]==[stable],record
    key="STABLE_VERSION" if mode=="nightly" else "BUILD_EMBED_LABEL"
    cmd=record["command"]
    assert len(cmd)==3 and cmd[0].endswith("/package_version") and cmd[1]==key,cmd
    out,=[v for v in r["actualOutputs"] if v["path"]==cmd[2]]
    content=(executor/out["path"]).read_bytes()
    assert hashlib.sha256(content).hexdigest()==out["digest"]["hash"],out
    assert content==tag.split("-",1)[1].encode()+b"\n",content
    record["package_version"]=content.decode().rstrip()
    record["package_sha256"]=out["digest"]["hash"]
    record["status_env"]=stable
    (dest/"action.json").write_text(json.dumps(record,indent=2)+"\n")
(dest/"verdict").write_text("verified\n")
print(record)
