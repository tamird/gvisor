"""Check the artifacts actually selected by Make after joining the image rules."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
from urllib.parse import unquote, urlsplit

import qualification_report as report


BASIC = ("alpine", "busybox", "tmpfile")
CUDA = ("gpu/cuda-tests", "gpu/cuda-tests-12-8")
SHARED = "//images:source_contexts"
TARGETS = ["//images/basic:" + image + "_context" for image in BASIC] + [SHARED]


def context_members(path, label, arch):
    """Read the inputs that contribute to each Make image tag."""
    with tarfile.open(path) as archive:
        files = {str(PurePosixPath(m.name)): m for m in archive if m.isfile() or m.islnk()}
        if label == SHARED:
            tracked = set(subprocess.check_output(["git", "ls-files", "--", "images/basic"], text=True).splitlines())
            basic = {name for name in files if name.startswith("images/basic/")}
            if tracked != basic:
                raise ValueError(f"Shared basic sources: missing {tracked - basic}; unexpected {basic - tracked}")
            for name in sorted(tracked):
                with archive.extractfile(files[name]) as f:
                    if hashlib.file_digest(f, "sha256").hexdigest() != report.file_record(Path(name))["sha256"]:
                        raise ValueError("Changed source in shared context: " + name)
            for image in CUDA:
                m = files["images/" + image + "/run_sample"]
                with archive.extractfile(m) as f:
                    elf = f.read(20)
                if not m.mode & 0o111 or elf[:6] != b"\x7fELF\x02\x01" or int.from_bytes(elf[18:20], "little") != {"x86_64": 62, "aarch64": 183}[arch]:
                    raise ValueError("Wrong CUDA helper architecture or mode: " + image)
            images = CUDA
        else:
            image = "basic/" + label.split(":")[1].removesuffix("_context")
            if set(files) != {"images/" + image + "/image.tar"}:
                raise ValueError(f"Wrong native image context for {image}: {list(files)}")
            images = (image,)
        tags = {}
        for image in images:
            names = [n.removeprefix("images/") for n in files if n.startswith("images/" + image + "/")]
            ordered = subprocess.check_output(["sort", "-fd"], input="\n".join(names) + "\n", text=True,
                                              env=dict(os.environ, LC_ALL="C")).splitlines()
            tag = hashlib.sha256()
            for name in ordered:
                with archive.extractfile(files["images/" + name]) as f:
                    tag.update(f"{hashlib.file_digest(f, 'sha256').hexdigest()}  {name}\n".encode())
            tags[image] = tag.hexdigest()[:16]
        return tags, len(basic) if label == SHARED else 0


def run(invoke, record_execution, evidence, private, output_root):
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != TARGETS:
        raise ValueError("Unexpected build targets")
    options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc"]
    result = {"status": "INCOMPLETE", "source": config["source"], "product": config["product"], "architectures": {}, "errors": []}
    try:
        for arch in ("x86_64", "aarch64"):
            phase = "contexts-" + arch
            row = result["architectures"][arch] = {"artifacts": {}, "expected_tags": {}}
            execution = private / (phase + ".json")
            built = invoke(phase, ["build", "--config=" + arch, *options, "--remote_download_outputs=all",
                            "--@io_bazel_rules_go//go/config:race=false", "--execution_log_json_file=" + str(execution),
                            "--noexecution_log_sort", *TARGETS], "build", targets=TARGETS)
            if execution.is_file():
                record_execution(execution, arch, "contexts")
            if built["status"] != "PASS":
                result["errors"].append(phase + " failed")
                continue
            events = evidence / ("invocation-" + phase) / "events.jsonl"
            owners = set()
            for line in events.read_text().splitlines():
                event = json.loads(line)
                identity = event.get("id", {}).get("targetCompleted", {})
                if identity.get("label") in TARGETS and not identity.get("aspect"):
                    owners.add((report.Owner.parse(identity), "default"))
            outputs = report.advertised_build_outputs(events, owners)
            if len(outputs) != len(TARGETS) or {o["identity"]["label"] for o in outputs} != set(TARGETS):
                raise ValueError("Missing configured context artifact: " + arch)
            for item in outputs:
                label = item["identity"]["label"]
                uri = urlsplit(item["uri"])
                if uri.scheme != "file" or uri.netloc:
                    raise ValueError("Context was not materialized: " + label)
                path = Path(unquote(uri.path))
                if report.file_record(path) != {k: item[k] for k in ("bytes", "sha256")}:
                    raise ValueError("Build output digest differs: " + label)
                tags, basic_count = context_members(path, label, arch)
                row["artifacts"][label] = {"output": item, "basic_sources": basic_count}
                row["expected_tags"].update(tags)
            expected = row["expected_tags"]
            script = Path("test/rbe/image_context_tags.py").resolve()
            json_path = evidence / ("make-tags-" + arch + ".json")
            checked = invoke("make-tags-" + arch, ["run", "--config=x86_64", *options, "@rules_python//python/bin:python", "--",
                             str(script), arch, json.dumps(expected), str(json_path)], "command", targets=[])
            if checked["status"] != "PASS":
                result["errors"].append("Real Make/context tag mismatch: " + arch)
        smoke = invoke("basic-docker-smoke", ["run", "--config=x86_64", *options, "@rules_python//python/bin:python", "--",
                       str(Path("test/rbe/image_basic_smoke.py").resolve()), str(evidence / "basic-smoke.json")], "command", targets=[])
        if smoke["status"] != "PASS":
            result["errors"].append("Basic rebuild, cached load or Docker runtime check failed")
        if result["errors"]:
            raise RuntimeError("; ".join(result["errors"]))
        result["status"] = "PASS"
    finally:
        report.write_json(evidence / "image-contexts.json", result)
