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
result = root / "split/result.json"
source = "bd8c827e2eaad36ffba2c4abcca0faf40ae122f3"
owner = "//pkg/sentry/loader/vdsodata:vdso_arch"
archive_owner = "//pkg/sentry/loader/vdsodata:vdsodata"
original_sha = "a54912f01589481ba440b57df27e4733ba3a2fc521585cebeeea499c582d1251"
bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
options = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64", "--compilation_mode=opt",
           "--jobs=4", "--local_resources=memory=8192", "--remote_cache=", "--remote_executor=", "--bes_backend=",
           "--spawn_strategy=local", "--strategy=TestRunner=local",
           "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
r = {"status": "INCOMPLETE", "phase": "source", "source": source, "owner": owner, "split_verified": False,
     "expected": ["native_links", "amd64_embed", "arm64_embed"], "executed": {}, "images": {}, "links": {},
     "original_failure": {"artifact": 13331402265, "stderr_sha256": original_sha}, "options": options}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    r["missing"] = [p for p in r["expected"] if p not in r["executed"]]
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
            "linker_lines": linker_lines(data), "diagnostics": small_lines(data)}


def small_lines(data):
    lines = linker_lines(data)
    for raw in data.decode(errors="replace").splitlines():
        match = re.search(r"([A-Za-z0-9_./+-]{1,160}\.(?:cc|c|h|go|bzl|S):\d+(?::\d+)?:\s*(?:fatal )?error:\s*.{1,330})", raw, re.I)
        if match and not re.search(r"(?i)https?://|token|password|authorization|credential|github_pat|gh[pousr]_|AKIA", raw):
            lines.append(re.sub(r"(?:/[^\s:]+/)", "<path>/", match.group(1))[:330])
        if len(lines) >= 5:
            break
    return lines[:5]


def invoke(label, args, timeout=500):
    with (root / (label + ".out")).open("wb") as out, (root / (label + ".err")).open("wb") as err:
        return subprocess.run(["timeout", "-k", "10", str(timeout), *bazel, *args], stdout=out, stderr=err).returncode


def build(phase, target, extra=()):
    r["phase"] = phase
    row = {"owner": target, "verified": False, "started_at": now()}
    r["executed"][phase] = row
    save()
    events = root / (phase + ".bep")
    row["native_exit"] = invoke(phase, ["build", *options, *extra, "--keep_going", "--build_event_publish_all_actions",
                                        "--build_event_json_file=" + str(events), "--build_event_json_file_path_conversion=false", target], 1000)
    row["execution_finished_at"] = now()
    if row["native_exit"]:
        r["status"] = "FAIL"
    save()
    actions = []
    if events.exists():
        for line in events.open():
            event = json.loads(line)
            action = event.get("action", {})
            key = event.get("id", {}).get("actionCompleted", {})
            if action.get("success"):
                out = key.get("primaryOutput", "")
                if action.get("type") == "CppLink" and re.fullmatch(r"bazel-out/[^/]+/bin/vdso/vdso\.so", out):
                    actions.append(out)
            elif action and len(row.setdefault("failures", [])) < 4:
                item = {"exit": action.get("exitCode"), "type": action.get("type"), "stderr": local_stderr(action, outbase)}
                row["failures"].append(item)
        if row["native_exit"]:
            row["diagnostics"] = small_lines((root / (phase + ".err")).read_bytes())
    save()
    return row, actions


def image(path, machine):
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[16:18], "little") == 3
    assert int.from_bytes(data[18:20], "little") == machine
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "machine": machine}


