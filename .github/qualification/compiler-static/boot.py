"""Run one already verified executable in a directory containing only its bytes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

root, expected = Path(sys.argv[1]), sys.argv[2]
tool=root/"tool"
assert list(root.iterdir()) == [tool] and hashlib.sha256(tool.read_bytes()).hexdigest()==expected

def enter():
    os.chroot(root)
    os.chdir("/")

try:
    p=subprocess.run(["/tool"],input=b"",capture_output=True,env={"TZ":"UTC"},preexec_fn=enter,timeout=20)
    print(json.dumps({"exit":p.returncode,"stdout_hex":p.stdout.hex()[:160],"stdout_bytes":len(p.stdout),
                      "stderr_sha256":hashlib.sha256(p.stderr).hexdigest(),
                      "stderr_diagnostic":p.stderr.decode(errors="replace")[:250]}))
except OSError as err:
    print(json.dumps({"exec_errno":err.errno,"exec_error":err.strerror}))
except subprocess.TimeoutExpired:
    print(json.dumps({"timeout_s":20}))
