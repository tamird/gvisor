"""Check dpkg-sig interoperability before choosing a release publisher."""

import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(*args, env=None):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, env=env).stdout


def main():
    publisher, signer, oracle, amd64, arm64 = map(lambda arg: Path(arg).resolve(), sys.argv[1:])
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "receipt.json"
    receipt = {"phases": [], "packages": {}, "suites": {}}

    def phase(name):
        receipt["phases"].append(name)
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(name, flush=True)

    # gpg-agent's UNIX socket is subject to sockaddr_un's path length limit.
    with tempfile.TemporaryDirectory(prefix="apt-signer-", dir="/tmp") as scratch:
        root = Path(scratch)
        home = root / "keyring"
        home.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(home))
        pub = root / "public.asc"
        secret = root / "throwaway.asc"
        identity = "Disposable APT proof <apt@example.invalid>"
        run("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
            "--quick-generate-key", identity, "rsa2048", "sign", "0", env=env)
        fp = next(line.split(":")[9] for line in run(
            "gpg", "--with-colons", "--list-secret-keys", env=env
        ).decode().splitlines() if line.startswith("fpr:"))
        pub.write_bytes(run("gpg", "--armor", "--export", fp, env=env))
        secret.write_bytes(run("gpg", "--batch", "--armor", "--export-secret-keys", fp, env=env))
        config = root / "relic.json"
        config.write_text(json.dumps({"tokens": {"file": {"type": "file"}},
                                      "keys": {"release": {"token": "file", "keyfile": str(secret),
                                                          "pgpcertificate": str(pub)}}}))
        absent = shutil.which("dpkg-sig") is None
        if not absent:
            raise ValueError("the Ubuntu 24.04 comparison must start without dpkg-sig")
        oracle_root = root / "oracle"
        run("dpkg-deb", "-x", oracle, oracle_root)
        old_signer = oracle_root / "usr/bin/dpkg-sig"
        receipt["host_has_dpkg_sig"] = not absent
        receipt["fingerprint"] = fp
        phase("disposable verification key generated; original verifier unpacked separately")
        signed_paths = []
        for arch, baseline in (("amd64", amd64), ("arm64", arm64)):
            src = root / arch / "input.deb"
            src.parent.mkdir()
            shutil.copyfile(baseline, src)
            # Reproduce the actual compiler output format by removing only the
            # public package's signature. Never load or replace the public key.
            run("ar", "d", src, "_gpgbuilder")
            names = run("ar", "t", src).decode().splitlines()
            if names != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
                raise ValueError(f"unexpected unsigned members: {names}")
            signed = src.with_name("runsc.deb")
            run(signer, "--config", config, "sign", "--key", "release",
                "--file", src, "--output", signed, "--role", "builder", env=env)
            actual = run(old_signer, "--verify", signed, env=env).decode()
            if f"GOODSIG _gpgbuilder {fp}" not in actual:
                raise ValueError(f"original verifier did not accept {arch}: {actual}")
            signed_paths.append(signed)
            receipt["packages"][arch] = {"sha256": sha(signed), "bytes": signed.stat().st_size,
                                         "original_verifier": next(x for x in actual.splitlines() if "GOODSIG" in x)}
            phase(f"{arch} native signature accepted by Ubuntu dpkg-sig")
        repo = root / "repo"
        suite_names = ("20261102", "release")
        script_env = dict(env, PATH=f"{old_signer.parent}:{env['PATH']}")
        for suite in suite_names:
            # The unmodified publisher verifies pre-signed inputs before deciding
            # whether to sign them. Equal hashes below prove it kept those bytes.
            run(publisher, secret, suite, repo, *signed_paths, env=script_env)
            dist = repo / "dists" / suite
            release = (dist / "Release").read_bytes()
            run("gpg", "--batch", "--verify", dist / "Release.gpg", dist / "Release", env=env)
            if run("gpg", "--batch", "--decrypt", dist / "InRelease", env=env) != release:
                raise ValueError(f"{suite}: clearsign content differs")
            lines = release.decode().splitlines()
            if f"Suite: {suite}" not in lines or "Architectures: amd64 arm64" not in lines:
                raise ValueError(f"{suite}: unexpected suite or architectures")
            start = lines.index("SHA256:") + 1
            digests = {}
            for line in lines[start:]:
                if not line.startswith(" "):
                    break
                digest, size, filename = line.split()
                path = dist / filename
                if sha(path) != digest or path.stat().st_size != int(size):
                    raise ValueError(f"{suite}: stale signature reference to {filename}")
                digests[filename] = digest
            if len(digests) != 6:
                raise ValueError(f"{suite}: expected six indexes, got {list(digests)}")
            for arch, signed in zip(("amd64", "arm64"), signed_paths, strict=True):
                index = dist / "main" / f"binary-{arch}" / "Packages"
                text = index.read_bytes()
                if (gzip.decompress(index.with_suffix(".gz").read_bytes()) != text
                    or lzma.decompress(index.with_suffix(".xz").read_bytes()) != text):
                    raise ValueError(f"{suite} {arch}: index readers disagree")
                fields = dict(line.split(": ", 1) for line in text.decode().splitlines() if ": " in line)
                filename = f"pool/20261102.0/binary-{arch}/runsc.deb"
                if (fields["Filename"], fields["Architecture"], fields["Version"], fields["SHA256"],
                    int(fields["Size"]), sha(repo / filename)) != (
                        filename, arch, "20261102.0", sha(signed), signed.stat().st_size, sha(signed)):
                    raise ValueError(f"{suite} {arch}: package index no longer describes native-signed bytes")
            receipt["suites"][suite] = {"signed_indexes": digests, "inrelease_and_detached_verified": True}
            phase(f"{suite}: both signed suite formats and all six indexes verified")
        if receipt["suites"][suite_names[0]] != receipt["suites"][suite_names[1]]:
            raise ValueError("the dated and latest package indexes differ")
        phase("both architectures retained the same signed bytes through both suites")
        run("gpgconf", "--kill", "all", env=env)


if __name__ == "__main__":
    main()
