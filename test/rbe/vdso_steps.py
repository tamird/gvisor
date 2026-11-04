"""Check the two configured vDSO links selected by the Sentry's embed rule."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


root = Path(sys.argv[1])
result = root / "out/result.json"
source = "caa2f641cb64ef4d22c5b9269d2d54acd0317f81"
owner = "//pkg/sentry/loader/vdsodata:vdso_arch"
original_sha = "a54912f01589481ba440b57df27e4733ba3a2fc521585cebeeea499c582d1251"
bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
options = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64", "--compilation_mode=opt",
           "--jobs=4", "--local_resources=memory=8192", "--remote_cache=", "--remote_executor=", "--bes_backend=",
           "--spawn_strategy=local", "--strategy=TestRunner=local"]
out_pattern = r"bazel-out/[A-Za-z0-9_.+~-]{1,180}/bin/vdso/vdso\.so"
platforms = {"k8": "linux_amd64", "aarch64": "linux_arm64"}
r = {"status": "INCOMPLETE", "phase": "source", "source": source, "owner": owner, "expected": sorted(platforms.values()),
     "original_failure": {"artifact": 13330128632, "stderr_bytes": 124, "stderr_sha256": original_sha},
     "options": options, "diagnostic_status": "INCOMPLETE", "configurations": {}, "actions": {}, "matched": [], "missing": sorted(platforms.values())}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    r["verified_at"] = now()
    result.write_text(json.dumps(r, sort_keys=True))


def linker_lines(data):
    lines = []
    for raw in data.decode(errors="replace").splitlines():
        line = raw.strip()
        match = re.fullmatch(r"(?:[/A-Za-z0-9_.+~-]+/)?((?:[A-Za-z0-9_.+-]+-)?ld(?:\.(?:lld|bfd|gold))?|lld|clang(?:\+\+)?)(?:-[0-9]+)?: (.{1,450})", line)
        if not match:
            continue
        detail = match.group(2)
        if (not re.fullmatch(r"[a-zA-Z0-9 .,;:'/+()=\[\]*_~<>‘’`-]+", detail)
                or re.search(r"(?i)token|password|authorization|credential|github_pat|gh[pousr]_|https?://|AKIA", detail)):
            continue
        # Keep the linker reason and basenames, never the worker's path.
        detail = re.sub(r"[/][^\s':,]+/", "<path>/", detail)
        lines.append(match.group(1) + ": " + detail[:300])
    return lines[:5]


def local_stderr(action, outbase):
    ref = urlsplit(action.get("stderr", {}).get("uri", ""))
    if ref.scheme != "file" or ref.netloc not in ("", "localhost"):
        return {}
    path = Path(unquote(ref.path)).resolve()
    if not path.is_relative_to(outbase) or not path.is_file() or path.stat().st_size > 65536:
        return {}
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return {"bytes": len(data), "sha256": digest, "matches_original": len(data) == 124 and digest == original_sha,
            "linker_lines": linker_lines(data)}


try:
    save()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])
    r["phase"] = "split_build"
    save()
    bep = root / "events.raw"
    with (root / "build.log").open("wb") as out:
        r["native_exit"] = subprocess.run(["timeout", "-k", "10", "900", *bazel, "build", *options, "--keep_going",
                                           "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep),
                                           "--build_event_json_file_path_conversion=false", owner], stdout=out, stderr=subprocess.STDOUT).returncode
    r["execution_finished_at"] = now()
    r["status"] = "FAIL" if r["native_exit"] else "INCOMPLETE"
    save()
    r["phase"] = "configured_outputs"
    save()
    # This is configured-graph inspection only; it cannot execute another link.
    expression = '"%s|%s|%s" % (build_options(target).get("//command_line_option:cpu", ""), build_options(target).get("//command_line_option:platforms", []), target.files.to_list()[0].path)'
    query = subprocess.run(["timeout", "150", *bazel, "cquery", *options, "--output=starlark", "--starlark:expr=" + expression,
                            'filter("vdso:vdso$", deps(' + owner + ', 1))'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    r["cquery_exit"] = query.returncode
    for raw in query.stdout.decode(errors="replace").splitlines():
        cells = raw.split("|")
        if len(cells) != 3 or cells[0] not in platforms or not re.fullmatch(out_pattern, cells[2]):
            continue
        cpu, labels, output = cells
        platform = platforms[cpu]
        if re.search(r"//tools/bazeldefs:" + platform + r"\b", labels):
            r["configurations"][output] = {"cpu": cpu, "platform": "//tools/bazeldefs:" + platform}
    save()
    outbase = Path(subprocess.check_output([*bazel, "info", "output_base"], stderr=subprocess.DEVNULL, text=True).strip()).resolve()
    r["phase"] = "original_action_comparison"
    save()
    for line in bep.open():
        event = json.loads(line)
        key = event.get("id", {}).get("actionCompleted", {})
        match = re.search(r"(" + out_pattern + r")$", key.get("primaryOutput", ""))
        action = event.get("action", {})
        if not match or action.get("type") != "Genrule":
            continue
        output = match[1]
        configuration_id = key.get("configuration", {}).get("id", "")
        row = {"success": bool(action.get("success")), "exit": action.get("exitCode", 0), "type": "Genrule",
               "configuration_id": configuration_id if re.fullmatch("[a-f0-9]{7,128}", configuration_id) else None}
        r["actions"][output] = row
        if not row["success"]:
            row["stderr"] = local_stderr(action, outbase)
            if row["stderr"].get("matches_original"):
                r["matched"].append(output)
        else:
            path = Path(output).resolve()
            if path.is_relative_to(outbase) and path.is_file() and path.stat().st_size < 256 * 1024:
                data = path.read_bytes()
                if data.startswith(b"\x7fELF\x02\x01"):
                    row["elf"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "machine": int.from_bytes(data[18:20], "little")}
        save()
    seen = {cfg["platform"].split(":")[-1] for out, cfg in r["configurations"].items() if out in r["actions"]}
    r["missing"] = sorted(set(platforms.values()) - seen)
    r["relation"] = "MATCHED" if len(r["matched"]) == 1 and r["matched"][0] in r["configurations"] else "NOT_ESTABLISHED"
    if not r["native_exit"] and not r["missing"] and all(row["success"] for row in r["actions"].values()):
        r["status"] = "PASS"
    if (r["native_exit"] == 1 and r["relation"] == "MATCHED" and not r["missing"]
            and len(r["actions"]) == 2 and r["configurations"].keys() == r["actions"].keys()):
        failed_key, = r["matched"]
        passed_key, = r["actions"].keys() - {failed_key}
        failure, success = r["actions"][failed_key], r["actions"][passed_key]
        expected_machine = {"k8": 62, "aarch64": 183}[r["configurations"][passed_key]["cpu"]]
        reasons = [line for line in failure["stderr"]["linker_lines"] if "linker command failed with exit code" not in line]
        if (not failure["success"] and failure["exit"] == 1 and reasons and success["success"]
                and success["exit"] == 0 and success.get("elf", {}).get("machine") == expected_machine):
            r["diagnostic_status"] = "PASS"
    r["phase"] = "complete"
except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
    r["reader_error"] = type(exc).__name__
finally:
    save()
sys.exit(int(r["diagnostic_status"] != "PASS"))
