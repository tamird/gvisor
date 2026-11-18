#!/bin/bash
# Prepared canonical first proof; no publication or launch before review.
set -euo pipefail
if test "${ATOMPTR_PROOF_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 84m env ATOMPTR_PROOF_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 02fc62f697b2957e37d53be359ba820676c0e738
test "$BASE" = b35ded894d3e1f6982cdb6fe13a2ba3b76343188
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
results="$RESULT_DIR/atomicptr-caller-proof"
mkdir -m 700 "$results"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 if test -f "$results/apparmor-original.txt"; then
  read -r original < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local name="$1" cap="$2" rc=0 started
 shift 2
 started=$(python3 -c 'import time; print(time.monotonic_ns())')
 printf '%q ' "$@" >> "$results/commands.txt"; printf '\n' >> "$results/commands.txt"
 timeout --kill-after=15s "$cap" "$@" 2>&1 | tee "$results/$name.log" || rc=$?
 printf '%s\t%s\t%s\n' "$name" "$(( ($(python3 -c 'import time; print(time.monotonic_ns())')-started)/1000000 ))" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
python3 - "$support/expected.json" <<'IDENTITY'
import hashlib,json,subprocess,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text())
for revision in (x['source'],x['base']):
 subprocess.run(['git','cat-file','-e',revision+'^{tree}'],check=True,timeout=5)
