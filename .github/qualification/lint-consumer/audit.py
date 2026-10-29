"""Measure lint repository visibility from a separate Bazel module."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(os.environ["GITHUB_WORKSPACE"])
AREA = Path(os.environ["RUNNER_TEMP"]) / "dev-dependency-audit"
CONSUMER = AREA / "consumer"
RAW = AREA / "raw"
OUT = AREA / "public"
MASTER = "0fa6803f2a5991dc066a29fc50414b4a3b519590"
CPPLINT = "b4720d1975646e4d202ba3f59b7b835cedcdee3b"
BUILDIFIER = "6946292a2275252009256d882c44900419ff0934"
SPELLING = "84fb13cc46f39b7eed90cb43139fb1b2928fdac8"
TARGET = "@gvisor//pkg/waiter:waiter"
SRC_FILES = ["BUILD", "MODULE.bazel"]
SOURCE_PKG = SOURCE / "tools/lint/spelling_check"
REPORT = {"rows": []}
KEYS = ("lint_deps", "starlark_sources", "cpplint_sources", "spelling_sources", "@cpplint", "buildifier_prebuilt", "kernel_headers", "Only the root module", "no such package", "does not contain a repository", "No repository visible", "no repository visible", "not a valid repository name", "Unable to find package", "No matching toolchains", "is not visible", "unknown tag", "does not have a tag class", "module extension", "NoSuchPackageException", "go_deps", "go_sdk", "no such target", "not declared", "BUILD file not found", "checksum", "403", "404", "out of memory", "Unrecognized option", "repo_mapping", "testng", "coral_crosstool", "invalid label", "not in scope", "not found", "target '//:production'", "target '//:development'")


def sha(b):
    return sha256(b).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=SOURCE).decode().strip()


def select(ref):
    if SOURCE_PKG.is_dir():
        (SOURCE_PKG / "BUILD").unlink()
        SOURCE_PKG.rmdir()
    for f in SRC_FILES:
        git("restore", "--", f)
    git("checkout", "--quiet", "--detach", ref)
    assert git("rev-parse", "HEAD") == ref and not git("status", "--porcelain", "--untracked-files=no")


def edit(path, before, after):
    f = SOURCE / path
    data = f.read_text()
    assert data.count(before) == 1, (path, before)
    f.write_text(data.replace(before, after))


def run(name, cwd, command, args, *, wants=None, witness=None, remote=False):
    row = {"name": name, "source": git("rev-parse", "HEAD"), "where": "gvisor root" if cwd == SOURCE else "external root", "diff": sha(git("diff").encode()), "command": command, "args": args}
    row["effective_files"] = {f:sha((SOURCE/f).read_bytes()) for f in (*SRC_FILES,"tools/lint/spelling_check/BUILD") if (SOURCE/f).is_file()}
    REPORT["rows"].append(row)
    prefix = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc"]
    if remote:
        prefix += ["--bazelrc="+str(AREA / "rbe.bazelrc")]
    cmd = prefix + [command, "--lockfile_mode=off", "--color=no", "--curses=no"] + args
    be = RAW / (name+".bep")
    if command == "build":
        cmd.insert(-1, "--build_event_json_file="+str(be))
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=480)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, err, code = e.stdout or b"", e.stderr or b"", 124
    (RAW / (name+".stderr")).write_bytes(err)
    (RAW / (name+".stdout")).write_bytes(out)
    lines = [x[:540] for x in err.decode(errors="replace").splitlines() if x.startswith("ERROR:") or "ERROR " in x]
    combined = "\n".join(lines) or err.decode(errors="replace")
    row.update(exit=code, stdout_sha256=sha(out), stderr_sha256=sha(err), error_markers=[k for k in KEYS if k in combined], error_count=len(lines))
    row["labels"] = [v for v in out.decode(errors="replace").splitlines() if v.startswith(("@gvisor//", "//pkg/", "//runsc:", "//:license", "//:codespell", "//:pub", "@fixture//", "//:both"))][:15]
    if be.is_file():
        events = [json.loads(x) for x in be.read_bytes().splitlines()]
        row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
        row["bazel_finished"] = [e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e]
    if wants is not None:
        row["matched"] = (code == 0) == wants and (witness is None or witness.encode() in out+err)
    print(name, code, row["error_markers"], flush=True)
    publish()
    return row


def publish():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True)+"\n")


def external(name, *args, **kw):
    return run(name, CONSUMER, "query", list(args), **kw)


def source_set(name):
    return external(name, "set("+TARGET+" @gvisor//runsc:runsc @gvisor//:license)")


def mixed():
    # Same extension identity: core vs dev tags, plus per-call repo rules.
    lib, root = AREA / "fixture", AREA / "mixed-consumer"
    lib.mkdir()
    root.mkdir()
    (lib/"MODULE.bazel").write_text("""module(name="fixture", version="1")
