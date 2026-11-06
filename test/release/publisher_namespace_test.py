"""Exercise the actual unprivileged launcher before materializing the full APT closure."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["TEST_TMPDIR"]) / "launcher"
subprocess.run(["/usr/bin/dpkg-deb", "-x", Path(sys.argv[1]).resolve(), root], check=True)
bwrap = root / "usr/bin/bwrap"
bwrap.chmod(0o755)
if not os.getuid():
    raise RuntimeError("the local publisher must be exercised without root")
cmd = [str(bwrap), "--unshare-all", "--die-with-parent", "--new-session", "--uid", "0", "--gid", "0",
       "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
       "--clearenv", "--", "/usr/bin/true"]
p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
decision = {"uid": os.getuid(), "child_exit": p.returncode, "launcher": "Ubuntu 24.04 bubblewrap 0.9.0-1ubuntu0.3; unprivileged private path",
            "diagnostic": p.stdout[-1000:]}
for k in ["kernel/apparmor_restrict_unprivileged_userns", "kernel/unprivileged_userns_clone", "user/max_user_namespaces"]:
    f = Path("/proc/sys") / k
    if f.exists():
        decision[k] = f.read_text().strip()
print(decision, flush=True)
(out / "namespace.json").write_text(json.dumps(decision, indent=2) + "\n")
p.check_returncode()
