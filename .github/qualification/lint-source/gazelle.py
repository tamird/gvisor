import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

root = Path.cwd()
out = Path(os.environ["RUNNER_TEMP"]) / "gazelle-qualification"
out.mkdir()
sha, parent = os.environ["SOURCE"], os.environ["BASE"]
record = {"source": sha, "parent": parent, "prior_alias_controls": "https://github.com/tamird/gvisor/actions/runs/41214197177/artifacts/13047627449", "commands": {}}
def now():
    return datetime.now(timezone.utc).isoformat()
def save():
    (out/"verdict.json").write_text(json.dumps(record, indent=2) + "\n")
def git(*args):
    return subprocess.check_output(["git", *args], text=True)
def bazel(name,*args):
    entry = record["commands"][name] = {"started": now()}
    res = subprocess.run(["bazel","--nohome_rc","--nosystem_rc",*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
    filtered = [line for line in res.stdout.splitlines() if any(v in line for v in ("# reformat","ERROR:","Error in ","(https://","not formatted","no such","Build completed"))]
    entry.update(exit=res.returncode, finished=now(), diagnostics=filtered[-35:])
    print(name, entry, flush=True)
    save()
    return res.returncode
assert git("rev-parse","HEAD").strip() == sha
assert git("rev-parse","HEAD^").strip() == parent
touched = {f for f in git("diff","--name-only",parent,sha).splitlines() if f.endswith(".bzl") or Path(f).name in ("BUILD", "MODULE.bazel")}
record["product_bazel_files"] = sorted(touched)
save()
bazel("whole_tree_before","build","//:buildifier")
formatter=bazel("canonical_format","run","//tools/lint:format")
changed=set(git("diff","--name-only").splitlines())
incidental=changed-touched
record["formatter_files"]=sorted(changed)
record["incidental"]=sorted(incidental)
if incidental:
    subprocess.run(["git","restore","--worktree","--",*sorted(incidental)],check=True)
patch=git("diff","--",*sorted(touched))
record["patch_sha256"]=hashlib.sha256(patch.encode()).hexdigest()
if patch: (out/"format.patch").write_text(patch)
subprocess.run(["git","add","-u","--",*sorted(touched)],check=True)
record["checked_tree"]=git("write-tree").strip()
assignments=[a for a in ast.parse((root/"tools/lint/BUILD").read_text()).body if isinstance(a,ast.Assign) and a.targets[0].id=="BUILDIFIER_FLAGS"]
flags=ast.literal_eval(assignments[0].value)
config=root/".buildifier.json"
scope=bazel("changed_bazel_file_check","run","@buildifier_prebuilt//buildifier:buildifier","--",*flags,"--config="+(str(config) if config.exists() else "off"),"--mode=check","--lint=warn","--",*(str(root/f) for f in sorted(touched)))
clean=bazel("clean_root_alias","build","//:gazelle_check")
status=root/"bazel-bin/tools/lint/gazelle.status"
record["status_file"]=status.read_text().strip() if status.exists() else None
if not incidental:
    whole=bazel("whole_tree_after","build","//:buildifier")
else:
    whole=None
record["whole_tree_after"]=whole
record["pass"]=formatter==scope==clean==0 and whole in (0,None) and record["status_file"]=="0"
record["completed"]=now()
save()
print(json.dumps(record),flush=True)
raise SystemExit(0 if record["pass"] else 1)
