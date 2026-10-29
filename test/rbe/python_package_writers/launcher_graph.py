"""Check the configured launcher commands before running the two package writers."""

import json
from pathlib import Path
import sys

source, out = map(Path, sys.argv[1:])
graph = json.loads(source.read_bytes())
labels = {r["id"]: r["label"] for r in graph["targets"]}
writers = {}
for action in graph["actions"]:
    owner = labels[action["targetId"]]
    name = owner.rsplit(":", 1)[-1]
    if name not in ("build_tar", "make_deb"):
        continue
    args = action.get("arguments", [])
    if "--template" not in args:
        continue
    assert "--transform" in args and args[args.index("--transform") + 1] == "0,3", args
    interpreter, isolated, no_site, adapter = args[args.index("--") + 1:]
    assert (isolated, no_site) == ("-I", "-S") and interpreter.endswith("/bin/python3.11")
    assert adapter.endswith(name + ".bootstrap.py") and adapter.startswith("rules_pkg+/"), adapter
    target = args[args.index("-o") + 1]
    assert "-exec" in target and target.endswith("/" + name), target
    writers[name] = {"owner": owner, "executable": target, "interpreter": interpreter, "adapter": adapter}
assert set(writers) == {"build_tar", "make_deb"}, writers
out.write_text(json.dumps(writers, indent=2) + "\n")
