"""Stage the pinned Linux TamaGo SDK and the guest's offline Go file proxy."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

SDK_URL = "https://github.com/usbarmory/tamago-go/releases/download/tamago-go1.27.3/tamago-go1.27.3.linux-amd64.tar.gz"
SDK_DIGEST = "8f9a42cf0d51d84e5e2f55d93ce7babdc2fdffe9d6468e8d2d172c1f7753acbc"
FIXED_REV = "5d6ca5025d9dfe7e1dff121d55ee5a6027338237"
DISPATCHER = Path("pkg/tcpip/transport/tcp/dispatcher.go")
GUEST = Path("test/tamago_dispatch/guest")


def sdk_path(name: str) -> str:
    path = PurePosixPath(name)
    if ".." in path.parts:
        raise ValueError(f"unexpected SDK archive path: {name!r}")
    return str(path.relative_to("/usr/local/tamago-go"))


def sdk_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    if member.isdir() and member.name in {".", "./"}:
        return None
    name = sdk_path(member.name)
    link = member.linkname
    if member.islnk():
        link = sdk_path(link)
    elif member.issym() and PurePosixPath(link).is_absolute():
        link = os.path.relpath(sdk_path(link), PurePosixPath(name).parent)
    return tarfile.data_filter(member.replace(name=name, linkname=link), destination)


def install_sdk(directory: Path) -> Path:
    archive = directory / "sdk.tar.gz"
    with urllib.request.urlopen(SDK_URL) as source, archive.open("wb") as output:
        shutil.copyfileobj(source, output)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != SDK_DIGEST:
        raise ValueError(f"SDK SHA-256 {digest}, expected {SDK_DIGEST}")
    sdk = directory / "sdk"
    sdk.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(sdk, filter=sdk_member)
    (sdk / "REPO.bazel").touch()
    (sdk / "BUILD.bazel").write_text(
        'package(default_visibility = ["//visibility:public"])\n'
        'exports_files(["bin/go"])\n'
        'filegroup(name = "files", srcs = glob(["**"]))\n'
    )
    return sdk


def verify_blob(contents: bytes, expected: str) -> None:
    digest = hashlib.sha1(f"blob {len(contents)}\0".encode() + contents).hexdigest()
    if digest != expected:
        raise ValueError(f"dispatcher Git blob {digest}, expected {expected}")


def stage_dispatcher() -> None:
    verify_blob(DISPATCHER.read_bytes(), "d138e3b8da9f750f6a2d9d640633f004eecac32e")
    url = f"https://raw.githubusercontent.com/tamird/gvisor/{FIXED_REV}/{DISPATCHER}"
    with urllib.request.urlopen(url) as source:
        fixed = source.read()
    verify_blob(fixed, "fbfab4a8a64e066e71ba7eea1634d44bac1aaac7")
    (GUEST / "dispatcher-fixed.txt").write_bytes(fixed)


def stage_proxy(directory: Path, sdk: Path) -> None:
    cache = directory / "gomodcache"
    go = sdk / "bin/go"
    env = dict(
        os.environ, GOROOT=str(sdk), GOOS="tamago", GOARCH="amd64",
        GOOSPKG="github.com/usbarmory/tamago", GOEXPERIMENT="simd", CGO_ENABLED="0", GOENV="off",
        GOWORK="off", GOTOOLCHAIN="local", GOPROXY="https://proxy.golang.org",
        GOMODCACHE=str(cache), GOCACHE=str(directory / "go-cache"),
    )
    # A local replacement lives in the checkout. A versioned replacement must
    # be stored under the replacement's name and version in the file proxy.
    template = (
        "{{with .Module}}{{if .Replace}}{{with .Replace}}"
        "{{if .Version}}{{.Path}}@{{.Version}}{{end}}{{end}}"
        "{{else if .Version}}{{.Path}}@{{.Version}}{{end}}{{end}}"
    )
    listing = subprocess.check_output(
        [go, "list", "-mod=mod", "-deps", "-tags=microvm", "-f", template, "."], cwd=GUEST, env=env, text=True
    )
    modules = sorted(set(listing.split()))
    if not modules:
        raise ValueError("the guest dependency list contains no external Go modules")
    downloads = subprocess.check_output(
        [go, "mod", "download", "-json", *modules], cwd=GUEST, env=env, text=True
    )
    proxy = GUEST.parent / "proxy"
    root = cache / "cache/download"
    # The resolver may read a go.mod even though the guest imports no packages
    # from that module. Only copy code archives for modules it actually used.
    files = set(root.glob("**/@v/*.mod"))
    decoder = json.JSONDecoder()
    while downloads := downloads.lstrip():
        module, end = decoder.raw_decode(downloads)
        downloads = downloads[end:]
        files.update(Path(module[field]) for field in ("Zip", "Info", "GoMod"))
    for source in sorted(files):
        output = proxy / source.relative_to(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
    print(f"Staged {len(files)} proxy files for {len(modules)} imported modules: {', '.join(modules)}")


if __name__ == "__main__":
    directory = Path(os.environ["RUNNER_TEMP"]) / "tamago-dispatch"
    directory.mkdir(exist_ok=True)
    stage_dispatcher()
    stage_proxy(directory, install_sdk(directory))
