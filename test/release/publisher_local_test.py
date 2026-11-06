"""Run the unmodified Debian publisher inside its declared tool distribution."""
import copy
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unsigned(source, dest):
    with source.open("rb") as r, dest.open("wb") as w:
        if r.read(8) != b"!<arch>\n":
            raise ValueError("missing ar signature")
        w.write(b"!<arch>\n")
        names = []
        while hdr := r.read(60):
            if len(hdr) != 60 or hdr[-2:] != b"`\n":
                raise ValueError("invalid ar header")
            n = int(hdr[48:58])
            name = hdr[:16].rstrip(b" /")
            names.append(name)
            length = n + n % 2
            if name == b"_gpgbuilder":
                r.seek(length, 1)
                continue
            w.write(hdr)
            while length:
                data = r.read(min(length, 1 << 20))
                if not data:
                    raise ValueError("short ar member")
                w.write(data)
                length -= len(data)
    if names != [b"debian-binary", b"control.tar.gz", b"data.tar.gz", b"_gpgbuilder"]:
        raise ValueError(names)


def tool_member(member, destination):
    # A distribution may have absolute symlinks. Represent their destinations
    # relative to each symlink so extraction cannot follow them outside root.
    if member.issym() and member.linkname.startswith("/"):
        member = copy.copy(member)
        target = PurePosixPath(member.linkname).relative_to("/")
        member.linkname = os.path.relpath(target, PurePosixPath(member.name).parent)
    return tarfile.data_filter(member, destination)


