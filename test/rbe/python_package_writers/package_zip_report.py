"""Check the original execution log against the two retained ordinary writer records."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import zipfile

P = Path(os.environ["RUNNER_TEMP"])
ARCH = os.environ["QUALIFICATION_ARCH"]
PREFIX = Path("test/rbe/python_package_writers")
CONTROL = json.loads((PREFIX / "ordinary.json").read_text())[ARCH]
EXPECTED = json.loads((PREFIX / "writer-digests.json").read_text())
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
RESULT = {"source": os.environ["GITHUB_SHA"], "arch": ARCH, "expected": list(CONTROL), "workers": {}, "status": "INCOMPLETE"}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def verify():
    with tarfile.open(P / "qualification.tar.gz") as tf:
        assert tf.extractfile("final-head.txt").read().decode().strip() == RESULT["source"]
        eventpath, = [n for n in tf.getnames() if n.endswith("/events.jsonl")]
        events = [json.loads(l) for l in tf.extractfile(eventpath)]
        RESULT["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
        native, = [n for n in tf.getnames() if n.endswith("/native-exit.txt")]
        RESULT["native_exit"] = int(tf.extractfile(native).read())
        path, = [n for n in tf.getnames() if n.endswith("/execution.binpb")]
        data = tf.extractfile(path).read()
        source = P / "physical.binpb"
        source.write_bytes(data)
        RESULT["execution_sha256"] = sha(data)
    text = subprocess.check_output([os.environ["BB"], "print", "--compact_execution_log=" + str(source)], text=True)
    while text.strip():
        text = text.lstrip()
        row, end = json.JSONDecoder().raw_decode(text)
        text = text[end:]
        key = row.get("mnemonic")
        if key not in CONTROL:
            continue
        assert key not in RESULT["workers"]
        rec = RESULT["workers"][key] = {"action": row["digest"], "argv": row["commandArgs"], "label": row["targetLabel"],
                                        "exit": row.get("exitCode", 0), "metrics": row.get("metrics", {})}
        props = {e["name"]: e["value"] for e in row["platform"]["properties"]}
        rec.update(runner=row.get("runner"), cache_hit=row.get("cacheHit", False), image=props.get("container-image"), cpu=props.get("Arch"))
        assert rec["runner"] == "remote" and not rec["cache_hit"] and rec["image"] == IMAGE and rec["cpu"] == ARCH, rec
        assert rec["exit"] == 0, rec
        expected_args, expected_inputs, expected_outputs = CONTROL[key]
        assert rec["argv"][1:] == expected_args, (rec["argv"], expected_args)
        files = {i["path"]:i.get("digest", {}) for i in row["inputs"]}
        assert {n:files[n] for n in expected_inputs} == expected_inputs
        rec["package_inputs"] = expected_inputs
        rec["input_count"] = len(files)
        rec["distinct_input_bytes"] = sum(int(d[1]) for d in {(v.get("hash"),v.get("sizeBytes",0)) for v in files.values()})
        program = rec["argv"][0]
        exe = Path(program).read_bytes()
        assert files[program]["hash"] == sha(exe) and exe[:5] == b"\x7fELF\x02"
        machine = int.from_bytes(exe[18:20], "little")
        assert machine == {"amd64":62, "arm64":183}[ARCH]
        off = struct.unpack_from("<Q", exe,32)[0]
        size,count = struct.unpack_from("<HH", exe,54)
        assert all(struct.unpack_from("<I",exe,off+i*size)[0] != 3 for i in range(count))
        rec["launcher"] = {"path":program,"sha256":sha(exe),"size":len(exe),"e_machine":machine,"elf_interpreter":False}
        zips = {d.get("hash"):(p,d) for p,d in files.items() if p.endswith(("/tar_archive.zip","/deb_archive.zip"))}
        assert len(zips)==1, zips
        ziphash,(zname,zdigest) = next(iter(zips.items()))
        zfile = Path(zname)
        # A runfiles link may be virtual on the coordinator; the physical artifact is downloaded.
        if not zfile.is_file():
            suffix = zname[zname.index("test/rbe/python_package_writers/"):]
            zfile, = [p for p in Path("bazel-out").glob("*/bin/"+suffix) if p.is_file() and sha(p.read_bytes())==ziphash]
        assert zfile.stat().st_size == int(zdigest["sizeBytes"])
        with zipfile.ZipFile(zfile) as za:
            member, = [n for n in za.namelist() if n.endswith("/"+EXPECTED[key]["path"])]
            actual = sha(za.read(member))
            assert actual==EXPECTED[key]["sha256"], (key,member,actual,EXPECTED[key])
        rec["zip"]={"path":zname,"sha256":ziphash,"bytes":int(zdigest["sizeBytes"]),"writer_source":member,"writer_sha256":actual}
        rec["products"]={}
        for o in row["actualOutputs"]:
            out=Path(o["path"])
            d=o["digest"]
            data=out.read_bytes()
            assert sha(data)==d["hash"] and len(data)==int(d["sizeBytes"])
            rec["products"][out.name]=d
        assert rec["products"]==expected_outputs, (rec["products"],expected_outputs)
    assert RESULT["native_exit"]==0 and set(RESULT["workers"])==set(CONTROL)
    RESULT["status"]="PASS"


try:
    verify()
except Exception as error:
    RESULT["diagnostic"]=repr(error)
    raise
finally:
    (P/"writers.json").write_text(json.dumps(RESULT,indent=2)+"\n")
