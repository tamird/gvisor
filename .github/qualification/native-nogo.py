"""Compare the ELF inputs and action keys under the two Nogo strip modes."""

import collections
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = root / "nogo-receipts"
events = Path(".build_events.json")
bazel = ["bazel", f"--bazelrc={root}/nogo-resources.bazelrc"]
owners = ["//pkg/sentry/loader/vdsodata:vdsodata_nogo", "//pkg/sentry/platform/systrap/sysmsg:sysmsg_nogo"]


def write(name, val):
    (out / (name + ".json")).write_text(json.dumps(val, sort_keys=True, indent=2) + "\n")


def file_hash(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def diag(p):
    with p.open(errors="replace") as f:
        return [s.strip()[:400] for s in f if ("ERROR:" in s or "not found" in s) and not any(t in s.lower() for t in ("token", "http", "bearer"))][:10]


def build(mode):
    # A real two-owner run builds the files under Nogo’s architecture transitions.
    # Force the old setting only in the old arm; Make's never is the new default.
    extra = ["--strip=sometimes"] if mode == "sometimes" else []
    cmd = [*bazel, "test", "--config=buildkite", "--config=nogo", *extra, *owners]
    log = root / f"{mode}.log"
    with log.open("w") as f:
        rc = subprocess.run(["timeout", "--kill-after=10s", "16m", *cmd], stdout=f, stderr=f).returncode
    sums = {}
    if events.is_file():
        with events.open() as f:
            for line in f:
                e = json.loads(line)
                if "testSummary" in e:
                    sums[e["id"]["testSummary"]["label"]] = e["testSummary"]["overallStatus"]
    receipt = {"exit":rc,"testSummaries":sums,"errors":diag(log)}
    write(mode,receipt)
    assert rc==0 and sums=={k:"PASSED" for k in owners}, receipt
    names = {}
    for arch in ("amd64","arm64"):
        files = [
            f"pkg/sentry/loader/vdsodata/vdso_{arch}.so",
            f"pkg/sentry/platform/systrap/sysmsg/sighandler.built-in.{arch}.bin",
            f"pkg/sentry/platform/systrap/sysmsg/sighandler_{arch}.go"]
        for n in files:
            matches = sorted({file_hash(p) for p in Path("bazel-out").glob("*/bin/" + n) if p.is_file()})
            names[n] = matches
    objects = collections.defaultdict(set)
    for p in Path("bazel-out").glob("*/bin/pkg/sentry/platform/systrap/sysmsg/sighandler.built-in.bin.o"):
        b = p.read_bytes()
        assert b[:4]==b"\x7fELF"
        objects[str(int.from_bytes(b[18:20],"little"))].add(hashlib.sha256(b).hexdigest())
    receipt.update(digests=names,linkedStubs={k:sorted(h) for k,h in objects.items()})
    write(mode,receipt)
    assert set(objects)=={"62","183"} and all(len(h)==1 for h in names.values()), names
    return receipt


def actions(mode):
    # Only Nogo roots: test-specific rc flags govern selection in the actual
    # wildcard, so reproduce that selection in the query rather than asking
    # for every build action beneath //... .
    q = 'deps(tests(attr("tags", "nogo", //...)))'
    cmd = [*bazel, "aquery", f"--strip={mode}", "--output=jsonproto", "--noinclude_artifacts", "--noinclude_commandline", q]
    p = root / f"action-{mode}.json"
    log = root / f"action-{mode}.log"
    with p.open("w") as f, log.open("w") as err:
        rc = subprocess.run(["timeout", "--kill-after=10s", "8m", *cmd], stdout=f, stderr=err).returncode
    write("analysis-"+mode,{"exit":rc, "errors":diag(log)})
    assert rc == 0, (mode,rc)
    a = json.loads(p.read_bytes())
    labels = {t["id"]: t["label"] for t in a["targets"]}
    selected = sorted({v for v in labels.values() if v.endswith("_nogo") and not v.startswith("@")})
    groups = collections.defaultdict(list)
    for act in a["actions"]:
        groups[labels.get(act.get("targetId"),"(none)"),act["mnemonic"]].append(act.get("actionKey",""))
    assert all(l in selected for l in owners)
    return {k:sorted(v) for k,v in groups.items()}, selected


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == os.environ["PRODUCT"]
    assert shutil.which("aarch64-linux-gnu-gcc") is None
    start = int(time.time())
    old, new = build("sometimes"), build("never")
    embeds = old["digests"]==new["digests"] and old["linkedStubs"]==new["linkedStubs"]
    write("outputs",{"identical":embeds,"source":os.environ["PRODUCT"],"old":old,"new":new})
    old_a, old_s = actions("sometimes")
    new_a, new_s = actions("never")
    diffs = {str(k):[len(old_a.get(k,[])),len(new_a.get(k,[]))] for k in old_a.keys()|new_a.keys() if old_a.get(k)!=new_a.get(k)}
    classes = collections.Counter(k[1] for k in old_a.keys()|new_a.keys() if old_a.get(k)!=new_a.get(k))
    write("action-differences",{"groups":diffs,"rootCandidates":new_s})
    receipt = {"verified":embeds and old_s==new_s, "source":os.environ["PRODUCT"],"candidate":"6aa894aa1",
        "sameEmbedBytes":embeds,"oldActions":sum(map(len,old_a.values())),"newActions":sum(map(len,new_a.values())),
        "nogoCandidates":len(new_s),"differentOwnerMnemonicGroups":len(diffs),"differenceMnemonics":classes,"started":start,"finished":int(time.time())}
    write("verdict",receipt)
    assert receipt["verified"],receipt


if __name__ == "__main__":
    main()
