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
required = Path(os.environ["RUNNER_TEMP"]) / "ruby-http-distro-required-cases.json"
required.write_text(json.dumps({'//test/image:qualification_ruby_native_test': ['image/TestRuby'], '//test/image:qualification_ruby_systrap_test': ['image/TestRuby'], '//test/rbe/ruby_distro:image_identity_test': ['test/rbe/ruby_distro/image_identity_test'], '//tools/lint:buildifier': ['tools/lint/buildifier/buildifier']}, indent=2) + "\n")
os.environ["QUALIFICATION_REQUIRED_CASES"] = str(required)
os.execvp("bash", ['bash', 'test/rbe/actions.sh', '--bazel-option=--config=rbe', '--bazel-option=--strategy=TestRunner=remote', '--bazel-option=--nocache_test_results', '--bazel-option=--runs_per_test=1', '--bazel-option=--test_sharding_strategy=disabled', '--bazel-option=--flaky_test_attempts=1', '--bazel-option=--remote_download_outputs=minimal', '--bazel-option=--nozip_undeclared_test_outputs'])
