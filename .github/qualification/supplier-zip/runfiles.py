import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

base = Path.cwd()
supplier = base / "supplier"
consumer = base / "consumer"
consumer.mkdir()
scratch = Path(os.environ["RUNNER_TEMP"])
report = {"expected": 4, "records": [], "errors": [], "verdict": "FAIL"}
out = scratch / "rules-zip-runfiles.json"
out.write_text(json.dumps(report))
(consumer / "MODULE.bazel").write_text('''module(name = "zip_probe")
bazel_dep(name = "rules_pkg")
local_path_override(module_name = "rules_pkg", path = "../supplier")
bazel_dep(name = "rules_python", version = "1.9.0")
''')
(consumer / "BUILD.bazel").write_text('''load("@rules_pkg//pkg:zip.bzl", "pkg_zip")
pkg_zip(name="empty")
pkg_zip(name="normal", srcs=["payload.txt"], package_dir="demo", mode="0640", timestamp=1000000000, compression_type="deflated")
''')
(consumer / "payload.txt").write_text("ZIP writer\n" * 50)

def objects(path):
    raw = path.read_text()
    d = json.JSONDecoder()
    i = 0
    while (raw := raw[i:].lstrip()):
        o, i = d.raw_decode(raw)
        yield o

for name, sha in (("stock", "01446201fd8e758267f4e1abd9727926655110b7"),
                  ("candidate", "1702e63c4673bf8c00df82a1613c2359895fbe20")):
    subprocess.run(["git", "checkout", "--detach", sha], cwd=supplier, check=True)
    for zipped in ("no", "yes"):
        tag = f"{name}-manifest-zipped-{zipped}"
        log = scratch / f"{tag}-actions.json"
        try:
            # Clean outputs between modes, so every result includes two writer executions.
            subprocess.run(["bazel", "clean"], cwd=consumer, check=True)
            subprocess.run(["bazel", "build", "--enable_runfiles=no", f"--build_python_zip={zipped}",
                            "--@rules_python//python/config_settings:bootstrap_impl=script",
                            "--strategy=PackageZip=linux-sandbox", f"--execution_log_json_file={log}",
                            "//:empty", "//:normal"], cwd=consumer, check=True)
            actions = [x for x in objects(log) if x["mnemonic"] == "PackageZip"]
            assert len(actions) == 2 and all(x.get("runner") == "linux-sandbox" and
                                            not x.get("cacheHit") for x in actions), [(x.get("runner"),x.get("cacheHit")) for x in actions]
            stage2 = [any("stage2_bootstrap" in arg for arg in x["commandArgs"]) for x in actions]
            assert stage2 == ([name == "candidate"] * 2), (tag,stage2)
            data = []
            for f in ("empty", "normal"):
                contents = (consumer / f"bazel-bin/{f}.zip").read_bytes()
                with zipfile.ZipFile(io.BytesIO(contents)) as z:
                    members = [i.filename for i in z.infolist()]
                    if f == "empty":
                        assert not members, members
                    else:
                        assert z.read("demo/payload.txt") == (consumer / "payload.txt").read_bytes()
                    data.append([f,hashlib.sha256(contents).hexdigest(),members])
            report["records"].append({"source":sha,"case":tag,"stage2":stage2,"files":data})
        except Exception as e:
            report["errors"].append({"case":tag,"error":repr(e)})
            print(f"ERROR for {tag}: {e}", flush=True)
        out.write_text(json.dumps(report,indent=2)+"\n")
if len(report["records"]) == report["expected"] and len({str(r["files"]) for r in report["records"]}) == 1:
    report["verdict"] = "PASS"
out.write_text(json.dumps(report,indent=2)+"\n")
print(out.read_text(), flush=True)
if report["verdict"] != "PASS":
    raise SystemExit(1)
