"""Describe any partial module graph explicitly; no builds or user credentials."""
import json
import os
from pathlib import Path
import re
import subprocess

out = {}
for name, folder in (("stock", "stock"), ("candidate", ".")):
    path = Path(folder)
    p = subprocess.run(["bazel", "mod", "show_repo", "--ignore_dev_dependency",
                        "--lockfile_mode=update", "@org_golang_google_protobuf",
                        "@com_github_containerd_containerd_v2"], cwd=path, text=True, capture_output=True)
    out[name] = {
        "exit": p.returncode,
        "protobuf_emitted": bool(re.search(r'name = "[^"]*org_golang_google_protobuf"', p.stdout)),
        "protobuf_patched": "protobuf.disable-binary-reading-on-init.patch" in p.stdout,
        "errors": [],
    }
    for s in p.stderr.splitlines():
        if not re.match(r"ERROR:|Error in \w+:", s) or any(t in s.lower() for t in ("credential", "token", "secret", "header", "api-key")):
            continue
        s = re.sub(r"https?://[^\s]+", "[URL]", s)
        s = re.sub(r"/home/runner/[\w./+~-]+/external/", "external/", s)
        out[name]["errors"].append(s[:500])
Path(os.environ["RUNNER_TEMP"], "module-diagnostics.json").write_text(json.dumps(out, indent=2)+"\n")
