"""Check which released DPDK trees the pinned TLDK patch series can consume."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

stage = Path(os.environ["RUNNER_TEMP"]) / "dpdk-qualification"
module = Path("MODULE.bazel")
original = module.read_text()
begin = original.index('http_archive(\n    name = "plugin_dpdk",')
block = original[begin:original.index("\n)", begin) + 2]
patches = re.findall(r'^ {8}"([^\n"]+\.patch)",$', block, re.MULTILINE)
assert len(patches) == 6
integration, = [p for p in patches if "/0004-" in p]
releases = [
    ("18.11.11", "51625b133e63197d1de79b4d43818709cbde48cf", "6b2a6b17334d2f7b72884b7e2a6ea65825727ef947f32a53fcba4059e82f8780"),
    ("19.11.14", "cd28195eaf5d0d089a2737106a2a9ce328b23844", "0eafe3dd28032629b07c710116bd59809a4ce76c3aa50819b5a043b7b58a10c9"),
    ("25.11.4", "6953b6da139a83b62c92093aab8e88a5c1e0c042", "ae1b729be62cf8504ccc827e006f48360f6515bac548b38af9f4a211f6583936"),
]
bases = [("18.11", "dpdk-18.11", "0026a9bc67f37f07969101bdf72fd5921169b4744f765118e5db57cace645bee",
          "https://github.com/DPDK/dpdk/archive/refs/tags/v18.11.tar.gz")]
bases += [(v, "dpdk-stable-" + rev, digest, "https://codeload.github.com/DPDK/dpdk-stable/tar.gz/" + rev)
          for v, rev, digest in releases]
experiments = []
decls = []
for version, prefix, digest, url in bases:
    for label, series in (("full", patches), ("plugin", [integration])):
        repo = "dpdk_" + version.replace(".", "_") + "_" + label
        experiments.append((repo, version, label))
        decls.append(f"""
http_archive(
    name = "{repo}",
    urls = ["{url}"],
    type = "tar.gz",
    sha256 = "{digest}",
    strip_prefix = "{prefix}",
    patches = {json.dumps(series)},
    patch_args = ["-p1"],
    build_file_content = "filegroup(name = 'source', srcs = glob(['README*'], allow_empty = False), visibility = ['//visibility:public'])",
)""")
rows = []
try:
    module.write_text(original + "\n".join(decls) + "\n")
    for repo, version, series in experiments:
        target = "@" + repo + "//:source"
        run = subprocess.run([stage / "bazel", "--nosystem_rc", "--nohome_rc", "build",
                              "--lockfile_mode=off", "--color=no", "--curses=no", "--noshow_progress",
                              target], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        output = run.stdout.decode(errors="replace")
        # A download or toolchain configuration failure says nothing about patch compatibility.
        failed = [p.rsplit(":", 1)[-1].rsplit("/", 1)[-1] for p in patches
                  if p.rsplit("/", 1)[-1].split(":")[-1] in output]
        diagnostics = [line.strip()[:500] for line in output.splitlines() if
                       any(s in line.lower() for s in ("cannot find", "applying patch", "failed to apply", "incorrect chunk",
                                                        "wrong position", "error applying", "does not exist", "looking for patch",
                                                        "some hunks failed","could not apply","couldn't apply","failed to patch"))]
        row = {"release": version, "series": series, "exit": run.returncode,
               "log_sha256": hashlib.sha256(run.stdout).hexdigest(),
               "patch_names": failed, "diagnostic": diagnostics[:8]}
        rows.append(row)
        (stage / "result.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(json.dumps(row), flush=True)
finally:
    module.write_text(original)
assert len(rows) == 8 and rows[0]["exit"] == 0
assert all(r["exit"] == 0 or (r["patch_names"] and r["diagnostic"]) for r in rows)
