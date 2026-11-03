"""Identify the original failure in two bounded test logs without retrying."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile

from cas_inputs import blob

root = Path(os.environ["RUNNER_TEMP"])
archive = (root / "original.zip").read_bytes()
assert hashlib.sha256(archive).hexdigest() == "0d45db8f8b717b2435c89ff3e3b4e20517014d5386dd5e7dff7f000f43c07e83"
with zipfile.ZipFile(io.BytesIO(archive)) as source:
    assert source.namelist() == ["verdict.json"]
    original = json.loads(source.read("verdict.json"))
assert original["source"] == "7418e0ff3e05db3e7622fc199a8fd033da67630c"
identifiers = {"stock": "e3549627-93f1-406d-a7de-b960fea5d45a", "fixed": "f6f5204a-1cf2-4fd5-af7f-b7e7f676b268"}
vocabulary = set("""
a an the of or by is in from with to on failed failure cannot could can not found no such file directory
or permission permissions operation permitted refused denied invalid argument arguments missing error
fatal panic refused exists already executable exec executing execution run running startup start starting
boot initializing setting setting-up settingup creating create make setup configure mount mounted
mountpoint mounter mode mounts tmpfs filesystem overlay overlayfs proc sys dev rootfs root runsc
sentry sandbox gofer systrap ptrace seccomp namespace unshare user network net veth linux sandboxed
link addr address mtu netlink dev device tools binary test tests command host shared socket input output
failed-to-parse parse load loader loading open opening read write path stat chdir mkdir read-only
timeout deadline exceeded disconnected closed network unreachable unsupported supported service
timed out context canceled killed signal exit code status system call function implemented command-not-found
bash sh ip sed grep dirname mount chmod which iproute2 rtnetlink_test.sh setlink_test.sh setlink_test
test_veth01 test_veth02 netns cap_sys_admin cap_net_admin runfiles setup_container setupcontainer
symlink resolve resolution too many levels symbolic links mountinfo namespaces cgroup cgroups
apparmor memory cpus platform backends resources buildbuddy firecracker log losing skipped interpreting
working process terminal standard stdin stdout stderr sync cap capability capabilities procfs devpts
portmapping runsc-sandbox runsc-gofer file-access directfs group gid uid map bin usr var tmp home
public transport new network-child linux-amd64 file-system did unable specified unsupported target
systemd not-a-directory executable-file permission-denied cannot-find rtnetlink answers mountpoint
ELF interpreter mount_setattr cannot-execute source sourced bundle missing-dependencies runfiles.bash
test-binary readlink waited initialization failure creating subprocess container
""".lower().split())
marker = re.compile(r"(?i)\b(?:cannot|unable|failed|failure|fatal|error|panic|denied|permitted|not found|no such|unsupported|not supported|killed|exception|exit status|read-only|skip)\b")
result = {"workload_repeated": False, "original_carrier": original["source"], "originals": {}}
for arm, invocation in identifiers.items():
    row = original["arms"][arm]
    assert row["invocation"] == invocation and row["attempt"]["status"] == "FAILED" and row["mtu_readings"] == {}
    record, = [v for v in row["reports"] if v["name"] == "test.log"]
    assert 0 < record["bytes"] < 12 * 1024
    data = blob({"hash": record["sha256"], "sizeBytes": record["bytes"]}, invocation)
    phrases = (
        "setting up network", "creating default loopback interface", "creating loopback link and routes",
        "creating links and routes", "urpc method", "containerManager.SetNetworkArgs", "broken pipe",
        "connection reset by peer", "connection refused", "EOF", "operation not permitted", "permission denied",
        "the sandbox exited before the container started (see the sandbox logs)",
    )
    matched = []
    tokens = []
    for line in data.decode(errors="replace").splitlines():
        if "setting up network" in line:
            matched.extend(s for s in phrases if s in line)
        if marker.search(line):
            words = [w for w in re.findall(r"[a-z_][a-z0-9_.-]*", line.lower()) if w in vocabulary]
            if words:
                tokens.append(words[:48])
    result["originals"][arm] = {**record, "invocation": invocation, "diagnostic_words": tokens[-24:], "network_error_phrases": matched}
(root / "veth-public/verdict.json").write_text(json.dumps(result, indent=2) + "\n")