source_headers=subprocess.check_output(['git','cat-file','commit',x['source']],timeout=5).split(b'\n\n',1)[0].splitlines()
assert [line[7:].decode('ascii') for line in source_headers if line.startswith(b'parent ')]==[x['base']]
headers=subprocess.check_output(['git','cat-file','commit',x['base']],timeout=5).split(b'\n\n',1)[0].splitlines()
assert [line[7:].decode('ascii') for line in headers if line.startswith(b'parent ')]==[x['baseParent']]
for name,digest in x['sourceSHA256'].items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
subprocess.run(['git','diff','--quiet'],check=True,timeout=5)
subprocess.run(['git','diff','--cached','--quiet'],check=True,timeout=5)
IDENTITY
bazel --version > "$results/bazel-version.txt"
test "$(cat "$results/bazel-version.txt")" = 'bazel 9.3.1'
uname -a > "$results/worker.txt"
lscpu > "$results/cpu.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-active.txt"
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 -c opt --strip=never --nostamp --jobs=3 --enable_runfiles --remote_executor= --remote_cache= --bes_backend= --color=no --curses=no)
test_flags=(--nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_timeout=900 --test_output=all --test_env=GO_TEST_WRAP_TESTV=1)
proof=//pkg/sentry/platform/kvm:atomicptr_codegen_test
inputs=//pkg/sentry/platform/kvm:atomicptr_codegen_inputs
stage owner-query 1m "${bazel_cmd[@]}" query --output=label "set($proof $inputs)"
run_test() {
 local name="$1" cap="$2" rc=0 capture=0 began
 shift 2
 began=$(python3 -c 'import time; print(time.time_ns()//1000000)')
 printf '%s\n' "$began" > "$results/$name-start-ms.txt"
 stage "$name" "$cap" "${bazel_cmd[@]}" test "${flags[@]}" "${test_flags[@]}" "--build_event_json_file=$results/$name.bep" "$@" || rc=$?
 printf '%s\n' "$rc" > "$results/$name-exit.txt"
 timeout --kill-after=5s 45s python3 "$support/collect.py" tests "$results/$name.bep" "$results/$name-evidence" "$began" "$support/expected.json" "$@" || capture=$?
 test "$rc" -eq 0 && test "$capture" -eq 0
}
# Reuse only the actual verified prior lint and four Go-owner passes.
python3 - "$support/expected.json" "$results/reused-functional-evidence.json" <<'REUSE'
import hashlib,json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text());prior=x['reusedQualification'];receipt=prior['receipt']
assert hashlib.sha256((json.dumps(receipt,indent=2)+'\n').encode()).hexdigest()==prior['receiptSHA256']
assert receipt['source']==x['reuseSource'] and receipt['run']==44335049809
old=prior['sourceAndToolchainInputsSHA256'];assert set(old)<set(x['sourceSHA256'])
assert set(x['sourceSHA256'])-set(old)==set(x['reuseAddedInputs'])
assert {name for name in old if old[name]!=x['sourceSHA256'][name]}==set(x['reuseChangedInputs'])
assert len(receipt['passedOwnerRecords'])==4 and receipt['passedXMLCaseRecords']==53 and receipt['passedTopLevelFunctions']==11
assert len(receipt['failedGoldenOwners'])==8 and receipt['sourceLintAndFormat'].startswith('PASS')
Path(sys.argv[2]).write_text(json.dumps(prior,indent=2)+'\n')
REUSE
python3 - "$support/expected.json" "$results/reused-golden-evidence.json" <<'GOLDEN_REUSE'
import hashlib,json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text());prior=x['reusedGoldenQualification'];receipt=prior['receipt']
assert hashlib.sha256((json.dumps(receipt,indent=2)+'\n').encode()).hexdigest()==prior['receiptSHA256']
assert receipt['identity']['source']==x['reuseSource'] and receipt['run']==44338516920
old=prior['sourceAndToolchainInputsSHA256'];assert set(old)<set(x['sourceSHA256'])
assert set(x['sourceSHA256'])-set(old)==set(x['reuseAddedInputs'])
assert {name for name in old if old[name]!=x['sourceSHA256'][name]}==set(x['reuseChangedInputs'])
assert receipt['goldenInvocationExit']=='0' and len(receipt['goldens'])==8
assert {r['label'] for r in receipt['goldens']}==set(x['goldenTargets'])
assert all(r['passed'] and r['caseCount']==1 for r in receipt['goldens'])
assert receipt['outcome']['steps']['qualify']=='failure'
Path(sys.argv[2]).write_text(json.dumps(prior,indent=2)+'\n')
GOLDEN_REUSE
printf '%s\n' 'Historical443350 lint/4Goowners retained; reuse only unchanged3Goowners+443385 eightgoldens. Changed codegen/source lint require fresh pass. No production/runtime/performance claim.' > "$results/generator-prefix-complete.txt"
# Reuse actual current-source lint and changed owner, preserving outer failure.
python3 - "$support/expected.json" "$results/reused-current-prefix.json" <<'CURRENT_REUSE'
import hashlib,json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text());prior=x['reusedCurrentPrefix'];receipt=prior['receipt']
assert hashlib.sha256((json.dumps(receipt,indent=2)+'\n').encode()).hexdigest()==prior['receiptSHA256']
assert receipt['source']==x['historicalInspectorSource'] and receipt['run']==44378538905
assert receipt['sourceLintPassed'] and receipt['changedOwnerPassed'] and receipt['caseCount']==22
assert receipt['overall']=='FAILURE retained' and 'TestCompilationUnitWithoutLines' in receipt['names']
assert receipt['cleanSource']['actualFinalHead']==x['historicalInspectorSource'] and not receipt['cleanSource']['trackedWorktreeDirty'] and not receipt['cleanSource']['indexDirty']
Path(sys.argv[2]).write_text(json.dumps(prior,indent=2)+'\n')
CURRENT_REUSE
# Reuse the exact current-source lint and full changed-owner qualification.
python3 - "$support/expected.json" "$results/reused-qualified-inspector.json" <<'QUALIFIED_REUSE'
import hashlib,json,sys
from pathlib import Path
x=json.loads(Path(sys.argv[1]).read_text());prior=x['reusedQualifiedInspector'];receipt=prior['receipt']
assert hashlib.sha256((json.dumps(receipt,indent=2)+'\n').encode()).hexdigest()==prior['receiptSHA256']
assert receipt['source']==x['qualifiedInspectorSource'] and receipt['run']==44391663447
assert receipt['sourceLintPassed'] and receipt['changedOwnerPassed'] and receipt['allEightRequiredSubcasesPresent']
assert receipt['observedCases']==len(receipt['names']) and receipt['observedCases']>0
assert receipt['overall']=='FAILURE retained'
assert receipt['outcome']['actualFinalHead']==x['qualifiedInspectorSource'] and not receipt['outcome']['trackedWorktreeDirty'] and not receipt['outcome']['indexDirty']
Path(sys.argv[2]).write_text(json.dumps(prior,indent=2)+'\n')
QUALIFIED_REUSE
# Fresh owner exercises the actual production sources before any Sentry links.
test_flags+=(--test_arg=-test.v --test_arg=-test.count=1)
stage source-lint 10m make DOCKER_BUILD=false "BAZEL=${bazel_cmd[*]}" "BAZEL_OPTIONS=${flags[*]}" lint
run_test changed-codegen-owner 5m //pkg/sentry/platform/kvm/codegen:codegen_test
# Preserve passed owner evidence before a dependency/compiler failure.
if ! test -x /usr/bin/aarch64-linux-gnu-gcc || ! test -x /usr/bin/aarch64-linux-gnu-g++; then
 stage official-apt-source 30s python3 - "$results" <<'APT_SOURCE'
