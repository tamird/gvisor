"""Trace vDSO metadata and configured edges from a plain consuming root."""

import json
import platform
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from hashlib import sha256

import downstream_llvm as qa


SRC = "42ac721046639db7753e69f9794b296390ef4bb4"
VDSO = qa.TARGET
NORMAL = "@gvisor//:sandbox"
BUILTIN = "@gvisor//:__subpackages__"
LICENSE = "@gvisor//:license"
PACKAGES = {"plain": (None, False), "visibility_only": (NORMAL, False),
            "visibility_builtin": (BUILTIN, False), "license_only": (None, True),
            "both": (NORMAL, True), "both_builtin": (BUILTIN, True)}
PROBE = 'extern "C" int downstream_abi_probe(int x) { return x + 1; }\n'


def workspace():
    qa.CONSUMER.mkdir()
    module = ('module(name = "downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\n'
              f'local_path_override(module_name = "gvisor", path = {json.dumps(str(qa.SRC))})\n')
    (qa.CONSUMER / "MODULE.bazel").write_text(module)
    (qa.CONSUMER / ".bazelversion").write_text("9.3.1\n")
    (qa.CONSUMER / "BUILD.bazel").write_text("")
    qa.REPORT["consumerModule"] = module.replace(str(qa.SRC), "<source>")
    for name, (visibility, license_) in PACKAGES.items():
        pkg = qa.CONSUMER / name
        pkg.mkdir()
        lines = ['load("@gvisor//tools/bazeldefs:cc.bzl", "cc_binary")']
        if license_:
            lines.append('package(default_applicable_licenses = ["@gvisor//:license"])')
        lines.extend(['cc_binary(', '    name = "probe.so",', '    srcs = ["probe.cc"],',
                      '    additional_linker_inputs = [],', '    copts = ["-fPIC", "-fno-stack-protector"],',
                      '    features = ["-default_link_flags", "-pie"],', '    link_extra_lib = None,',
                      '    linkopts = ["-nostdlib", "-Wl,--no-undefined"],', '    linkshared = True,',
                      '    linkstatic = True,', '    stamp = 0,'])
        if visibility:
            lines.append('    visibility = [' + json.dumps(visibility) + '],')
        lines.extend([')', ''])
        (pkg / "BUILD.bazel").write_text("\n".join(lines))
        (pkg / "probe.cc").write_text(PROBE)


def fields(case, name, target):
    row, out = qa.bazel(case, name, "query", target, "--output=xml", "--xml:default_values")
    if row["exit"]:
        return
    rule = ET.fromstring(out).find("rule")
    if rule is None:
        return
    row["ruleClass"] = rule.get("class")
    row["metadata"] = {}
    for attr in rule:
        key = attr.get("name")
        if key in ("visibility", "applicable_licenses", "srcs", "additional_linker_inputs",
                   "link_extra_lib", "malloc", "deps"):
            row["metadata"][key] = ([attr.get("value")] if attr.get("value") is not None else
                                    [item.get("value") for item in attr if item.get("value") is not None])[:12]
    row["gvisorDirectInputs"] = [e.get("name") for e in rule.findall("rule-input")
                                 if "gvisor" in e.get("name", "")][:30]
    qa.save()


def query(case, name, operation, target, *options):
    row, out = qa.bazel(case, name, operation, *options, target, "--output=label")
    row["projectLabels"] = [s for s in out.decode(errors="replace").splitlines()
                            if s.startswith(("@gvisor//", "@@gvisor+//", "//")) and len(s) < 250][:22]
    qa.save()
    return row["exit"] == 0


