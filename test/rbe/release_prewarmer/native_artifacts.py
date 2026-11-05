"""Inspect only the test-group wrapper and the seccheck helper as Linux ELFs."""

import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import uuid
import zipfile

import qualify as q

STOCK = "//test/rbe/release_prewarmer:stock_test_wrapper"
ARM_WRAPPER = "compiled_posix_test_wrapper"
SERVER = "//examples/seccheck:server_cc"


def elf(path, cpu):
    assert path.is_file() and path.stat().st_size <= 200 * 1024 * 1024
    data = path.read_bytes()
    result = q.elf_bytes(data)
    assert result["load"] and result["machine"] == {"amd64": 62, "arm64": 183}[cpu]
    output = subprocess.check_output(["readelf", "-lWd", str(path)], text=True, timeout=45)
    interpreter = re.findall(r"Requesting program interpreter: ([/A-Za-z0-9_.+-]{1,160})\]", output)
    needed = re.findall(r"\(NEEDED\).*?\[([A-Za-z0-9+_.-]{1,100})\]", output)
    assert len(interpreter) == int(result["interp"]) and len(needed) == output.count("(NEEDED)")
    return dict(result, interpreter=interpreter, needed=needed)


def image(arch):
    raw = (q.TMP / "images.zip").read_bytes()
    assert len(raw) == 4965 and hashlib.sha256(raw).hexdigest() == "5f919ba8d820862f00522ab10658beb6738f31c12550c1cda1d8e15ce5973859"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        doc = json.loads(z.read(arch + "-image.json"))
    assert doc["manifest"] == q.PIN[arch] and doc["architecture"] == arch and doc["elf_paths"] == []
    return {"original_artifact": 13100242102, "manifest": doc["manifest"], "elf_paths": []}


def install(bazel):
    root = subprocess.check_output([str(bazel), *q.PROOF, "info", "install_base"], text=True, timeout=60).strip()
    tools = Path(root).resolve(strict=True) / "embedded_tools"
    assert tools.is_dir()
    return tools


def arm_stock(bazel, base, work, row):
    invocation = str(uuid.uuid4())
    bep = work / "wrapper.bep"
    args = [str(bazel), *q.PROOF, "build", *base,
            "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(bep),
            "--invocation_id=" + invocation, "--nobuild_event_json_file_path_conversion", STOCK]
    with (work / "wrapper.log").open("wb") as log:
        result = subprocess.run(["timeout", "--signal=INT", "--kill-after=15", "420", *args], stdout=log, stderr=subprocess.STDOUT)
    row.update(stock_build_exit=result.returncode, requested_invocation=invocation, diagnostics=q.diagnostics(work / "wrapper.log"))
    if result.returncode:
        return []
    # The build-only rule forwards the native wrapper File from the test exec
    # group; cquery prints its actual exec-config path, not a guessed glob.
    with (work / "wrapper-query.err").open("wb") as err:
        result = subprocess.run([str(bazel), *q.PROOF, "cquery", *base, "--output=files", STOCK],
                                stdout=subprocess.PIPE, stderr=err, text=True, timeout=90)
    row["query_exit"] = result.returncode
    assert result.returncode == 0
    paths = [s for s in result.stdout.splitlines() if s.strip()]
    path, = paths
    assert re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]+/bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper", path)
    resolved = Path(path).resolve(strict=True)
    resolved.relative_to(Path("bazel-out").resolve(strict=True))
    built = False
    attempts = 0
    for line in bep.read_text().splitlines():
        e = json.loads(line)
        if "started" in e:
            row["observed_invocation"] = e["started"]["uuid"]
        if e.get("id", {}).get("targetCompleted", {}).get("label", "").lstrip("@") == STOCK:
            built |= bool(e.get("completed", {}).get("success"))
        attempts += int("testResult" in e)
        if "finished" in e:
            row["bep_exit"] = e["finished"]["exitCode"].get("code", 0)
    assert row["bep_exit"] == 0 and row["observed_invocation"] == invocation and built and not attempts
    row.update(path=path, stock=elf(resolved, "arm64"), preflight_test_results=attempts)
    row["choice"] = "stock"
    if not row["stock"]["interpreter"] and not row["stock"]["needed"]:
        return []
    assert row["stock"]["interpreter"] and row["worker"]["elf_paths"] == []
    # Expose the exact embedded tools from this supplier. Only the selected
    # fallback rule gets the feature; the original tool installation is read-only.
    source = install(bazel)
    dest = work / "bazel_tools"
    names = ["tools", "test"]
    for depth in range(3):
        dst = dest.joinpath(*names[:depth]); dst.mkdir()
        src = source.joinpath(*names[:depth])
        skip = names[depth] if depth < 2 else "BUILD"
        for child in src.iterdir():
            if child.name != skip:
                (dst / child.name).symlink_to(child, target_is_directory=child.is_dir())
    src = source / "tools/test/BUILD"
    text = src.read_bytes()
    row["original_build_sha256"] = hashlib.sha256(text).hexdigest()
    assert row["original_build_sha256"] == "20b1b758246fc4e987170550f32c24b5ffab7857c66af622f0f403f2fbf94933"
    mark = b'    name = "compiled_posix_test_wrapper",\n'
    assert text.count(mark) == 1
    new = text.replace(mark, mark + b'    features = ["fully_static_link"],\n')
    (dest / "tools/test/BUILD").write_bytes(new)
    row.update(choice="wrapper_target_only", override_build_sha256=hashlib.sha256(new).hexdigest())
    return ["--override_repository=bazel_tools=" + str(dest)]
