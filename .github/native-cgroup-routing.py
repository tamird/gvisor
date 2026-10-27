"""Keep only the cgroup test itself local on the native ARM worker."""

import json
import os
import subprocess
import sys
from collections import Counter


target = os.environ["QUALIFICATION_TARGETS"]


def actions(*options):
    result = subprocess.check_output([
        "bazelisk", "aquery", "--config=x86_64", "--compilation_mode=opt",
        "--host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo",
        "--extra_execution_platforms=//test/rbe:native_amd64,//test/rbe:native_arm64",
        "--noinclude_artifacts", "--noinclude_commandline", "--output=jsonproto",
        *options, f'mnemonic("Go.*|TestRunner", deps({target}))',
    ])
    graph = json.loads(result)
    labels = {int(t["id"]): t["label"].removeprefix("@@") for t in graph["targets"]}
    return Counter(
        (labels[int(a["targetId"])], a["mnemonic"], a.get("executionPlatform", ""),
         tuple(sorted((r["key"], r.get("value", "")) for r in a.get("executionInfo", ()))))
        for a in graph["actions"]
    )


before = actions()
expected = Counter()
mnemonics = set()
for (owner, mnemonic, platform, requirements), count in before.items():
    if "local" in dict(requirements) and mnemonic != "TestRunner":
        if owner != target and not owner.startswith(target + "_"):
            raise ValueError(f"Would change another target's routing: {owner} {mnemonic}")
        mnemonics.add(mnemonic)
        requirements = tuple(r for r in requirements if r[0] != "local")
    expected[owner, mnemonic, platform, requirements] += count

if "GoTestGenTest" not in mnemonics:
    raise ValueError("The cgroup test generator did not request local execution")
if not any(owner == target and mnemonic == "TestRunner" and
           platform.endswith("//test/rbe:native_arm64") and "local" in dict(reqs)
           for owner, mnemonic, platform, reqs in before):
    raise ValueError("The public test runner is not local on the ARM execution platform")

modifier = "--modify_execution_info=^(" + "|".join(sorted(mnemonics)) + ")$=-local"
after = actions(modifier)
if after != expected:
    raise ValueError(f"Unexpected routing changes: {expected - after}; {after - expected}")
print(f"Verified the ARM TestRunner stays local; only these Go build actions change: {modifier}", file=sys.stderr)
print(modifier)
