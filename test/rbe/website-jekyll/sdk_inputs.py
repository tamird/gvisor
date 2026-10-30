"""Materialize the published Jekyll tool files for one fork-only scratch proof."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import urllib.request

pkg = Path("test/rbe/website-jekyll")
sdk = pkg / "tool_files"
sdk.mkdir()
temp = Path(os.environ["RUNNER_TEMP"])
digest = "sha256:fa979718aac3528a2c127eedf1e4f721ac07aed5c22bd5ea493e6ab7b8eca9a1"
for registry in ("mirror.gcr.io", "docker.io"):
    reference = f"{registry}/jekyll/jekyll@{digest}"
    if subprocess.run(["docker", "pull", reference]).returncode == 0:
        break
else:
    raise RuntimeError("could not fetch the official Jekyll distribution")
args = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "ruby", reference, "-rjson", "-rrbconfig", "-e", """node=ENV.fetch('PATH').split(':').map { |d| File.join(d,'node') }.find { |f| File.executable?(f) }
puts JSON.generate({ruby: RbConfig.ruby, node: node, gem_paths: Gem.path, load_paths: $LOAD_PATH})"""]
meta = json.loads(subprocess.check_output(args))
container = subprocess.check_output(["docker", "create", reference], text=True).strip()
archive = temp / "official-jekyll.tar"
try:
    subprocess.run(["docker", "export", "-o", archive, container], check=True)
finally:
    subprocess.run(["docker", "rm", container], check=True)
with tarfile.open(archive) as tar:
    members = {m.name.removeprefix("./"): m for m in tar.getmembers()}
    def resolve(name):
        for _ in range(14):
            m = members.get(name)
            if m is None:
                return None
            if m.isfile():
                return m
            if m.islnk():
                name = m.linkname.lstrip("/").removeprefix("./")
            elif m.issym():
                name = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname)).lstrip("/")
            else:
                return None
        raise ValueError(name)
    lib = set()
    count = 0
    for name, member in members.items():
        subpath = PurePosixPath(name)
        if subpath.is_absolute() or ".." in subpath.parts:
            raise ValueError(name)
        # The action gets Ruby, Node, standard and gem libraries. There is no
        # /bin or shell. Exclude gem install caches and documentation.
        is_library = re.search(r"\.so(?:\.[^/]+)?$", name) and name.startswith(("lib/", "usr/lib/", "usr/local/lib/"))
        is_ruby = any(name.startswith(root.lstrip("/") + "/") for root in meta["load_paths"] + meta["gem_paths"])
        if name not in (meta["ruby"].lstrip("/"), meta["node"].lstrip("/")) and not is_library and not is_ruby:
            continue
        if any(name.startswith(root.lstrip("/") + suffix) for root in meta["gem_paths"] for suffix in ("/cache/", "/doc/")) or ".git" in subpath.parts:
            continue
        target = resolve(name)
        if target is None:
            continue
        out = sdk / name
        out.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(target) as source, out.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        out.chmod(target.mode & 0o777)
        if is_library:
            lib.add(str(subpath.parent))
        count += 1
elf = "lib/ld-musl-x86_64.so.1"
assert all((sdk / v.lstrip("/")).is_file() for v in (meta["ruby"], meta["node"], elf)), meta
# Directory order for this Alpine release's loader is recorded by its package.
libs = [p for p in ("lib", "usr/local/lib", "usr/lib") if p in lib]
meta.update(loader=elf, library_path=libs, files=count)
(pkg / "runtime_vars.bzl").write_text("RUNTIME = " + repr(meta) + "\n")
print("Official Ruby/Node closure:", meta, flush=True)
plugins = json.loads((pkg / "gems.json").read_text())
gem_dir = pkg / "gems"
gem_dir.mkdir()
receipts = {}
for name, detail in plugins.items():
    version = detail["version"]
    blob = urllib.request.urlopen(detail["gem_uri"], timeout=55).read()
    sha=hashlib.sha256(blob).hexdigest()
    assert sha == detail["sha256"], (name, sha, detail["sha256"])
    (gem_dir / f"{name}-{version}.gem").write_bytes(blob)
    receipts[name] = {"version":version, "sha256":sha}
(temp / "website-probe" / "sdk.json").write_text(json.dumps({"runtime":meta,"gems":receipts}, indent=2)+"\n")