core = use_extension("//:edge.bzl", "edge")
core.input(name="prod_tag")
use_repo(core, "prod_tag")
dev = use_extension("//:edge.bzl", "edge", dev_dependency=True)
dev.input(name="dev_tag")
use_repo(dev, "dev_tag")
r = use_repo_rule("//:edge.bzl", "data")
r(name="prod_repo")
r(name="dev_repo", dev_dependency=True)
register_toolchains("//:dev_tool", dev_dependency=True)
register_toolchains("//:prod_tool")
""")
    (lib/"edge.bzl").write_text('''def _data(r):
    r.file("BUILD", "exports_files(['payload'])")
    r.file("payload", "present")
data = repository_rule(implementation=_data)
def _edge(ctx):
    for m in ctx.modules:
        for tag in m.tags.input:
            data(name=tag.name)
def _provide(ctx):
    return [platform_common.ToolchainInfo(mark=ctx.attr.mark)]
provide = rule(implementation=_provide, attrs={"mark":attr.string()})
def _pick(ctx):
    f = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.write(f, ctx.toolchains[Label("//:kind")].mark)
    return [DefaultInfo(files=depset([f]))]
pick = rule(implementation=_pick, toolchains=[Label("//:kind")])
edge = module_extension(implementation=_edge, tag_classes={"input":tag_class(attrs={"name":attr.string()})})
''')
    (lib/"BUILD").write_text('''load(":edge.bzl", "provide", "pick")
toolchain_type(name="kind")
provide(name="prod", mark="production")
provide(name="dev", mark="development")
toolchain(name="prod_tool", toolchain=":prod", toolchain_type=":kind")
toolchain(name="dev_tool", toolchain=":dev", toolchain_type=":kind")
pick(name="chosen", visibility=["//visibility:public"])
filegroup(name="production", srcs=["@prod_tag//:payload","@prod_repo//:payload"], visibility=["//visibility:public"])
filegroup(name="development", srcs=["@dev_tag//:payload","@dev_repo//:payload"], visibility=["//visibility:public"])
''')
    (root/"MODULE.bazel").write_text(f'module(name="mixed_consumer")\nbazel_dep(name="fixture",version="1")\nlocal_path_override(module_name="fixture",path={str(lib)!r})\n')
    (root/"BUILD").write_text('filegroup(name="both",srcs=["@fixture//:production"])\n')
    run("mixed_root_all", lib, "query", ["deps(//:development)"], wants=True, witness="@dev_tag")
    run("mixed_real_external", root, "build", ["//:both"], wants=True)
    for label, cwd, name, flag, value in (("//:chosen",lib,"mixed_tool_in_root",[],"development"),("@fixture//:chosen",root,"mixed_tool_in_consumer",[],"production"),("//:chosen",lib,"mixed_tool_ignore_dev",["--ignore_dev_dependency"],"production")):
        row = run(name, cwd, "build", flag+[label], wants=True)
        if row["exit"] == 0:
            import urllib.parse
            ev = [json.loads(x) for x in (RAW/(name+".bep")).read_bytes().splitlines()]
            uris = {f["uri"] for e in ev for f in e.get("namedSetOfFiles",{}).get("files",[]) if f.get("name","").split("/")[-1] == "chosen.txt" and f.get("uri","").startswith("file:")}
            if len(uris) == 1:
                path = Path(urllib.parse.unquote(urllib.parse.urlparse(uris.pop()).path))
                row["selection"] = path.read_text()
                row["output_sha256"] = sha(path.read_bytes())
        row["matched"] = row.get("selection") == value
    run("mixed_dev_external", root, "query", ["deps(@fixture//:development)"], wants=False, witness="dev_")
    run("mixed_dev_ignored_root", lib, "query", ["--ignore_dev_dependency", "deps(//:development)"], wants=False)


def main():
    for p in (AREA, CONSUMER, RAW, OUT):
        p.mkdir(exist_ok=True)
    (CONSUMER/"MODULE.bazel").write_text(f'module(name="lint_downstream")\nbazel_dep(name="gvisor",version="0.0.1")\nlocal_path_override(module_name="gvisor",path={str(SOURCE)!r})\n')
    (CONSUMER/"BUILD").write_text(f'alias(name="pub",actual="{TARGET}")\n')
    assert all(git("cat-file","-t",x)=="commit" for x in (MASTER, CPPLINT, BUILDIFIER, SPELLING))
    try:
        mixed()
        select(MASTER)
        source_set("master_external_labels")
        run("master_external_build", CONSUMER, "build", ["--jobs=24", "//:pub"], remote=True)
        for name, ref, module, repo in (("cpplint",CPPLINT,"cpplint","cpplint_sources"),("buildifier",BUILDIFIER,"buildifier_prebuilt","starlark_sources")):
            select(ref)
            source_set(name+"_original_external")
            line=next(x for x in (SOURCE/"MODULE.bazel").read_text().splitlines() if x.startswith(f'bazel_dep(name = "{module}"'))
            edit("MODULE.bazel",line,line[:-1]+", dev_dependency = True)")
            if name == "cpplint":
                edit("MODULE.bazel", 'cpplint_sources(name = "cpplint_sources")', 'cpplint_sources(name = "cpplint_sources", dev_dependency = True)')
            else:
                edit("MODULE.bazel",'    name = "starlark_sources",','    name = "starlark_sources",\n    dev_dependency = True,')
            external(name+"_marked_root_license", "@gvisor//:license")
            external(name+"_marked_component",TARGET)
            external(name+"_marked_checker_deps","deps(@gvisor//:"+("cpplint" if name=="cpplint" else "buildifier")+", 1)")
        select(SPELLING)
        source_set("spelling_original_external")
        edit("MODULE.bazel",'pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")','pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip", dev_dependency = True)')
        edit("MODULE.bazel",'    name = "spelling_sources",','    name = "spelling_sources",\n    dev_dependency = True,')
        run("spelling_marked_own_root", SOURCE, "query", ["//:codespell"], wants=True, witness="//:codespell")
        run("spelling_ignore_dev", SOURCE, "query", ["--ignore_dev_dependency", "//:license"], wants=False, witness="lint_deps")
        external("spelling_marked_external", "@gvisor//:license", wants=False, witness="lint_deps")
        external("spelling_marked_component_only",TARGET)
        # Keep root public labels as aliases; load pip's generated .bzl only
        # when a caller actually selects the check package.
        edit("BUILD",'load("//tools/lint:spelling.bzl", "spelling")\n',"")
        edit("BUILD",'spelling(name = "codespell")','alias(name = "codespell", actual = "//tools/lint/spelling_check:check")')
        SOURCE_PKG.mkdir()
        (SOURCE_PKG/"BUILD").write_text('load("//tools/lint:spelling.bzl", "spelling")\nspelling(name="check",visibility=["//:__pkg__"])\n')
        source_set("spelling_isolated_external")
        run("spelling_isolated_ignore_dev",SOURCE,"query",["--ignore_dev_dependency","//:license"],wants=True,witness="//:license")
        external("spelling_isolated_lint_deps","deps(@gvisor//:codespell, 1)",wants=False,witness="lint_deps")
        run("spelling_isolated_external_build",CONSUMER,"build",["--jobs=24","//:pub"],remote=True)
    finally:
        publish()
    REPORT["matched_predicates"] = all(r.get("matched",True) for r in REPORT["rows"])
    publish()
    return 0 if REPORT["matched_predicates"] else 1


if __name__ == "__main__":
    sys.exit(main())
