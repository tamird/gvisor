#!/bin/bash
set -euo pipefail
root="$PWD"; q="$root/test/rbe/python-launcher-compat"; mkdir -p client results
patch -d upstream -p1 < "$q/rules_python.patch"
cd client
cp "$q/client.MODULE" MODULE.bazel
cp "$q/probe.py" .
printf '%s\n' 'written by upstream compatibility fixture' > payload.txt
cp "$q/smoke.BUILD" BUILD.bazel
cat >> BUILD.bazel <<'EOF'
load("@rules_python//python:py_runtime.bzl","py_runtime")
load("@rules_python//python:py_runtime_pair.bzl","py_runtime_pair")
py_runtime(name="custom_runtime",interpreter="@python_3_11//:python3",files=["@python_3_11//:files"],python_version="PY3",stub_shebang="#!/usr/bin/python3")
py_runtime_pair(name="custom_pair",py3_runtime=":custom_runtime")
toolchain(name="custom",toolchain=":custom_pair",toolchain_type="@rules_python//python:toolchain_type")
EOF
export USE_BAZEL_VERSION=9.1.1
b=(bazelisk --ignore_all_rc_files build --@rules_python//python/config_settings:bootstrap_impl=native --@rules_python//python/config_settings:venvs_use_declare_symlink=no)
for mode in default custom; do
  extra=()
  if [[ "$mode" == custom ]]; then extra+=(--extra_toolchains=//:custom); fi
  "${b[@]}" "${extra[@]}" //:probe
  python3 - "$mode" "$root/results/$mode.json" <<'PY'
import json,os,subprocess,sys,tempfile
from pathlib import Path
m,file=sys.argv[1:]; exe=Path('bazel-bin').resolve()/'probe'
header=exe.open('rb').read(18)
assert header.startswith(b'\x7fELF') if m=='default' else header.startswith(b'#!/usr/bin/python3'),header
with tempfile.TemporaryDirectory() as temp:
 env=dict(os.environ,PATH='',TMPDIR=temp,RULES_PYTHON_ADDITIONAL_INTERPRETER_ARGS='-X standalone=on')
 for k in ('RUNFILES_DIR','RUNFILES_MANIFEST_FILE','JAVA_RUNFILES','PYTHONPATH'):env.pop(k,None)
 args=['spaced value','--flag','']; run=subprocess.run([str(exe),*args],env=env,text=True,capture_output=True)
 v={'mode':m,'header':header.hex(),'exit':run.returncode,'stderr':run.stderr[-900:],'tmp':temp}
 if run.returncode==0:v['payload']=p=json.loads(run.stdout)
 Path(file).write_text(json.dumps(v,indent=2)+'\n')
 assert run.returncode==0,v
 assert p['argv']==args and p['option']=='on' and p['payload']=='written by upstream compatibility fixture',p
 assert Path(p['prefix']).is_relative_to(temp) and p['prefix']!=p['base_prefix'],p
PY
done