def main():
    bwrap_package, flat, publisher, keygen, *packages = [Path(p).resolve() for p in sys.argv[1:]]
    out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    result = {"verified": [], "packages": {}, "suites": {}}

    def phase(info):
        result["verified"].append(info)
        print(info, flush=True)
        (out / "publisher.json").write_text(json.dumps(result, indent=2) + "\n")

    if not os.getuid():
        raise ValueError("a root host process cannot qualify a rootless publisher")
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as d:
        d = Path(d)
        launcher = d / "launcher"
        subprocess.run(["/usr/bin/dpkg-deb", "-x", bwrap_package, launcher], check=True)
        bwrap = launcher / "usr/bin/bwrap"
        bwrap.chmod(0o755)
        root = d / "rootfs"
        root.mkdir()
        with tarfile.open(flat) as t:
            t.extractall(root, filter=tool_member)
        for dst, target in [("bin/sh", "bash"), ("usr/bin/awk", "mawk")]:
            if not (root / dst).exists():
                (root / dst).symlink_to(target)
        for name in ["input", "work", "tmp", "proc", "dev"]:
            (root / name).mkdir(exist_ok=True)
        inputs = d / "inputs"
        work = d / "work"
        inputs.mkdir()
        work.mkdir()
        shutil.copyfile(publisher, inputs / "make_apt.sh")
        shutil.copyfile(keygen, inputs / "make_test_key.sh")
        for arch, package in zip(("amd64", "arm64"), packages, strict=True):
            f = inputs / arch / "runsc.deb"
            f.parent.mkdir()
            unsigned(package, f)
        phase("both public runsc packages stripped only of their existing signature; tools extracted")

        def namespace(args, mounts):
            cmd = [str(bwrap), "--unshare-all", "--die-with-parent", "--new-session",
                   "--uid", "0", "--gid", "0", "--ro-bind", str(root), "/",
                   "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc"]
            for mode, src, dst in mounts:
                cmd.extend([mode, str(src), dst])
            cmd.extend(["--chdir", "/work", "--clearenv", "--setenv", "LC_ALL", "C",
                        "--setenv", "PATH", "/usr/sbin:/usr/bin:/sbin:/bin", "--setenv", "HOME", "/work",
                        "--", *args])
            answer = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if answer.returncode:
                raise RuntimeError(f"publisher exited {answer.returncode}: {answer.stdout[-2500:]}")
            return answer.stdout

        mounts = [("--ro-bind", inputs, "/input"), ("--bind", work, "/work")]
        namespace(["/bin/bash", "/input/make_test_key.sh", "/work/disposable.key"], mounts)
        phase("existing fixture generated a disposable key inside the offline publisher")
        public = namespace(["/bin/bash", "-eu", "-c", "mkdir -m 700 /tmp/gpg; "
                            "gpg --homedir /tmp/gpg --batch --import /work/disposable.key >/dev/null 2>&1; "
                            "gpg --homedir /tmp/gpg --batch --armor --export"], mounts)
        if not public.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            raise ValueError("missing public certificate")
        verify = d / "verify"
        verify.mkdir()
        (verify / "public.asc").write_text(public)
        for suite in ("20261102", "release"):
            namespace(["/bin/bash", "/input/make_apt.sh", "/work/disposable.key", suite, "/work/repo",
                       "/input/amd64/runsc.deb", "/input/arm64/runsc.deb"], mounts)
            phase(f"unmodified publisher signed and indexed {suite}")
        repo = work / "repo"
        verify_mounts = [("--ro-bind", verify, "/input"), ("--ro-bind", repo, "/work")]
        for suite in ("20261102", "release"):
            dist = repo / "dists" / suite
            signed_index = {}
            lines = (dist / "Release").read_text().splitlines()
            if "Architectures: amd64 arm64" not in lines or f"Suite: {suite}" not in lines:
                raise ValueError(f"{suite}: invalid labels")
            for line in lines[lines.index("SHA256:") + 1:]:
                if not line.startswith(" "):
                    break
                hashval, size, filename = line.split()
                f = dist / filename
                if (digest(f), f.stat().st_size) != (hashval, int(size)):
                    raise ValueError(f"{suite}: stale {filename}")
                signed_index[filename] = hashval
            if len(signed_index) != 6:
                raise ValueError(f"{suite}: expected six signed indexes")
            check = namespace(["/bin/bash", "-eu", "-c",
                'mkdir -m 700 /tmp/gpg; export GNUPGHOME=/tmp/gpg; '
                'gpg --batch --import /input/public.asc; cd "/work/dists/$1"; '
                'gpg --batch --verify Release.gpg Release; '
                'gpg --batch --output /tmp/signed --decrypt InRelease; cmp Release /tmp/signed; '
                'for p in /work/pool/*/binary-*/runsc.deb; do dpkg-sig --verify "$p"; done',
                "verify", suite], verify_mounts)
            good = [line for line in check.splitlines() if line.startswith("GOODSIG _gpgbuilder ")]
            if len(good) != 2:
                raise ValueError(f"{suite}: missing package signatures")
            for arch in ("amd64", "arm64"):
                ind = dist / "main" / f"binary-{arch}" / "Packages"
                text = ind.read_bytes()
                if gzip.decompress(ind.with_suffix(".gz").read_bytes()) != text or lzma.decompress(ind.with_suffix(".xz").read_bytes()) != text:
                    raise ValueError(f"{suite}: {arch} index variants differ")
                fields = dict(line.split(": ", 1) for line in text.decode().splitlines() if ": " in line)
                fname = f"pool/20261102.0/binary-{arch}/runsc.deb"
                p = repo / fname
                if (fields["Filename"], fields["Architecture"], fields["Version"], fields["SHA256"], int(fields["Size"])) != (
                    fname, arch, "20261102.0", digest(p), p.stat().st_size):
                    raise ValueError(f"{arch}: index did not describe signed package")
                result["packages"][arch] = {"sha256": fields["SHA256"], "size": p.stat().st_size}
            result["suites"][suite] = {"indexes": signed_index, "independent_keyring": good}
            phase(f"{suite}: both suite signatures, package signatures, six indexes and pool hashes verified")
        if result["suites"]["20261102"]["indexes"] != result["suites"]["release"]["indexes"]:
            raise ValueError("dated and latest suites do not stage identical package indexes")


if __name__ == "__main__":
    main()
