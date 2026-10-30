import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

p = Path(os.environ["RUNNER_TEMP"]) / "website-probe"
secret = os.environ.pop("BUILDBUDDY_API_KEY")
assert secret and "\n" not in secret
rc = p / "private.bazelrc"
rc.write_text(f"build --remote_header=x-buildbuddy-api-key={secret}\nbuild --bes_header=x-buildbuddy-api-key={secret}\n")
rc.chmod(0o600)
flags = ["bazel", "--bazelrc="+str(rc), "build", "--jobs=4", "--curses=no", "--color=no", "--noshow_progress",
         "--workspace_status_command=", "--remote_executor=grpcs://remote.buildbuddy.io", "--remote_cache=grpcs://remote.buildbuddy.io",
         "--bes_backend=grpcs://remote.buildbuddy.io", "--bes_results_url=https://app.buildbuddy.io/invocation/",
         "--remote_download_outputs=toplevel", "--extra_execution_platforms=//test/rbe/website-jekyll:scratch",
         "--spawn_strategy=local", "--strategy=JekyllSite=remote", "--strategy=WebsiteSyscallJSON=local",
         "--strategy=WebsiteSyscallDocs=local", "--verbose_failures", "--build_event_publish_all_actions",
         "--nobuild_event_json_file_path_conversion", "--build_event_json_file="+str(p/"pair.bep")]
try:
    cmd = flags + ["--keep_going", "//website:files", "//website:site_docker_archive", "//website:rendered_site"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=4500)
    log = result.stdout.decode(errors="replace").replace(secret, "[redacted]")
    (p/"bazel.log").write_text(log)
    print("\n".join(s[:650] for s in log.splitlines() if any(t in s for t in ("ERROR:", "Error", "exception", "No such file", "invocation/", "Proofer", "Build completed", "Build did NOT")))[:9100],flush=True)
    events = []
    if (p/"pair.bep").exists():
        for line in (p/"pair.bep").read_text().splitlines():
            e=json.loads(line)
            if "started" in e:
                events.append({"invocation":e["started"]["uuid"]})
            if "action" in e:
                a=e["action"]
                if a.get("type") in ("WebsiteSyscallJSON","WebsiteSyscallDocs","JekyllSite") or not a.get("success", True):
                    events.append({k:a.get(k) for k in ("type","success","exitCode")})
        (p/"pair.bep").unlink()
    (p/"actions.json").write_text(json.dumps({"exit":result.returncode,"events":events},indent=2)+"\n")
finally:
    rc.unlink(missing_ok=True)
if result.returncode:
    sys.exit(result.returncode)
stock, fixed = {}, {}
with tarfile.open("bazel-bin/website/files.tgz","r:*") as archive:
    for entry in archive.getmembers():
        if entry.isfile():
            name=entry.name.removeprefix("./")
            if name.startswith("_site/"):
                stock[name.removeprefix("_site/")] = archive.extractfile(entry).read()
root=Path("bazel-bin/website/rendered_site")
for f in root.rglob("*"):
    if f.is_file():
        fixed[str(f.relative_to(root))] = f.read_bytes()
missing=sorted(stock.keys()-fixed.keys())
extra=sorted(fixed.keys()-stock.keys())
changed=sorted(k for k in stock.keys() & fixed.keys() if stock[k] != fixed[k])
(p/"comparison.json").write_text(json.dumps({"stock":len(stock), "fixed":len(fixed), "identical":len(stock)-len(missing)-len(changed), "missing":missing, "extra":extra, "changed":[(k,hashlib.sha256(stock[k]).hexdigest(),hashlib.sha256(fixed[k]).hexdigest()) for k in changed]},indent=2)+"\n")
diff = []
for k in changed:
    if len(diff)>1800: break
    if not k.endswith((".html",".css",".xml",".txt")):
        continue
    diff.extend(s[:1300] for s in difflib.unified_diff(stock[k].decode(errors="replace").splitlines(),fixed[k].decode(errors="replace").splitlines(),k,k,n=1))
(p/"content.diff").write_text("\n".join(diff[:1800])+"\n")
print(f"Website files: {len(stock)} stock, {len(fixed)} candidate, {len(changed)} changed, {len(missing)} missing, {len(extra)} extra",flush=True)
image_tar=subprocess.check_output(["bazel","cquery","--noshow_progress","--output=files","//website:site_docker_archive"],text=True).strip().splitlines()[-1]
subprocess.run(["docker","load","-i",image_tar],check=True)
info=json.loads(subprocess.check_output(["docker","image","inspect","gvisor-website:qualify"]))[0]
assert info["Config"]["Entrypoint"] == ["/server"] and "8080/tcp" in info["Config"]["ExposedPorts"], info["Config"]
cid=subprocess.check_output(["docker","run","-d","-p","127.0.0.1::8080","gvisor-website:qualify","--custom-domain=*"],text=True).strip()
try:
    port=subprocess.check_output(["docker","port",cid,"8080/tcp"],text=True).strip().rsplit(":",1)[1]
    responses={}
    for url in ("/","/blog/index.xml","/archive.key","/css/main.css"):
        for t in range(8):
            try:
                with urlopen(f"http://127.0.0.1:{port}{url}",timeout=4) as resp:
                    data=resp.read()
                    assert resp.status==200 and data
                    responses[url]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
                    break
            except OSError:
                if t==7: raise
                time.sleep(.5)
    (p/"server.json").write_text(json.dumps({"image":info["Id"],"arch":info["Architecture"],"entrypoint":info["Config"]["Entrypoint"],"ports":list(info["Config"]["ExposedPorts"]),"responses":responses},indent=2)+"\n")
finally:
    subprocess.run(["docker","rm","-f",cid],check=True)
if missing or extra or changed:raise SystemExit(1)
