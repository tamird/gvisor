import json
import os
from pathlib import Path
import shutil
import sys

python_bin = Path(sys.executable).resolve().parent
python3 = python_bin / "python3"
if sys.version_info < (3, 11) or not python3.is_file() or not python3.samefile(sys.executable):
    raise RuntimeError(f"Pinned interpreter has no matching python3 executable: {sys.executable}")
native_bazel = Path(os.environ.pop("QUALIFICATION_NATIVE_BAZEL"))
inside_bazel = shutil.which("bazel")
print(f"Native Bazel before/inside interpreter: {native_bazel} / {inside_bazel}", flush=True)
os.environ["PATH"] = str(python_bin) + os.pathsep + os.environ.pop("QUALIFICATION_NATIVE_PATH")
resolved_bazel = shutil.which("bazel")
if resolved_bazel is None or not native_bazel.samefile(resolved_bazel):
    raise RuntimeError("Pinned interpreter did not retain the native Bazel wrapper")
os.environ["QUALIFICATION_WORK_DEADLINE"] = os.environ["APPLICABILITY_WORK_DEADLINE"]
os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
print(f"Qualification reporter interpreter: {sys.version}", flush=True)
lane = os.environ["QUALIFICATION_LANES"]
if lane not in ("license-check", "license-fetch"):
    raise ValueError(f"Unexpected license qualification lane: {lane}")
os.environ.pop("QUALIFICATION_REQUIRED_CASES", None)
os.execvp("bash", ["bash", "test/rbe/actions.sh"])
