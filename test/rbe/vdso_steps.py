"""Run isolated vDSO source checks before the existing fixed-fixture race test."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


root = Path(sys.argv[1])
abi = root / "abi"
standalone = "cf2264788a7cfe5302a96311d314bf715d50de9a"
parent = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
integration = "caa2f641cb64ef4d22c5b9269d2d54acd0317f81"
carrier = (root / "carrier").read_text().strip()
expected = ["fixed_native", "layout", "parent_old_header", "fixed_old_header", "native_symbols_and_clocks"]
receipt = {"status": "INCOMPLETE", "expected": expected, "executed": {}, "failed": [], "missing": expected,
           "standalone": standalone, "export": "1899f71d79be9b78ed439313ee050ff6b8d62a5d",
           "export_source": "471d6dff5229b99208bf6fec985601067f1f9d69",
           "legacy_header_sha256": hashlib.sha256((root / "legacy/sys/time.h").read_bytes()).hexdigest(),
           "native_header_sha256": hashlib.sha256(Path("/usr/include/x86_64-linux-gnu/sys/time.h").read_bytes()).hexdigest()}
bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
options = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64", "--compilation_mode=opt",
           "--jobs=4", "--local_resources=memory=8192", "--remote_cache=", "--remote_executor=", "--bes_backend=",
           "--spawn_strategy=local", "--strategy=TestRunner=local"]
marker = "GVISOR_QUALIFICATION_OLD_HEADER"


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    receipt["missing"] = [p for p in expected if p not in receipt["executed"]]
    receipt["failed"] = [p for p, v in receipt["executed"].items() if v.get("verified") is False]
    receipt["verified_at"] = now()
    (abi / "builds.json").write_text(json.dumps(receipt, sort_keys=True))


def select(source):
    subprocess.run(["git", "switch", "--detach", "--quiet", source], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source


def run(phase, source, target, *, legacy=False, test=False):
    select(source)
    row = {"source": source, "owner": target, "started_at": now(), "verified": False}
    receipt["executed"][phase] = row
    save()
    event_path = abi / (phase + ".bep")
    args = [*bazel, "test" if test else "build", *options, "--build_event_json_file=" + str(event_path)]
    if legacy:
        args.append("--action_env=CPATH=" + str(root / "witness"))
    if test:
        args.extend(("--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1"))
    if phase == "native_symbols_and_clocks":
        args.append("--test_env=VDSO_RECEIPTS=" + str(abi))
    log = abi / (phase + ".log")
    with log.open("wb") as out:
        row["native_exit"] = subprocess.run(["timeout", "-k", "10", "1000", *args, target], stdout=out, stderr=subprocess.STDOUT).returncode
    row["execution_finished_at"] = now()
    save()
    body = log.read_text(errors="replace")
    row["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
    row["old_header_witness"] = marker in body
    if row["native_exit"]:
        row["diagnostics"] = []
        for line in body.splitlines():
            if not re.search(r"(fatal error:|error:|^ERROR:)", line, re.I):
                continue
            line = re.sub(r'https?://\S+|/home/runner/\S+|/tmp/\S+', '<path>', line)
            if not re.search(r"(?i)token|credential|password|authorization|gh[pousr]_|https?://|(?:^|\s)(?:env|exec|cd)\s", line):
                row["diagnostics"].append(line[:340])
            if len(row["diagnostics"]) == 4:
                break
        save()
        return False
    if legacy and not row["old_header_witness"]:
        row["failure"] = "old header witness missing"
        save()
        return False
    if test:
        attempts = []
        for line in event_path.read_text().splitlines():
            event = json.loads(line)
            if "testResult" in event:
                info = event["testResult"]
                meta = info.get("executionInfo", {})
                attempts.append({"owner": event["id"]["testResult"]["label"], "status": info.get("status"),
                                 "cached": bool(info.get("cachedLocally") or meta.get("cachedRemotely")),
                                 "strategy": meta.get("strategy")})
        row["attempts"] = attempts
        row["verified"] = attempts == [{"owner": target, "status": "PASSED", "cached": False, "strategy": "local"}]
    else:
        image = Path("bazel-bin/vdso/vdso.so")
        assert image.is_file()
        shutil.copyfile(image, abi / (phase + ".so"))
        row["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        row["verified"] = True
    save()
    return row["verified"]


try:
    save()
    witness = root / "witness/sys"
    witness.mkdir(parents=True)
    (witness / "time.h").write_text('#pragma message("' + marker + '")\n#include "' + str(root / "legacy/sys/time.h") + '"\n')
    native = run("fixed_native", standalone, "//vdso:vdso")
    layout = native and run("layout", standalone, "//vdso:vdso_test", test=True)
    run("parent_old_header", parent, "//vdso:vdso", legacy=True)
    run("fixed_old_header", standalone, "//vdso:vdso", legacy=True)
    if native:
        run("native_symbols_and_clocks", carrier, "//test/rbe:vdso_alias_test", test=True)
    receipt["status"] = "PASS" if not receipt["failed"] and not receipt["missing"] else "FAIL"
finally:
    select(integration)
    save()
sys.exit(int(receipt["status"] != "PASS"))
