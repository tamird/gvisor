"""Run Gazelle's root-module tests on its own pinned Bazel version."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
source="faf0f078942123f153391b4d430786a7841269e0"
staging=Path(tempfile.mkdtemp(dir=os.environ["RUNNER_TEMP"],prefix="gazelle-ci-"))
evidence=Path.cwd()/"supplier-evidence";evidence.mkdir()
subprocess.run(["git","fetch","--depth=1","--no-tags","https://github.com/tamird/gvisor.git","refs/heads/research/gazelle-native-repo-env"],check=True)
assert subprocess.check_output(["git","rev-parse","FETCH_HEAD"],text=True).strip()==source
archive=staging/"source.tar"
subprocess.run(["git","archive","-o",archive,source],check=True)
work=staging/"work"; work.mkdir()
subprocess.run(["tar","-xf",archive,"-C",work],check=True)
version=(work/".bazelversion").read_text().strip();assert version=="8.2.1"
bin_dir=staging/"bin";bin_dir.mkdir();bazel=bin_dir/"bazel"
subprocess.run(["curl","-fsSL","--retry","3","-o",bazel,f"https://releases.bazel.build/{version}/release/bazel-{version}-linux-x86_64"],check=True)
assert hashlib.sha256(bazel.read_bytes()).hexdigest()=="7ff2b6a675b59a791d007c526977d5262ade8fa52efc8e0d1ff9e18859909fc0";bazel.chmod(0o755)
env={**os.environ,"PATH":f"{bin_dir}:{os.environ['PATH']}","USE_BAZEL_VERSION":version};env.pop("BUILDBUDDY_API_KEY",None)
args=[str(bazel),"--batch",f"--output_user_root={staging/'output'}","test","--jobs=3","--keep_going","--test_output=errors",f"--build_event_json_file={staging/'bep.jsonl'}","//..."]
try:
    run=subprocess.run(args,cwd=work,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=3300)
    status,output=run.returncode,run.stdout
except subprocess.TimeoutExpired as exc:
    status,output=124,(exc.stdout or b"")+b"\nFULL SUITE TIMEOUT\n"
(evidence/"full-suite.log").write_bytes(output)
report={"source":source,"bazel":version,"bazel_exit":status,"tests":{},"aborted":[]}
if (staging/"bep.jsonl").exists():
    for raw in (staging/"bep.jsonl").read_text().splitlines():
        ev=json.loads(raw)
        if "testSummary" in ev:
            report["tests"][ev["id"]["testSummary"]["label"]]=ev["testSummary"].get("overallStatus")
        if "aborted" in ev and ev["aborted"].get("description"):
            report["aborted"].append({"reason":ev["aborted"].get("reason"),"description":ev["aborted"]["description"]})
(evidence/"suite.json").write_text(json.dumps(report,indent=2))
print("Gazelle",source,version,status,output.decode(errors="replace")[-1600:],flush=True)
raise SystemExit(status)
