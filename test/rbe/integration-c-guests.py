import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

lane = sys.argv[1]
target = "runtime_in_docker_test" if lane == "nested" else "integration_test"
required = ["TestGVisorInDocker/" + case for case in ("Rootful", "Rootful_without_networking", "Rootful_with_host_networking", "Rootful_without_networking_and_cgroupfs", "Rootless", "Rootless_without_networking")] if lane == "nested" else ["TestHostOverlayfsCopyUp", "TestHostOverlayfsRewindDir", "TestLink", "TestStickyDir", "TestPing4Loopback", "TestPing6Loopback"]
tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp/"results"/lane
out.mkdir(parents=True)
opts = ["--test_env=RUNTIME=integration-c", "--test_arg=-test.run=^("+"|".join(required)+")$"] if lane=="guest" else []
args = ["bazel", "--bazelrc="+str(tmp/"images.bazelrc"), "test", "--config=x86_64", "--strategy=TestRunner=local", "--nocache_test_results", "--test_output=errors", "--test_arg=-test.v", "--run_under="+str(tmp/"privileged-fixture"), "--test_timeout=600", *opts, "//test/e2e:"+target]
with (out/"build.log").open("w") as log:
    rc = subprocess.run(args,stdout=log,stderr=subprocess.STDOUT).returncode
for name in ("test.log","test.xml"):
    path=Path("bazel-testlogs/test/e2e")/target/name
    if path.exists(): shutil.copyfile(path,out/name)
text=(out/"test.log").read_text(errors="replace") if (out/"test.log").exists() else ""
states={k:re.findall(r"--- "+k+r": (Test[^ (]+) ",text) for k in ("PASS","SKIP","FAIL")}
record={"source":os.environ["PRODUCT"],"rc":rc,"required":required,"missing":sorted(set(required)-set(states["PASS"])),**states}
(out/"result.json").write_text(json.dumps(record,indent=2)+"\n")
print(json.dumps(record))
sys.exit(bool(rc or record["missing"] or states["FAIL"] or states["SKIP"]))