def inspect_links(outputs):
    expression = 'mnemonic("CppLink", deps(' + owner + ', 3))'
    code = invoke("aquery", ["aquery", *options, "--output=jsonproto", "--include_param_files", expression])
    r["aquery_exit"] = code
    if code:
        return False
    graph = json.loads((root / "aquery.out").read_bytes())
    fragments = {str(p["id"]): p for p in graph["pathFragments"]}
    artifacts = {str(a["id"]): str(a["pathFragmentId"]) for a in graph["artifacts"]}

    def path(file_id):
        parts = []
        fragment = artifacts[str(file_id)]
        while fragment and fragment != "0":
            part = fragments[fragment]
            parts.append(part["label"])
            fragment = str(part.get("parentId", ""))
        return "/".join(reversed(parts))

    for action in graph["actions"]:
        if action.get("mnemonic") != "CppLink":
            continue
        for name in map(path, action["outputIds"]):
            if name not in outputs:
                continue
            argv = list(action.get("arguments", []))
            for params in action.get("paramFiles", []):
                argv.extend(params.get("arguments", []))
            target = "arm64" if any("vdso_arm64.lds" in arg for arg in argv) else "amd64" if any("vdso_amd64.lds" in arg for arg in argv) else None
            if not target:
                continue
            row = {"configured_output": name, "compiler_driver": Path(argv[0]).name, "fuse_ld_lld": "-fuse-ld=lld" in argv,
                   "nostdlib": "-nostdlib" in argv, "linker_script": "vdso_" + target + ".lds",
                   "target_triples": [a for a in argv if re.fullmatch(r"--?target=[A-Za-z0-9_.-]{1,64}", a)]}
            linked = image(Path(name), {"amd64": 62, "arm64": 183}[target])
            staged = image(Path("bazel-bin/pkg/sentry/loader/vdsodata") / ("vdso_" + target + ".so"), linked["machine"])
            row["matches_embed_input"] = staged == linked
            r["links"][target] = row
            r["images"][target] = linked
            (root / "split" / (target + ".so")).write_bytes(Path(name).read_bytes())
            save()
    return (set(r["links"]) == {"amd64", "arm64"} and all(row["matches_embed_input"] and row["nostdlib"] for row in r["links"].values())
            and r["links"]["arm64"]["fuse_ld_lld"])


def archive_output(phase):
    # rules_go's default output is export data. compilation_outputs selects
    # the full Go archive, including the embedded vDSO bytes.
    sets, selected = {}, []
    for line in (root / (phase + ".bep")).open():
        event = json.loads(line)
        key = event.get("id", {})
        if "namedSet" in key:
            sets[key["namedSet"]["id"]] = event["namedSetOfFiles"]
        complete = key.get("targetCompleted", {})
        if complete.get("label") == archive_owner and not complete.get("aspect"):
            for group in event.get("completed", {}).get("outputGroup", []):
                if group.get("name") == "compilation_outputs":
                    selected.extend(f["id"] for f in group.get("fileSets", []))
    files, visited = [], set()
    while selected:
        name = selected.pop()
        if name in visited:
            continue
        visited.add(name)
        node = sets[name]
        files.extend(f for f in node.get("files", []) if f["name"].endswith(".a"))
        selected.extend(f["id"] for f in node.get("fileSets", []))
    return files


try:
    save()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])
    outbase = Path(subprocess.check_output([*bazel, "info", "output_base"], stderr=subprocess.DEVNULL, text=True).strip()).resolve()
    row, links = build("native_links", owner)
    if row["native_exit"] == 0:
        r["split_verified"] = row["verified"] = inspect_links(links)
        save()
    if r["split_verified"]:
        for cpu, extra in (("amd64", []), ("arm64", ["--config=aarch64", "--@io_bazel_rules_go//go/config:race=false", "--@io_bazel_rules_go//go/config:pure=true"])):
            row, _ = build(cpu + "_embed", archive_owner, [*extra, "--output_groups=+compilation_outputs"])
            if row["native_exit"]:
                continue
            candidates = archive_output(cpu + "_embed")
            row["archive_candidates"] = len(candidates)
            if len(candidates) == 1:
                item = candidates[0]
                uri = urlsplit(item["uri"])
                assert uri.scheme == "file" and uri.netloc in ("", "localhost")
                file = Path(unquote(uri.path)).resolve()
                assert file.is_relative_to(outbase) and file.stat().st_size < 100 * 1024**2
                archive = file.read_bytes()
                elf = (root / "split" / (cpu + ".so")).read_bytes()
                row["archive_sha256"] = hashlib.sha256(archive).hexdigest()
                row["image_sha256"] = hashlib.sha256(elf).hexdigest()
                row["verified"] = (archive.startswith(b"!<arch>\n") and archive.count(elf) == 1
                                   and (not item.get("digest") or item["digest"] == row["archive_sha256"])
                                   and row["image_sha256"] == r["images"][cpu]["sha256"])
            save()
    r["status"] = "PASS" if all(r["executed"].get(p, {}).get("verified") for p in r["expected"]) else "FAIL"
    r["phase"] = "complete"
except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
    import traceback
    r["reader_error"] = type(exc).__name__
    r["reader_lines"] = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name == "vdso_steps.py"]
finally:
    save()
sys.exit(int(r["status"] != "PASS"))
