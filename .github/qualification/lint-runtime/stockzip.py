"""After diagnosing the old worker, compare just the missing stock ZIP bytes."""
import json
import os
from pathlib import Path
import subprocess
import uuid
import zipfile
import check as h

SOURCE = "700b0bbea70ff699553d8e40827a2f3dab4afdd2"
ORIGINAL = "01446201fd8e758267f4e1abd9727926655110b7"
EXPECTED = {"//test/rbe/zip_writer_probe:foreign_archive": "87ded80b2b85abe8ea0f3537ac1984bc1c2834a6b6ae853eb44e63b459364859", "@@rules_pkg+//tests/zip:test_zip_empty":"8739c76e681f900923b900c9df0ef75cf421d39cabb54650c4b9ad19b6a76d85"}
PKG = Path("test/rbe/zip_writer_probe")
MARKER = "rules_pkg_inherited_value"
DATA = b"Compressed from the same bytes.\n" * 200
O = {"verdict":"INCOMPLETE","source":SOURCE,"supplier":ORIGINAL,"candidate_evidence":"https://github.com/tamird/gvisor/actions/runs/40933723437","targets":list(EXPECTED),"actions":{},"started":h.utc(),"invocation":str(uuid.uuid4())}

def main():
    old = json.loads((h.ROOT / "original-diagnostics.json").read_text())["zip"]
    h.check(old["bootstrap_diagnostic"], "original tool diagnostic was not recovered; no stock re-execution")
    h.check(h.git("rev-parse", "HEAD").decode().strip() == SOURCE, "wrong product")
    supplier = (Path.cwd().parent / "original").resolve()
    h.check(subprocess.check_output(["git","-C",supplier,"rev-parse","HEAD"],text=True).strip()==ORIGINAL,"wrong supplier")
    PKG.mkdir(parents=True)
    (PKG/"payload.txt").write_bytes(DATA)
    (PKG/"BUILD").write_text('load("@rules_pkg//pkg:zip.bzl", "pkg_zip")\npkg_zip(name="foreign_archive", srcs=["payload.txt"], mode="0640", timestamp=1000000000, package_dir="/demo")\n')
    log = h.ROOT / "stockzip.exec"
    command = ["bazel","--bazelrc="+os.environ["QA_RC"],"build","--config=x86_64","--config=static-python","--jobs=24","--override_module=rules_pkg="+str(supplier),"--spawn_strategy=local","--extra_execution_platforms=//tools/bazeldefs:linux_amd64","--modify_execution_info=^PackageZip$=+no-cache","--action_env=RULES_PKG_ZIP_PROBE="+MARKER,"--execution_log_json_file="+str(log),"--invocation_id="+O["invocation"],"--keep_going","//test/rbe/zip_writer_probe:foreign_archive","@rules_pkg//tests/zip:test_zip_empty"]
    p = subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    O["exit"]=p.returncode
    O["diagnostics"]=[x[:350] for x in p.stdout.splitlines() if x.startswith(("ERROR:","FAILED:")) and not any(w in x.lower() for w in ("key","credential","token","header","command line"))][-8:]
    h.check(p.returncode==0,"ordinary stock ZIP build failed")
    root=Path(subprocess.check_output(["bazel","--bazelrc="+os.environ["QA_RC"],"info","execution_root"],text=True).strip())
    for spawn in h.rows(log):
        if spawn.get("mnemonic")!="PackageZip":continue
        label=spawn["targetLabel"]
        h.check(label in EXPECTED and not spawn.get("cacheHit") and spawn.get("exitCode",0)==0,"uncached successful ZIP action required")
        env={i["name"]:i.get("value","") for i in spawn["environmentVariables"]}
        h.check(env.get("RULES_PKG_ZIP_PROBE")==MARKER and [env.get(x) for x in ("LANG","LC_CTYPE","PYTHONIOENCODING","PYTHONUTF8")]==["en_US.UTF-8","UTF-8","UTF-8","1"],"different environment")
        executable=root/spawn["commandArgs"][0]
        h.check(executable.read_bytes().splitlines()[0]==b"#!/usr/bin/env bash","not original writer wrapper")
        output,=spawn["actualOutputs"]
        archive=root/output["path"]
        digest=h.digest(archive.read_bytes()); h.check(digest==EXPECTED[label],"complete ZIP bytes differ")
        with zipfile.ZipFile(archive) as z:
            members=[{"path":i.filename,"mode":format((i.external_attr>>16)&0o7777,"04o"),"time":i.date_time,"compression":i.compress_type} for i in z.infolist()]
            if label.startswith("//"):h.check(z.read("demo/payload.txt")==DATA,"regular member differs")
        O["actions"][label]={"runner":spawn["runner"],"digest":digest,"bytes":archive.stat().st_size,"members":members}
    h.check(set(O["actions"])==set(EXPECTED),"wrong target set")
    O["verdict"]="PASS"

try: main()
except Exception as exc:O.update(verdict="FAIL",error=str(exc))
O["finished"]=h.utc()
(h.ROOT/"stockzip.json").write_text(json.dumps(O,indent=2)+"\n")
print(json.dumps(O))
raise SystemExit(O["verdict"]!="PASS")
