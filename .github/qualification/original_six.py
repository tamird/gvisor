"""Read six fixed original Bazel output objects and classify their source diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib import error, request

ORIGIN="https://remote.buildbuddy.io"
INV="7323fde8-cb0d-4d84-a7f1-b5972b3327a8"
INPUTS={
    "docprocessor":("058cf120fd4042cb5d36667591ec0c28527b438cd094d19260b4063145698e10",3478),
    "sandbox_py":("c24b0a2a6bf3f804daed545ce2cfee1207510ccd6462678ed79605d33d930470",15756),
    "runsc_coverage":("7e227cd5e2680382dd59c8d9be788f676d9a5f0069bff7c046cc51e85b07167a",904),
    "runsc_race_coverage":("2f857b710ec3a957a32edbc66fd22d878ea956a0e47b5114727ff3d631441d96",913),
    "sentry_coverage":("3ad8c1c8daa685da32e8a839a63c085f98d8f33cedc3e42f7ce8b4d18537bccc",960),
    "sentry_race_coverage":("6cab7886b0ebcabba4cfd8bce392e8ecf30d1b482f9cf1237da6fb08483fe94e",969),
}
key=os.environ.pop("BUILDBUDDY_API_KEY")
assert key
class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None
opener=request.build_opener(NoRedirect)
sensitive=re.compile(r"(?i)(?:authorization|x-buildbuddy-api-key|api.token|remote.header|password|credential|access.key|cookie|github_pat_|gh[pousr]_[a-z0-9]{20,}|://[^/\s]*@)")
CASES = frozenset(['TestMain', 'TestProcessDocuments_BasicTransform', 'TestProcessDocuments_TimeoutEnforcement', 'TestProcessDocuments_NetworkModes', 'TestProcessDocuments_SnapshotRestore', 'TestProcessDocuments_ReadOnlyEnforcement', 'TestProcessDocuments_WorkingDir', 'TestProcessDocuments_ExitCodeFailure', 'test_exec_dmesg', 'test_exec_timeout', 'test_exec_with_args', 'test_exec_invalid_command_or_args', 'test_sandbox_options', 'test_non_root_networking_error', 'test_create_bundle', 'test_init_env_list_and_dict', 'test_init_env_overrides_path', 'test_init_env_malformed_raises_error', 'test_exec_env_flags', 'test_runtime_dir_ownership_and_cleanup', 'test_close_idempotent', 'test_runtime_dir_creation_error', 'test_state_dir_creation_error', 'test_create_bundle_makedirs_error', 'test_sandbox_subprocess_timeout', 'test_sandbox_subprocess_error', 'test_invalid_networking_mode', 'test_invalid_networking_type', 'test_network_modes', 'test_network_mode_enum_bundle', 'test_network_sandbox_nonroot_raises_error', 'test_network_mode_none_real_sandbox', 'test_network_mode_host_real_sandbox', 'test_find_runsc_not_found', 'test_custom_bind_mount_readonly', 'test_custom_bind_mount_readwrite', 'test_custom_tmpfs_mount', 'test_custom_proc_mount', 'test_dict_mount_configuration', 'test_mount_validation_errors', 'test_container_default_working_dir', 'test_container_working_dir', 'test_container_working_dir_relative', 'test_bad_working_dir', 'test_exec_cwd_override', 'test_exec_bad_cwd', 'test_mount_factory_methods'])
TOOLS = ['bash', 'sh', 'python', 'python3', 'realpath', 'pwd', 'mkdir', 'tee', 'uname', 'curl', 'wget', 'tar', 'gzip', 'gunzip', 'unzip', 'patch', 'cat', 'head', 'tail', 'sed', 'awk', 'grep', 'tr', 'find', 'ln', 'cp', 'rm', 'mv', 'chmod', 'cd', 'make', 'make.bash', 'go', 'gofmt', 'gcc', 'g++', 'clang', 'clang++', 'ld', 'strip', 'ar', 'cc', 'c++', 'mount', 'umount', 'rmdir', 'unshare', 'clone', 'clone3', 'execve', 'open', 'openat', 'create', 'start', 'restore', 'runsc', 'gvisor_sentry', 'build_cover.sh', 'go-allow-internal.patch', 'gopath.zip', 'go.mod', 'go.sum', 'main.go', 'gocache', 'GOCACHE', 'GOMODCACHE', 'GOROOT', 'GOROOT_BOOTSTRAP', 'GOTOOLCHAIN', 'GOFLAGS', 'GOPATH', 'cmd/go', 'go-bootstrap', 'linux-amd64.tar.gz', 'src.tar.gz', 'dl.google.com', 'bazel-out', '.gocache', '/dev/kvm', '/proc/self/ns', '/proc/self/uid_map', '/proc/self/gid_map', '/proc/sys/kernel/unprivileged_userns_clone', '/dev/fuse', '/dev/net/tun', 'sandboxexec', 'systrap', 'rootless', 'rootfs', 'sandbox', 'bundles', 'nsenter']
TERMS = ['command not found', 'no such file or directory', 'cannot find', 'not found in', 'no executable', 'failed to find', 'permission denied', 'operation not permitted', 'device or resource busy', 'invalid argument', 'read-only file system', 'no space left', 'network is unreachable', 'connection reset', 'connection refused', 'no such host', 'name resolution', 'not a directory', 'file exists', 'not implemented', 'not supported', 'not allowed', 'not permitted', 'needs root', 'must be root', 'root privileges', 'unexpected end of file', 'not in gzip format', 'archive', 'child returned status', 'unrecoverable', 'not recoverable', 'unexpected eof', 'build constraints exclude', 'no go files', 'not in std', 'not in goroot', 'not a main package', 'not an importable package', 'missing go.sum', 'checksum mismatch', 'module lookup disabled', 'outside module root', 'requires cgo', 'requires external linking', 'cannot use', 'unknown revision', 'go: downloading', 'go: cannot', 'go: errors', 'verifying module', 'x509:', 'certificate signed by unknown authority', 'tls handshake timeout', 'could not resolve host', 'could not resolve proxy', 'unsupported architecture', 'segmentation fault', 'killed', 'failed to create', 'failed to launch', 'failed to start', 'failed to set up', 'failed to set', 'failed to mount', 'failed to restore', 'failed to delete', 'failed to cleanup', 'failed to clean', 'failed to run', 'failed to open', 'failed to read', 'fork/exec', 'failed to write', 'failed to copy', 'mount namespace', 'user namespace', 'network namespace', 'null network', 'mount source', 'cannot mount', 'bind mount', 'umount', 'runsc binary is not found', 'unable to find', 'no such device', 'unable to create sandbox', 'unexpected return code', 'returned non-zero', 'failed with exit', 'failed to resolve', 'no such process', 'already exists', 'resource temporarily unavailable', 'cannot allocate memory', 'network sandbox', 'manifest unknown', 'docker', 'expected writing', 'expected error message', 'expected command', 'namespace creation', 'newuidmap', 'newgidmap', 'userns', 'pivot_root', 'creating sandbox', 'creating container', 'creating child', 'subcommand failed', 'failed to execute', 'exit status', 'shared libraries', 'cannot open shared object', 'buildid', 'missing sentry', 'sandbox binary', 'sidecar', 'disallowed', 'cannot execute', 'working directory', 'no binary', 'failed to get', 'permissionerror', 'filenotfounderror', 'cannot map', 'executable file', 'operation not supported', 'setup_network', 'connection timed out', 'runsc path', 'self-exe', 'insecure root', 'namespace', 'exiting now', 'not mounted']
EXCEPTIONS = ['AssertionError', 'RuntimeError', 'ValueError', 'OSError', 'FileNotFoundError', 'PermissionError', 'ChildProcessError', 'CalledProcessError', 'TimeoutExpired', 'SandboxError', 'Error', 'NotADirectoryError', 'IsADirectoryError', 'TypeError', 'KeyError', 'EOFError', 'CalledProcessError', 'CommandError']
FILES = frozenset(['docprocessor_test.go', 'docprocessor.go', 'sandbox_test.py', 'sandbox.py', 'build_cover.sh', 'main.go', 'go.mod', 'go.sum', 'go-allow-internal.patch', 'make.bash'])
ERRNOS = ("EPERM","EACCES","ENOENT","EBUSY","EINVAL","EAGAIN","E2BIG","EINTR","EROFS","ENOMEM","ENOSPC","SIGSYS","SIGCHLD","SIGABRT","SIGSEGV","SIGKILL")
def classify(data):
    cases={}; records=[]
    for no,line in enumerate(data.decode(errors="replace").splitlines(),1):
        line=line.strip();lower=line.lower()
        for status,case in re.findall(r"--- (PASS|FAIL|SKIP): (Test[A-Za-z0-9_]+)",line):
            if case in CASES:cases[case]=status
        for case in CASES:
            if case.startswith("test_") and re.search(r"^(?:ERROR|FAIL): "+re.escape(case)+r"\b",line):cases[case]=line.split(":",1)[0]
        frame=[]
        m=re.search(r'File "([^"]+)".*?line (\d{1,5})',line)
        if m and Path(m[1]).name in FILES:frame=[Path(m[1]).name,int(m[2])]
        m=re.search(r"(?:^|/)([\w.-]+\.(?:go|py|sh)):(?: line )?(\d{1,5}):",line)
        if m and m[1] in FILES:frame=[m[1],int(m[2])]
        tags=[t for t in TERMS if t in lower]
        words=[t for t in TOOLS if re.search(r"(?<![a-zA-Z0-9_.+-])"+re.escape(t)+r"(?![a-zA-Z0-9_.+-])",line)]
        exc=[x for x in EXCEPTIONS if re.search(r"\b"+x+r"\b",line)]
        errno=[x for x in ERRNOS if re.search(r"\b"+x+r"\b",line)]
        codes=[int(x) for x in re.findall(r"(?i)(?:exit (?:code|status)|errno|child returned status|returned non-zero exit status|status code|returncode)[:= ]+(\d{1,3})\b",line) if int(x)<=255]
        if any([frame,tags,words,exc,errno,codes]) and len(records)<100:
            records.append({"line":no,"frame":frame,"terms":tags[:12],"tools":words[:12],"exceptions":exc,"errno":errno,"codes":codes[:6]})
    return {"cases":cases,"source_records":records}
result={"invocation":INV,"objects":{}}
for name,(sha,n) in INPUTS.items():
    row=result["objects"][name]={"sha256":sha,"bytes":n,"verified":False}
    url=ORIGIN+"/file/download?invocation_id="+INV+"&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F"+sha+"%2F"+str(n)
    try:
        with opener.open(request.Request(url,headers={"x-buildbuddy-api-key":key}),timeout=45) as res:
            assert res.geturl()==url and res.status==200
            b=res.read(n+1)
        assert len(b)==n and hashlib.sha256(b).hexdigest()==sha
        row.update(classify(b));row["verified"]=True
    except (OSError,ValueError,AssertionError) as exc:row["read_error"]=getattr(exc,"code",type(exc).__name__)
data=json.dumps(result,separators=(",",":")).encode()
assert len(data)<65000 and key.encode() not in data and not sensitive.search(data.decode())
(Path(os.environ["RUNNER_TEMP"])/"canonical-six-errors.json").write_bytes(data)
