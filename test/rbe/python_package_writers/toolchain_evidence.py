"""Compare compiler and writer selections across the Python settings."""
import json
import os
from pathlib import Path
import re


folder = Path(os.environ["REPORT"])
report = {}
for arch in ("x86_64", "aarch64"):
    modes = {}
    for mode in ("default", "musl", "static"):
        data = json.loads((folder / f"{arch}-{mode}.json").read_text())
        by_type = {}
        for act in data["actions"]:
            argv = [re.sub(r"bazel-out/[^/]+/", "bazel-out/CFG/", arg) for arg in act["arguments"]]
            kind = act["mnemonic"]
            if kind == "GoCompilePkg":
                choice = (argv[0], argv[argv.index("-sdk") + 1]) if "-sdk" in argv else (argv[0],)
            elif kind == "CppCompile":
                choice = (argv[0], *[arg for arg in argv if arg.startswith("--target=") or arg.startswith("--sysroot=")])
            elif kind == "PackageTar":
                choice = (argv[0],)
            else:
                continue
            by_type.setdefault(kind, set()).add(choice)
        assert by_type.keys() == {"CppCompile", "GoCompilePkg", "PackageTar"}, by_type
        modes[mode] = {key: sorted(value) for key, value in by_type.items()}
    for kind in ("GoCompilePkg", "CppCompile"):
        assert modes["default"][kind] == modes["musl"][kind] == modes["static"][kind], modes
    assert modes["default"]["PackageTar"] != modes["static"]["PackageTar"], modes
    report[arch] = modes
(folder / "verdict.json").write_text(json.dumps({"status":"PASS","selections":report}, indent=2))
