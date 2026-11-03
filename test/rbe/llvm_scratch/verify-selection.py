"""Check the supplier's actual repository declarations against LLVM's overlays."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from urllib.request import urlopen

supplier = Path("supplier").resolve()
records = Path(os.environ["RUNNER_TEMP"]) / "package-evidence"
records.mkdir()
product = os.environ["PRODUCT"]
parent = "21a5215f89afe39a50bfab5f5ff10a75fffa614a"
versions = {
    "23.1.2": "85ac560262434c9ccfc0c183ec22d4138ed647fb",
    "24.0.0": "8cb8b5767b826dade657e3544d081e37b7aef0a9",
}
backport = "instrumentor-template.patch"
patch = supplier / "3rd_party/llvm-project/23.x/patches" / backport
expected = [f"{source}/{version}" for source in (parent, product) for version in versions]
results = {}
selections = {}
failures = {}
try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD", "HEAD^"], cwd=supplier, text=True).split() == [product, parent]
    module = (supplier / "MODULE.bazel").read_text()
    assert module.count('LLVM_VERSION = "23.1.2"') == 1
    index_path = supplier / "llvm_versions.json"
    index = json.loads(index_path.read_text())
    for version, upstream in versions.items():
        overlay = records / version / "llvm"
        overlay.mkdir(parents=True)
        url = f"https://raw.githubusercontent.com/llvm/llvm-project/{upstream}/utils/bazel/llvm-project-overlay/llvm/BUILD.bazel"
        with urlopen(url, timeout=60) as response:
            content = response.read()
        (overlay / "BUILD.bazel").write_bytes(content)
        # show_repo needs an index entry to select the future major. It
        # inspects the repository declaration without downloading an archive.
        if version not in index:
            index[version] = {"url": url, "sha256": hashlib.sha256(content).hexdigest()}
    index_path.write_text(json.dumps(index))
    for source in (parent, product):
        subprocess.run(["git", "restore", f"--source={source}", "--", "extensions/llvm.bzl"], cwd=supplier, check=True)
        for version, upstream in versions.items():
            key = f"{source}/{version}"
            try:
                (supplier / "MODULE.bazel").write_text(module.replace('LLVM_VERSION = "23.1.2"', f'LLVM_VERSION = "{version}"'))
                declaration = subprocess.check_output(["bazel", "mod", "show_repo", "@llvm-project"], cwd=supplier, text=True)
                line, = [s for s in declaration.splitlines() if s.strip().startswith("patches = ")]
                labels = re.findall(r"[-.\w]+\.patch", line)
                assert "llvm-bazel-libxml2.patch" in labels, line
                choices = selections[source, version] = labels
                selected = backport in choices
                path = records / version
                check = subprocess.run(["patch", "--dry-run", "--batch", "--forward", "--fuzz=0", "-p1", "-i", patch], cwd=path, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                # The stock 24 selector reproduces the erroneous second application.
                assert selected == (source == parent or version == "23.1.2"), (key, labels)
                assert check.returncode == (0 if version == "23.1.2" else 1), (key, check.stdout)
                if version == "24.0.0":
                    assert b'expand_template(\n    name = "instrumentor_variables_gen"' in (path / "llvm/BUILD.bazel").read_bytes()
                results[key] = {"upstream": upstream, "selected": selected, "patch_exit": check.returncode, "patches": labels}
            except Exception as error:
                failures[key] = repr(error)
    for version in versions:
        old = selections[parent, version]
        assert selections[product, version] == [p for p in old if version == "23.1.2" or p != backport], version
except Exception as error:
    failures["setup or selection comparison"] = repr(error)
finished = datetime.now(timezone.utc).isoformat()
missing = sorted(set(expected) - set(results))
verdict = {"source": product, "parent": parent, "expected": expected, "results": results, "failures": failures, "missing": missing, "skips": [], "executionFinished": finished, "verifiedAt": datetime.now(timezone.utc).isoformat(), "complete": not failures and not missing}
(records / "verdict.json").write_text(json.dumps(verdict, indent=2))
print(json.dumps({"failures": failures, "missing": missing, "complete": verdict["complete"]}))
raise SystemExit(not verdict["complete"])