import hashlib,json,re,sys
from pathlib import Path

def official_source(text):
    routes={
        'mirror+file:/etc/apt/apt-mirrors.txt':'https://archive.ubuntu.com/ubuntu/',
        'mirror+file:/etc/apt/apt-security-mirrors.txt':'https://security.ubuntu.com/ubuntu/',
        'http://archive.ubuntu.com/ubuntu/':'https://archive.ubuntu.com/ubuntu/',
        'http://security.ubuntu.com/ubuntu/':'https://security.ubuntu.com/ubuntu/',
        'http://security.ubuntu.com/ubuntu':'https://security.ubuntu.com/ubuntu/',
        'https://archive.ubuntu.com/ubuntu/':'https://archive.ubuntu.com/ubuntu/',
        'https://security.ubuntu.com/ubuntu/':'https://security.ubuntu.com/ubuntu/',
    }
    def replace(match):
        assert match[2] in routes, match[2]
        return match[1]+routes[match[2]]
    result,count=re.subn(r'^(URIs:[ \t]*)([^\r\n]*)',replace,text,flags=re.M)
    assert count>0
    assert re.sub(r'^URIs:[^\r\n]*','',result,flags=re.M)==re.sub(r'^URIs:[^\r\n]*','',text,flags=re.M)
    return result

results=Path(sys.argv[1])
original=Path('/etc/apt/sources.list.d/ubuntu.sources').read_text()
(results/'original-ubuntu.sources').write_text(original)
os_fields=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '='in line)
assert os_fields['ID']=='ubuntu' and os_fields['VERSION_CODENAME']=='noble'
modified=official_source(original)
assert Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg').is_file()
(results/'original-ubuntu.sources').write_text(original)
(results/'official-ubuntu.sources').write_text(modified)
(results/'apt-source-policy.json').write_text(json.dumps({'operationOnly':True,'onlyURIsChanged':True,'originalSHA256':hashlib.sha256(original.encode()).hexdigest(),'activeSHA256':hashlib.sha256(modified.encode()).hexdigest(),'package':'gcc-13-aarch64-linux-gnu','version':'13.3.0-6ubuntu2~24.04.1cross1','packageSHA256':'154d549389d13010bc0ef2e89c7870e22c4193afa69335f8e57c5fc891e75578'},indent=2)+'\n')
APT_SOURCE
 apt_options=(-o "Dir::Etc::sourcelist=$results/official-ubuntu.sources" -o Dir::Etc::sourceparts=-)
 stage apt-index 90s sudo apt-get "${apt_options[@]}" update
 timeout --kill-after=2s 15s apt-cache "${apt_options[@]}" --no-all-versions show gcc-13-aarch64-linux-gnu > "$results/gcc-package-metadata.txt"
 python3 - "$results/gcc-package-metadata.txt" <<'APT_PACKAGE'
