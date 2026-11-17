#!/bin/bash
set -euo pipefail
status=0
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
run() {
 local name="$1" rc=0; shift
 "$@" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
 printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
 if test "$rc" != 0; then status=1; fi
 return "$rc"
}
trap '"${bazel_cmd[@]}" shutdown || true' EXIT
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
run build make "${make_args[@]}" build TARGETS='//pkg/tcpip/transport/tcp:tcp_test //pkg/tcpip/transport/tcp/test/e2e:tcp_test //test/packetimpact/tests:tcp_rack_native_test //test/packetimpact/tests:tcp_rack_netstack_test' || true
opts='--test_output=errors --nocache_test_results --flaky_test_attempts=1 --test_env=GO_TEST_WRAP_TESTV=1 --test_sharding_strategy=disabled'
run owners make "${make_args[@]}" test TARGETS='//pkg/tcpip/transport/tcp:tcp_test //pkg/tcpip/transport/tcp/test/e2e:tcp_test //pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test' "OPTIONS=$opts" || true
packet_opts="$opts --test_filter=TestR"
if run linux make "${make_args[@]}" test TARGETS=//test/packetimpact/tests:tcp_rack_native_test "OPTIONS=$packet_opts"; then
 run netstack make "${make_args[@]}" test TARGETS=//test/packetimpact/tests:tcp_rack_netstack_test "OPTIONS=$packet_opts" || true
fi
logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
if ! python3 - "$logs" "$RESULT_DIR" <<'PY'
import shutil,sys
from pathlib import Path
import xml.etree.ElementTree as ET
logs,out=map(Path,sys.argv[1:])
checks={
 'pkg/tcpip/transport/tcp/tcp_test':('Test',),
 'pkg/tcpip/transport/tcp/test/e2e/tcp_test':('TestRepeatedRetransmissionTimeoutThreshold','TestZeroWindowSend'),
 'pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test':('Test',),
 'test/packetimpact/tests/tcp_rack_native_test':('TestRTOAfterPartialACK/no_ack','TestRTOAfterPartialACK/half_segment','TestRTOInterruptsCubicRecovery','TestRACKTailProbeACK'),
 'test/packetimpact/tests/tcp_rack_netstack_test':('TestRTOAfterPartialACK/no_ack','TestRTOAfterPartialACK/half_segment','TestRTOInterruptsCubicRecovery','TestRACKTailProbeACK'),
}
for label,expected in checks.items():
 folder=out/label;folder.mkdir(parents=True,exist_ok=True)
 for name in ('test.xml','test.log'):
  shutil.copy2(logs/label/name,folder/name)
 cases=ET.parse(folder/'test.xml').getroot().findall('.//testcase')
 assert cases,label
 assert not [c.attrib for c in cases if any(c.find(t) is not None for t in ('skipped','failure','error'))],label
 names={c.get('name','') for c in cases}
 for name in expected:
  assert any(n.startswith(name) for n in names),(label,name,sorted(names))
 print(label,len(cases),'cases, zero skips')
PY
then status=1; fi
run lint make "${make_args[@]}" lint || true
run lint-cc make "${make_args[@]}" lint-cc || true
cat > "$RESULT_DIR/performance-scope.txt" <<'SCOPE'
This rebase retains the original RTO threshold algorithm and upstream spurious-recovery bookkeeping. It provides no new performance measurement. Broad matched networking benchmark comparisons (iperf, nginx, redis and relevant concurrency variants), plus loss/recovery-sensitive throughput comparisons, remain required before readiness. Historical measurements apply only to their recorded source commits. No performance exemption is asserted.
SCOPE
exit "$status"
