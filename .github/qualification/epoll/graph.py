"""Admit the committed fixture and its real Bazel source label before Run."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

root=Path(sys.argv[1])
p=json.loads((root/"task/plan.json").read_bytes())
assert sha256((root/"task/request.json").read_bytes()).hexdigest()==p["request_sha256"]
def git(*args):
    return subprocess.check_output(["git",*args],text=True).strip()
assert git("rev-parse","HEAD")==p["source"]
assert git("rev-parse","HEAD^{tree}")==p["trees"][p["source"]]
assert git("rev-parse","HEAD^")==p["products"]["source"]
assert git("rev-parse","HEAD:pkg/sentry/epollqualification")==p["tracked_package_tree"]
expected="//pkg/sentry/fsimpl/fuse:epoll_qualification_test.go"
query="labels(srcs, //pkg/sentry/fsimpl/fuse:fuse_test) intersect set("+expected+")"
result=subprocess.run(["bazel","query","--noshow_progress","--lockfile_mode=off","--output=label",query],capture_output=True,text=True,timeout=420)
if result.returncode:
    print("Bazel source admission failed with exit",result.returncode)
    raise SystemExit(1)
assert set(result.stdout.splitlines())=={expected}
assert not git("status","--porcelain","--untracked-files=no")
print("The committed FUSE fixture belongs to the original fuse_test target.")
