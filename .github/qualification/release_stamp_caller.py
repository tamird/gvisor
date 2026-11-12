"""Run the checked-in Buildkite selection and compare the materialized packages."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BASE_TAG = "release-20261101.0"
TAG = "release-20261112.2"
DIST = "2026.11.12.2"
OUT = Path(os.environ["RUNNER_TEMP"]) / "release-identity" / "public"
OPTIONS = ["--lockfile_mode=off", "--jobs=4", "--noshow_progress", "-c", "opt"]
STAMP = ["--config=release_version", "--embed_label=" + TAG]
FILES = {"runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd"}
ARCHES = {"x86_64": ("amd64", 62), "aarch64": ("arm64", 183)}

def run(*args, **kwargs):
    result = subprocess.run(args, check=True, **kwargs)
    return result.stdout

def output(*args):
    return run(*args, text=True, stdout=subprocess.PIPE).strip()

def save(name, data):
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        value = hashlib.new(algorithm)
        while part := stream.read(1024 * 1024):
            value.update(part)
    return value.hexdigest()

def commands():
    # Decode only the two literal production build commands. The tag, signing,
    # cloud copy, and promotion commands elsewhere in the step are never run.
    script = Path(".buildkite/release.yaml").read_text().split('label: ":ship: Release"', 1)[1]
    script = script.split('      - ./.buildkite/scripts/release/tag.sh stage\n', 1)[1]
    script = script.split('      # Sign the labeled archives', 1)[0]
    blocks = script.split('      - |\n')
    assert not blocks[0] and len(blocks) == 3
    return "\n".join(line.removeprefix("        ") for b in blocks for line in b.splitlines()).replace("$$", "$")

def selection():
    run("git", "tag", BASE_TAG, "HEAD^")
    source = commands()
    previous = output("git", "describe", "--always", "--tags", "--exclude", "release-*-staging", "--abbrev=12", "--dirty")
    assert previous.startswith(BASE_TAG + "-1-g")
    records = []
    for name, staging, nightly, expected, wants_dist in [
        ("master", "", "false", previous, False),
        ("nightly", "", "true", previous, False),
        ("staging", TAG + "-staging", "false", TAG, True),
        ("tagged-nightly", "", "true", TAG, False),
    ]:
        if name == "staging":
            # tag.sh stage installs this verified ref on its clean working tree.
            run("git", "tag", TAG)
        env = dict(os.environ, BUILDKITE_TAG=staging, RELEASE_NIGHTLY=nightly)
        observed = run("bash", "-eu", "-c", 'make() { printf "%s\\n" "$@"; };\n' + source,
                       env=env, text=True, stdout=subprocess.PIPE).splitlines()
        assert observed == ["release-artifacts", "RELEASE_VERSION=" + expected,
                            "BAZEL_OPTIONS=--compilation_mode=opt"] + (["artifacts-python"] if wants_dist else []), observed
        records.append(dict(build=name, native=expected, python=wants_dist, argv=observed))
    # Check the actual signing recipe with prebuilt artifacts. The dummy key
    # is a prerequisite for the dry run; generated keys and uploads are unused.
    key = OUT.parent / "not-a-key"
    key.touch()
    sign = output("make", "-n", "DOCKER_BUILD=false", "RELEASE_ARTIFACTS=artifacts",
                  "RELEASE_KEY=" + str(key), "release")
    assert "tools/make_release.sh" in sign and " bazel " not in sign and "artifacts-all" not in sign, sign
    save("production-selection", dict(cases=records, signing_recipe=sign.splitlines()))
    assert 'release-inputs' in Path(".buildkite/pipeline.yaml").read_text()
    # Use host mode of the same Makefile on the disposable runner. Its Bazel
    # and both GNU cross tools are provided in the previous step.
    env = dict(os.environ, BUILDKITE_TAG=TAG + "-staging", RELEASE_NIGHTLY="false",
               RELEASE_NAME=TAG, MAKEFLAGS="DOCKER_BUILD=false",
               BAZEL_REMOTE_CACHE="--jobs=4 --lockfile_mode=off --noshow_progress")
    run("bash", "-eu", "-c", source, env=env)

def stage():
    run("bazel", "build", *OPTIONS, *STAMP, "--remote_download_outputs=toplevel",
        "--//sandboxexec/sandbox/python:dist_version=" + DIST, "//debian:release-inputs")
    root = Path("bazel-bin/debian/release-inputs").resolve(strict=True)
    assert root.is_dir()
    def deb_members(path):
        return {opt: hashlib.sha256(run("dpkg-deb", opt, str(path), stdout=subprocess.PIPE)).hexdigest()
                for opt in ("--fsys-tarfile", "--ctrl-tarfile")}

    rows = {}
    result = dict(staged=rows, python=[], mismatches=[])
    expected = {f"{arch}/{f}" for arch in ARCHES for f in FILES}
    assert set((root / "inputs").read_bytes().decode().rstrip("\0").split("\0")) == expected
    for relative in sorted(expected):
        path = root / relative
        staged = digest(path)
        production = Path("artifacts", relative)
        production_sha = digest(production)
        rows[relative] = dict(sha256=staged, size=path.stat().st_size, production_sha256=production_sha)
        if production_sha != staged:
            result["mismatches"].append("production/" + relative)
            if path.suffix == ".deb":
                rows[relative]["production_members"] = deb_members(production)
                rows[relative]["staged_members"] = deb_members(path)
        save("package-comparison",result)
        if path.suffix != ".deb":
            checkfile = path.with_name(path.name + ".sha512").read_text().strip()
            assert checkfile == digest(path, "sha512") + "  " + path.name, (relative,checkfile)
        else:
            deb_arch, machine = ARCHES[path.parent.name]
            assert output("dpkg-deb", "-f", str(path), "Architecture") == deb_arch
            assert output("dpkg-deb", "-f", str(path), "Version") == TAG.removeprefix("release-")
            rows[relative]["debian"] = [deb_arch, TAG.removeprefix("release-")]
            extract = OUT.parent / ("installed-" + deb_arch)
            run("dpkg-deb", "-x", str(path), str(extract))
            cli = extract / "usr/bin/runsc"
            with cli.open("rb") as reader:
                header = reader.read(20)
            assert header[:4] == b"\x7fELF" and int.from_bytes(header[18:20], "little") == machine
            if deb_arch == "amd64":
                version = output(str(cli), "--version")
                assert TAG in version, version
                rows[relative]["runsc"] = version
    for pkg in sorted((root / "python").iterdir()):
        if pkg.name.endswith((".whl", ".tar.gz")):
            assert (Path("artifacts/python") / pkg.name).exists() and DIST in pkg.name
            checkfile = pkg.with_name(pkg.name + ".sha512").read_text().strip()
            assert checkfile == digest(pkg,"sha512") + "  " + pkg.name
            result["python"].append(pkg.name)
    assert len(result["python"]) == 2, result["python"]
    save("package-comparison",result)
    for arch in ARCHES:
        opts = [*OPTIONS, *STAMP, "--config=" + arch]
        run("bazel", "build", *opts, "//debian:release-packages")
        paths = output("bazel", "cquery", *opts, "--output=files", "//debian:release-packages").splitlines()
        assert {Path(x).name for x in paths} == FILES, paths
        for name in paths:
            direct = Path(name)
            key = arch + "/" + direct.name
            row = rows[key]
            row["direct_sha256"] = digest(direct)
            row["identical"] = row["sha256"] == row["direct_sha256"]
            if not row["identical"]:
                result["mismatches"].append(key)
                if direct.suffix == ".deb":
                    row["direct_members"],row["staged_members"] = deb_members(direct),deb_members(root / key)
        save("package-comparison", result)
    assert not result["mismatches"], result["mismatches"]

OUT.mkdir(parents=True, exist_ok=True)
{"selection": selection, "stage": stage}[sys.argv[1]]()
