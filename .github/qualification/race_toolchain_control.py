"""Compare static race links with and without an unrelated QEMU registration."""
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = "3dab3494e2a2154244e8b2fc77699c02c2277a56"
QEMU = "@qemu_user_toolchains//:qemu_linux_aarch64_on_linux_x86_64"
RACE = ["//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race"]
WORK = Path(os.environ["RUNNER_TEMP"]) / "race-toolchain-control"
WORK.mkdir(exist_ok=True)
FLAGS = ["--config=x86_64", "--config=race", "-c", "opt", "--jobs=4",
         "--remote_executor=", "--remote_cache=", "--disk_cache=", "--color=no", "--curses=no"]
report = {"source": SOURCE, "registered_again": QEMU, "arms": {}}


def save():
    (WORK / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def bazel(name, command, extra, args):
    path = WORK / (name + ".log")
    with path.open("w") as output:
        p = subprocess.run(["bazelisk", "--nosystem_rc", "--nohome_rc", command,
                            *FLAGS, *extra, *args], stdout=output, stderr=subprocess.STDOUT)
    return p.returncode, path.read_text(errors="replace")


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert os.uname().machine == "x86_64"
    formatter = WORK / "compiler.cquery"
    formatter.write_text("""def format(target):
    for p in providers(target).values():
        c = getattr(p, "cc", p)
        if hasattr(c, "toolchain_id") and hasattr(c, "compiler"):
            return "TOOLCHAIN %s %s %s" % (c.toolchain_id, c.compiler, c.libc)
    return "PROVIDERS %s" % sorted(providers(target).keys())
""")
    for tag, extra in [("default", []), ("extra_qemu", ["--extra_toolchains=" + QEMU])]:
        row = report["arms"][tag] = {}
        save()
        code, log = bazel(tag + "_cc", "cquery", extra, ["--output=starlark",
            "--starlark:file=" + str(formatter), "@bazel_tools//tools/cpp:current_cc_toolchain"])
        row["compiler_query"] = code
        row["compiler"] = [l[:480] for l in log.splitlines() if l.startswith(("TOOLCHAIN ", "PROVIDERS "))]
        row["query_errors"] = [l[:400] for l in log.splitlines() if l.startswith("ERROR:")][:4]
        save()
        assert code == 0 and len(row["compiler"]) == 1
        code, log = bazel(tag + "_race", "cquery", extra, ["--output=starlark",
            '--starlark:expr="%s %s" % (target.label, build_options(target).get("//command_line_option:platforms", []))',
            'filter("(_impl|_musl)$", deps(set(%s), 2))' % " ".join(RACE)])
        row["producer_query"] = code
        row["producers"] = [l[:480] for l in log.splitlines() if any(label + suffix + " " in l for label in RACE for suffix in ("_musl", "_impl"))]
        save()
        assert code == 0 and row["producers"]
    code, log = bazel("extra_qemu_link", "build", ["--extra_toolchains=" + QEMU], ["--keep_going", *RACE])
    row = report["arms"]["extra_qemu"]
    row["build_exit"] = code
    row["link_errors"] = sorted(set(l[:430] for l in log.splitlines() if re.search(
        r"ld\.lld: error:|unable to find library|cannot find -l|undefined symbol:|cannot open (?:crt|.*libc)", l, re.I)))[:15]
    row["diagnostics"] = [l[:430] for l in log.splitlines() if l.startswith("ERROR:")][:5]
    save()
    assert all(a["compiler"][0].startswith("TOOLCHAIN ") for a in report["arms"].values())
    assert code == 0 or row["link_errors"], "Build failed before the link; inspect the receipt."


if __name__ == "__main__":
    main()
