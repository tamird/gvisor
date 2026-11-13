"""Build gVisor's AF_XDP object as an external module and retain its inputs."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess


AREA = Path(__file__).resolve().parent
ROOT = Path.cwd()
OUT = AREA / "receipts"
CONSUMER = AREA / "consumer"
TARGET = "@gvisor//runsc/sandbox/bpf:af_xdp_ebpf"



def bazel(command: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(AREA / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
         command, "--color=no", "--curses=no", "--lockfile_mode=off", *arguments],
        cwd=CONSUMER, text=True, capture_output=True,
    )


def build(name: str) -> subprocess.CompletedProcess[str]:
    result = bazel("build", "--jobs=3", TARGET)
    (OUT / f"{name}.txt").write_text(result.stderr)
    print(f"{name}: exit {result.returncode}", flush=True)
    if result.returncode:
        print(result.stderr[-4500:], flush=True)
    return result


def read_output(command: str, *arguments: str) -> str:
    result = bazel(command, *arguments)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def capture_object() -> None:
    actions = json.loads(read_output(
        "aquery", "--output=jsonproto", "--include_artifacts=false",
        f'mnemonic("BPFCompile", {TARGET})',
    ))["actions"]
    [action] = actions
    argv = action["arguments"]
    (OUT / "arguments.json").write_text(json.dumps(argv, indent=2) + "\n")
    assert "-nostdinc" in argv and "--target=bpfel" in argv, argv
    exec_root = Path(read_output("info", "execution_root"))
    source = exec_root / argv[argv.index("-c") + 1]
    assert source.read_bytes() == (ROOT / "runsc/sandbox/bpf/af_xdp.ebpf.c").read_bytes()
    inputs = {"source": [str(source.relative_to(exec_root)), sha256(source.read_bytes()).hexdigest()]}
    include_dirs = [exec_root / arg for flag, arg in zip(argv, argv[1:]) if flag in ("-isystem", "-iquote", "-I")]
    for name in ("linux/types.h", "linux/bpf.h", "bpf/bpf_helpers.h"):
        header = next((d / name for d in include_dirs if (d / name).is_file()), None)
        assert header is not None, (name, include_dirs)
        inputs[name] = [str(header.relative_to(exec_root)), sha256(header.read_bytes()).hexdigest()]
    for name in ("linux/types.h", "linux/bpf.h"):
        assert "6.12.91" in inputs[name][0], inputs[name]
    (OUT / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    obj = exec_root / argv[argv.index("-o") + 1]
    content = obj.read_bytes()
    assert content[:6] == b"\x7fELF\x02\x01" and content[16:20] == b"\x01\x00\xf7\x00", content[:20]
    elf = subprocess.check_output(["readelf", "-WhSsr", str(obj)], text=True)
    (OUT / "elf.txt").write_text(elf)
    for symbol in ("xdp_prog", "sock_map", "__license"):
        assert symbol in elf, symbol
    (OUT / "af_xdp_ebpf.o").write_bytes(content)


def main() -> None:
    CONSUMER.mkdir()
    module = CONSUMER / "MODULE.bazel"
    module.write_text(
        'module(name="consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'
        f'local_path_override(module_name="gvisor",path={json.dumps(str(ROOT))})\n'
        'bazel_dep(name="llvm",version="0.8.27")\n'
        'headers=use_extension("@llvm//kernel/extension:kernel_headers.bzl","kernel_headers")\n'
        'headers.configure(default_version="6.12")\n'
        'use_repo(headers,probe_headers="kernel_headers")\n'
    )
    (CONSUMER / "BUILD.bazel").touch()
    (CONSUMER / ".bazelversion").write_text((ROOT / ".bazelversion").read_text())
    (OUT / "consumer.MODULE.bazel").write_text(module.read_text())
    baseline = build("without-17555")
    result = subprocess.run(["git", "write-tree"], text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "7851600d42ab6f20888f887a88d9e709a15c2395"
    subprocess.run(["git", "apply", "--index", str(AREA / "17555.patch")], check=True)
    result = subprocess.run(["git", "write-tree"], text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "2ee6f0cb38fcf2c161f6aadec770bd7ee2986067"
    fixed = build("with-17555")
    fixed.check_returncode()
    capture_object()
    execution_root = Path(read_output("info", "execution_root"))
    sources = {
        "gazelle+/def.bzl": "5878ab285dc7953cf78b34ff34cd635a8753ef35e4ea2336797f25ea61984ef5",
        "llvm+/kernel/extension/kernel_headers.bzl": "383acbcaded721335b342403ba72579811139d8a0ed9f3f51e939ad23e12497e",
    }
    for path, expected in sources.items():
        actual = sha256((execution_root / "external" / path).read_bytes()).hexdigest()
        assert actual == expected, (path, actual, expected)
    (OUT / "extension-digests.json").write_text(json.dumps(sources, indent=2) + "\n")
    assert baseline.returncode != 0 and "non-root Bazel module is forbidden" in baseline.stderr and "go_deps." in baseline.stderr, baseline.stderr[-3500:]



if __name__ == "__main__":
    main()
