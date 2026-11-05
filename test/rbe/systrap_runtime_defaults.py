"""Exercise the freestanding stub with a hosted allocator and medium-model GNU."""
import json
from pathlib import Path
import re
import sys

import native_cross_inputs as q

STOCK = "005743727b3d80d373085133b5da9cd906cc9d7c"
FIX = "7f6ecfff61b465c2ae00d20ee4ebaad09d430519"
ROOTS = [q.SYSMSG + s for s in ("sighandler_binary_arch", "sighandler_go_arch")]
SIDECARS = ["//runsc/prewarmer:prewarmer_binary", "//runsc/fdparking:fdparking_binary", "//runsc/procumounter:procumounter_binary"]
OBJECT = q.SYSMSG + "sighandler_object"
ALLOCATOR = "//test/rbe/allocator:poison"
OVERRIDE = "--custom_malloc=" + ALLOCATOR
FLAGS = ["--config=x86_64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
DISABLE = "--features=-mcmodel_medium"
EXPR = "set(" + " ".join(ROOTS) + ")"
# Inject this named feature only into the Linux worker's GNU toolchain fixture.
# A raw command-line -mcmodel flag would bypass the target's feature settings.
FEATURE = """@@ -346,5 +351,10 @@
     return cc_common.create_cc_toolchain_config_info(
             ctx = ctx,
             features = [
+                feature(
+                    name = "mcmodel_medium",
+                    enabled = ctx.attr.cpu == "k8",
+                    flag_sets = [flag_set(actions = ALL_COMPILE_ACTIONS, flag_groups = [flag_group(flags = ["-mcmodel=medium", "-mlarge-data-threshold=0"])])],
+                ),
                 feature(name = "static_linking_mode"),
                 feature(name = "fully_static_link"),
"""


def checked(row, name, cmd, cap=240, expected_failure=False):
    rc, out, err = q.command(cmd, cap)
    row[name] = {"exit": rc, "stderr": q.diagnostics(err)}
    if expected_failure:
        assert rc != 0 and b"requires_a_hosted_runtime" in err and b"undefined reference" in err
    else:
        assert rc == 0, name
    return out.decode().strip()


def bazel(row, key, mode, flags, *args, **opts):
    return checked(row, key, [q.BAZEL, mode, *FLAGS, *flags, *args], **opts)


def inventory(row, flags):
    directory = Path(checked(row, "execroot", [q.BAZEL, "info", "execution_root"]))
    objects = bazel(row, "objects", "cquery", flags, "--output=files", 'filter(":sighandler_object$", deps(' + EXPR + '))')
    outputs = bazel(row, "outputs", "cquery", flags, "--output=files", EXPR).splitlines()
    assert len(outputs) == 4
    by_arch = {}
    for obj in set(objects.splitlines()):
        meta, data, exports = q.elf(q.read_file(directory, obj))
        arch = {62: "amd64", 183: "arm64"}[meta["machine"]]
        b, = [p for p in outputs if Path(p).name == f"sighandler.built-in.{arch}.bin"]
        g, = [p for p in outputs if Path(p).name == f"sighandler_{arch}.go"]
        bindings = re.findall(rb'^var Sighandler_blob_offset__(\w+) = (0x[0-9a-fA-F]+)$', q.read_file(directory, g), re.M)
        offsets = {k.decode():int(v, 16) for k,v in bindings}
        assert data and q.read_file(directory, b) == data and offsets and offsets == exports and len(offsets) == len(bindings)
        by_arch[arch] = {"bytes":len(data), "sha256":q.digest(data), "symbols":offsets}
    assert set(by_arch) == {"amd64","arm64"}
    row["outputs_by_arch"] = by_arch


def inspect_flags(row, flags):
    expr='mnemonic("CppCompile", deps('+OBJECT+'))'
    s=bazel(row,"aquery","aquery",flags,"--output=jsonproto","--noinclude_artifacts","--include_param_files",expr)
    d=json.loads(s);owners={str(t["id"]):t["label"] for t in d["targets"]}
    actions=[]
    for a in d.get("actions",[]):
        if not owners[str(a["targetId"])].startswith(q.SYSMSG):continue
        args=a.get("arguments",[])+[v for f in a.get("paramFiles",[]) for v in f.get("arguments",[])]
        actions.append({"medium":"-mcmodel=medium" in args,"threshold":"-mlarge-data-threshold=0" in args,"owner":owners[str(a["targetId"])]})
    row["x86_compiler_actions"]=actions
    assert actions
    return actions


def exercise(name,row):
    if name=="baseline":
        bazel(row,"build","build",[DISABLE],*ROOTS,cap=850)
        inventory(row,[DISABLE])
    if name=="stock_allocator":
        bazel(row,"build","build",[OVERRIDE,DISABLE],OBJECT,cap=500,expected_failure=True)
    if name=="stock_medium":
        assert all(a["medium"] and a["threshold"] for a in inspect_flags(row,[]))
        bazel(row,"build","build",[],OBJECT,cap=500)
        obj=bazel(row,"object","cquery",[],"--output=files",OBJECT)
        sections=checked(row,"sections",["readelf","-SW",obj])
        row["large_sections"] = re.findall(r"\]\s+(\.(?:lbss|ldata|lrodata)(?:\.\S+)?)\s+\S+\s+\S+\s+\S+\s+(\S+)",sections)
        assert any(int(size,16)>0 for section,size in row["large_sections"])
    if name=="fixed":
        assert all(not a["medium"] and not a["threshold"] for a in inspect_flags(row,[OVERRIDE]))
        bazel(row,"build","build",[OVERRIDE],*ROOTS,*SIDECARS,cap=850)
        inventory(row,[OVERRIDE])
        expr='str(build_options(target).get("//command_line_option:custom_malloc"))'
        actual=bazel(row,"inner_allocator","cquery",[OVERRIDE],"--output=starlark","--starlark:expr="+expr,'filter(":_linked/sighandler[.]built-in[.]bin[.]o$", deps('+OBJECT+'))')
        control=bazel(row,"ordinary_allocator","cquery",[OVERRIDE],"--output=starlark","--starlark:expr="+expr,"//test/rbe/allocator:ordinary")
        assert actual=="None" and control.endswith(ALLOCATOR)
        row["ordinary_keeps_allocator"]=True
        row["inner_clears_allocator"]=True


r={"published":STOCK,"product":FIX,"private_toolchain_executed":False,"phases":{}}
for sha,names in ((STOCK,("baseline","stock_allocator","stock_medium")),(FIX,("fixed",))):
    q.source(sha)
    patch=Path("tools/crosstool-link-flags.patch");original=patch.read_bytes()
    assert q.digest(original)=="f14a0654538c8df3e3c59ffbc9bffd4f9337146c65a2c3225068e0d0daad0191"
    patch.write_bytes(original+FEATURE.encode())
    fixture=Path("test/rbe/allocator");fixture.mkdir(exist_ok=True,parents=True)
    (fixture/"BUILD").write_text('load("@rules_cc//cc:defs.bzl","cc_binary","cc_library")\ncc_library(name="poison",srcs=["poison.c"],alwayslink=True,linkopts=["-Wl,-u,sidecar_qualification_allocator"],visibility=["//visibility:public"])\ncc_binary(name="ordinary",srcs=["ordinary.c"])\n')
    (fixture/"poison.c").write_text("extern void requires_a_hosted_runtime(void);\nvoid sidecar_qualification_allocator(void) { requires_a_hosted_runtime(); }\n")
    (fixture/"ordinary.c").write_text("int main(void) { return 0; }\n")
    try:
        for name in names:
            row=r["phases"][name]={}
            try:
                exercise(name,row);row["passed"]=True
            except (AssertionError,ValueError,KeyError,OSError) as err:
                row["error"]=type(err).__name__+": "+str(err)[:200]
            q.write(r)
    finally:
        patch.write_bytes(original)
r["same_stub"]=r["phases"]["baseline"].get("outputs_by_arch") is not None and r["phases"]["baseline"].get("outputs_by_arch")==r["phases"]["fixed"].get("outputs_by_arch")
q.write(r)
sys.exit(not (r["same_stub"] and all(row.get("passed") for row in r["phases"].values())))