import sys
from pathlib import Path
text=Path(sys.argv[1]).read_text();paragraphs=[part for part in text.split('\n\n') if part.strip()];assert len(paragraphs)==1
fields=dict(line.split(': ',1) for line in paragraphs[0].splitlines() if ': ' in line and not line[0].isspace())
assert fields['Package']=='gcc-13-aarch64-linux-gnu' and fields['Architecture']=='amd64'
assert fields['Version']=='13.3.0-6ubuntu2~24.04.1cross1' and fields['Size']=='21069414'
assert fields['SHA256']=='154d549389d13010bc0ef2e89c7870e22c4193afa69335f8e57c5fc891e75578'
APT_PACKAGE
 stage cross-compiler-prerequisite 8m sudo env DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install --yes --no-install-recommends gcc-aarch64-linux-gnu g++-aarch64-linux-gnu gcc-13-aarch64-linux-gnu=13.3.0-6ubuntu2~24.04.1cross1
fi
stage cross-tool-readiness 30s bash -c '
set -euo pipefail
for tool in gcc g++ ld objcopy nm; do
 path="/usr/bin/aarch64-linux-gnu-$tool"
 test -x "$path"
 readlink -f "$path" > "$1/cross-$tool-path.txt"
 sha256sum "$path" > "$1/cross-$tool-sha256.txt"
 "$path" --version > "$1/cross-$tool-ready.txt"
done
/usr/bin/aarch64-linux-gnu-gcc -dumpmachine > "$1/cross-gcc-target.txt"
test "$(cat "$1/cross-gcc-target.txt")" = aarch64-linux-gnu
' bash "$results"
# Go-only args already apply to fresh inspector and production owners.
gate_rc=0
run_test production-caller-proof 48m "$proof" || gate_rc=$?
# Retain declared ELF/archives even when actual DWARF validation fails.
query_rc=0
if timeout --kill-after=5s 1m python3 "$support/collect.py" declared "$results/production-caller-proof.bep" "$results" "$support/expected.json"; then
 timeout --kill-after=5s 1m python3 "$support/collect.py" products "$results/inputs.paths" "$results/proof-binary.paths" "$results/declared-products" "$support/expected.json" "$results/production-caller-proof-evidence/actual-inline-reports.json" || query_rc=$?
else query_rc=1; fi
# Resolve retained runfile aliases against the canonical execution root only.
timeout --kill-after=10s 1m "${bazel_cmd[@]}" info "${flags[@]}" execution_root > "$results/execution-root.txt" || query_rc=1
aquery_rc=0
timeout --kill-after=10s 1m "${bazel_cmd[@]}" aquery "${flags[@]}" "${test_flags[@]}" --output=jsonproto --include_artifacts --include_param_files 'mnemonic("GoCompilePkg|GoLink", filter("//pkg/sentry/kernel:kernel|//pkg/sentry/platform/kvm:kvm|//pkg/sentry/platform/kvm:atomicptr_noinline|//runsc/cmd/sentry:gvisor_sentry", deps(//pkg/sentry/platform/kvm:atomicptr_codegen_inputs)))' > "$results/selected-actions.json" 2> "$results/actions-query.log" || aquery_rc=$?
if test "$aquery_rc" -eq 0; then
 timeout --kill-after=5s 30s python3 "$support/collect.py" actions "$results/selected-actions.json" "$results/actual-configured-actions.json" "$results/declared-products/verified.json" "$(cat "$results/execution-root.txt")" || aquery_rc=$?
fi
printf '%s\n' "$query_rc" > "$results/product-capture-exit.txt"
printf '%s\n' "$aquery_rc" > "$results/action-capture-exit.txt"
if test "$gate_rc" -ne 0 || test "$query_rc" -ne 0 || test "$aquery_rc" -ne 0; then
 exit 1
fi
printf '%s\n' 'Current-source canonical caller gate first proof only; no complete-runtime/performance/merge or original-PR modification claim.' > "$results/scope.txt"
