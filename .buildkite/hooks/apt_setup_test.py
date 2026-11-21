import os
from pathlib import Path
import subprocess
import tempfile

text = Path(__file__).with_name("pre-command").read_text()
subprocess.run(["bash", "-n", str(Path(__file__).with_name("pre-command"))], check=True)
start = text.index("packages=(")
array = text[start:text.index(")\n", start) + 2]
condition_start = text.index("if ! dpkg-query")
condition = text[condition_start:text.index("; then", condition_start) + len("; then")]
probe = "set -euo pipefail\n" + array + "\n" + condition + "\n echo setup-required\nelse\n echo skip-APT\nfi\n"
names = subprocess.check_output(["bash", "-c", array + '\nprintf "%s\\n" "${packages[@]}"'], text=True).splitlines()
names.append("linux-headers-" + os.uname().release)
assert len(names) == 18, names
arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
    env = dict(os.environ, DPKG_ADMINDIR=directory)
    def write_status(broken=None, missing=None):
        rows = []
        for index, name in enumerate(names):
            if name == missing:
                continue
            flag = "reinstreq" if name == broken else "ok"
            architecture = "all" if index == 0 else arch
            rows.append(f"Package: {name}\nStatus: install {flag} installed\nArchitecture: {architecture}\nVersion: 1\nDescription: disposable native status database\n")
        Path(directory, "status").write_text("\n".join(rows))
    for label, broken, missing, want in [
        ("prepared", None, None, "skip-APT"),
        ("reinstallation-required", names[0], None, "setup-required"),
        ("missing-package", None, names[-1], "setup-required"),
    ]:
        write_status(broken, missing)
        result = subprocess.check_output(["bash", "-c", probe], env=env, text=True).strip()
        print(label, "18 required records:", result, flush=True)
        assert result == want, (label, result)
    write_status()
    block = "set -euo pipefail\n" + text[start:text.index("\nset -x", start)]
    subprocess.run(["bash", "-c", block], env=env, check=True)
    print("actual complete package setup block: prepared18 skip, no APT output", flush=True)