def ordinary_link(case):
    target = "//plain:probe.so"
    planned, out = qa.bazel(case, "configured", "aquery", qa.ARCH, "--output=jsonproto",
                            'mnemonic("CppCompile|CppLink", ' + target + ')')
    if planned["exit"]:
        return
    actions = {}
    for a in json.loads(out)["actions"]:
        args = a.get("arguments", [])
        actions[a["mnemonic"]] = {"argv0": args[0][:240] if args else None,
                                  "llvm": bool(args and "external/" in args[0] and "llvm" in args[0].lower())}
    planned["actions"] = actions
    bep = qa.AREA / "control.bep"
    built, _ = qa.bazel(case, "build", "build", qa.ARCH, "--jobs=3",
                        "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), target, timeout=1200)
    reported = []
    if bep.exists():
        with bep.open() as f:
            for line in f:
                e = json.loads(line)
                action = e.get("action", {})
                uid = e.get("id", {}).get("actionCompleted", {})
                if action.get("type") in ("CppCompile", "CppLink") and (
                        uid.get("label") == target or "/plain/" in uid.get("primaryOutput", "")):
                    reported.append({"mnemonic": action["type"], "success": action.get("success", False)})
    built["reportedOwnerActions"] = reported[:12]
    qa.save()
    if built["exit"]:
        return
    row, out = qa.bazel(case, "defaultOutput", "cquery", qa.ARCH, "--output=files", target)
    files = [qa.CONSUMER / line for line in out.decode().splitlines() if line.endswith("/probe.so")]
    if row["exit"] or len(files) != 1 or not files[0].is_file():
        return
    data = files[0].read_bytes()
    symbols = subprocess.check_output(["readelf", "--dyn-syms", "--wide", str(files[0])], text=True)
    elf = {"sha256": sha256(data).hexdigest(), "size": len(data), "elf64le": data[:6] == b"\x7fELF\2\1",
           "type": struct.unpack_from("<H", data, 16)[0], "machine": struct.unpack_from("<H", data, 18)[0],
           "definedSymbol": any(line.rstrip().endswith(" downstream_abi_probe") and " UND " not in line
                                for line in symbols.splitlines())}
    built["elf"] = elf
    case["verified"] = (set(actions) == {"CppCompile", "CppLink"} and actions["CppCompile"]["llvm"] and
                        elf["elf64le"] and elf["type"] == 3 and elf["machine"] == 183 and elf["definedSymbol"])


def main():
    assert platform.machine() == "x86_64" and qa.git("rev-parse", "HEAD^") == SRC
    qa.git("switch", "--quiet", "--detach", SRC)
    assert qa.git("status", "--porcelain", "--untracked-files=no") == ""
    workspace()
    edges = qa.REPORT["cases"]["edges"] = {}
    source = qa.REPORT["cases"]["source"] = {"source": SRC}
    physical = qa.REPORT["cases"]["consumerELF"] = {"verified": False}
    qa.save()
    for name in PACKAGES:
        query(edges, name, "cquery", "//" + name + ":probe.so", qa.ARCH)
    for name, target in (("vdso", VDSO), ("linker", "@gvisor//vdso:linker_script"),
                         ("selector", "@gvisor//tools/bazeldefs:arm64")):
        fields(source, name + "_declared", target)
        query(source, name + "_configured", "cquery", target, qa.ARCH)
    query(source, "direct_one_level", "query", "deps(" + VDSO + ", 1)", "--keep_going")
    for name, attr in (("visibility", "visibility"), ("license", "applicable_licenses")):
        query(source, "declared_" + name, "query", "labels(" + attr + ", " + VDSO + ")", "--keep_going")
    query(source, "root_sandbox", "query", NORMAL)
    query(source, "root_license", "query", LICENSE)
    if edges["plain"]["exit"] == 0:
        ordinary_link(physical)
    qa.REPORT["collected"] = all("exit" in edges[k] for k in PACKAGES) and "exit" in source["vdso_configured"]
    qa.REPORT["physicalConsumerLink"] = physical.get("verified", False)
    qa.REPORT["vdsoConfigured"] = source["vdso_configured"]["exit"] == 0
    qa.save()
    return int(not (qa.REPORT["collected"] and qa.REPORT["physicalConsumerLink"]))


if __name__ == "__main__":
    sys.exit(main())
