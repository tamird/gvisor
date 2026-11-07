"""Separate package loading, target configuration, and external C toolchain resolution."""

import json
import platform
import sys

import downstream_llvm as qa


PLATFORM = "@gvisor//tools/bazeldefs:linux_arm64"
BASE = "69e89d7a3dfc6b2b484f7dc01a68fad68741dd03"
FILES = ("vdso/BUILD", "tools/bazeldefs/BUILD")
LOADS = {
    "plain": None,
    "facade": ("@gvisor//tools:defs.bzl", "cc_binary"),
    "cc": ("@gvisor//tools/bazeldefs:cc.bzl", "cc_binary"),
    "defs": ("@gvisor//tools/bazeldefs:defs.bzl", "select_arch"),
    "arch": ("@gvisor//tools:arch.bzl", "select_arch"),
    "go": ("@gvisor//tools/bazeldefs:go.bzl", "gazelle"),
}


def work():
    module = ('module(name = "downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\n'
              f'local_path_override(module_name = "gvisor", path = {json.dumps(str(qa.SRC))})\n')
    qa.CONSUMER.mkdir()
    (qa.CONSUMER / "MODULE.bazel").write_text(module)
    (qa.CONSUMER / ".bazelversion").write_text("9.3.1\n")
    (qa.CONSUMER / "BUILD.bazel").write_text("")
    qa.REPORT["consumerModule"] = module.replace(str(qa.SRC), "<source>")
    for name, loaded in LOADS.items():
        pkg = qa.CONSUMER / name
        pkg.mkdir()
        src = ('load("%s", "%s")\n' % loaded) if loaded else ""
        src += 'filegroup(name = "marker")\n'
        if name == "cc":
            src += 'cc_binary(name = "probe", srcs = ["probe.cc"])\n'
            (pkg / "probe.cc").write_text("int main() { return 0; }\n")
        (pkg / "BUILD.bazel").write_text(src)


def probe(case, name, op, *args):
    row, out = qa.bazel(case, name, op, *args, "--output=label", timeout=300)
    row["labels"] = [line for line in out.decode(errors="replace").splitlines()
                     if line.startswith(("//", "@")) and len(line) < 260][:8]
    return row["exit"] == 0


def controls(case):
    for name in LOADS:
        probe(case, "load_" + name, "query", "//" + name + ":marker")
    for name, label in {"platform": PLATFORM, "root_visibility": "@gvisor//:sandbox",
                        "public_vdso": qa.TARGET}.items():
        probe(case, "load_" + name, "query", label)
    probe(case, "host_plain", "cquery", "//plain:marker")
    probe(case, "host_cc", "cquery", "//cc:probe")
    probe(case, "arm_plain", "cquery", qa.ARCH, "//plain:marker")
    probe(case, "arm_cc", "cquery", qa.ARCH, "//cc:probe")


def main():
    fixed = qa.git("rev-parse", "HEAD^")
    assert platform.machine() == "x86_64"
    assert qa.git("rev-parse", fixed + "^") == BASE
    assert set(qa.git("diff", "--name-only", BASE, fixed).splitlines()) == set(FILES)
    assert qa.git("diff", BASE, fixed, "--", "MODULE.bazel") == ""
    work()
    qa.git("switch", "--quiet", "--detach", BASE)
    for arm, overlay in (("original", ()), ("vdso_only", FILES[:1]),
                         ("platform_only", FILES[1:]), ("both", FILES)):
        if arm == "both":
            qa.git("restore", "--source=" + BASE, "--worktree", "--", *FILES)
            qa.git("switch", "--quiet", "--detach", fixed)
        else:
            qa.git("restore", "--source=" + BASE, "--worktree", "--", *FILES)
            if overlay:
                qa.git("restore", "--source=" + fixed, "--worktree", "--", *overlay)
        blobs = {name: qa.git("hash-object", name) for name in FILES}
        assert all(blob == qa.git("rev-parse", (fixed if name in overlay else BASE) + ":" + name)
                   for name, blob in blobs.items())
        case = qa.REPORT["cases"][arm] = {"source": qa.git("rev-parse", "HEAD"), "blobs": blobs}
        qa.save()
        if arm in ("original", "both"):
            controls(case)
        else:
            probe(case, "load_public_vdso", "query", qa.TARGET)
            probe(case, "load_platform", "query", PLATFORM)
        qa.plan(case)
        if arm == "both" and case["configured"]["exit"] == 0:
            qa.build(case)
        qa.save()
    case = qa.REPORT["cases"]["both"]
    qa.REPORT["collected"] = all(len(c.get("configured", {})) for c in qa.REPORT["cases"].values())
    qa.REPORT["passed"] = (qa.REPORT["collected"] and case.get("linkedARM64", False)
                           and case["configured"].get("counts") == {"CppCompile": 2, "CppLink": 1}
                           and all(a["toolFromLlvm"] for a in case["configured"]["actions"]
                                   if a["mnemonic"] == "CppCompile"))
    qa.save()
    return int(not qa.REPORT["passed"])


if __name__ == "__main__":
    sys.exit(main())
