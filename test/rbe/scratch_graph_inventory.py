"""Summarize configured actions without retaining arguments or environment values."""
import collections
import json
import os
from pathlib import Path
import sys

def describe(path):
    with path.open() as source:
        graph = json.load(source)
    targets = {str(t["id"]): t["label"] for t in graph["targets"]}
    counts = collections.Counter()
    host_tools = collections.Counter()
    ignored = 0
    for action in graph["actions"]:
        mnemonic = action.get("mnemonic", "")
        counts[mnemonic] += 1
        args = action.get("arguments", ())
        if not args:
            ignored += 1
            continue
        program = args[0]
        if program.startswith("/") or "/" not in program:
            host_tools[(program, mnemonic, targets[str(action["targetId"])])] += 1
    if not targets or not counts or sum(counts.values()) == ignored:
        raise ValueError(f"{path.stem}: action graph is empty or lacks command lines")
    return {
        "targets": len(targets), "actions": sum(counts.values()),
        "without_command": ignored, "mnemonics": dict(counts.most_common()),
        "absolute_or_path_lookup": [
            dict(program=program, mnemonic=mnemonic, owner=owner, actions=n)
            for (program, mnemonic, owner), n in sorted(host_tools.items())
        ],
    }

def main():
    out = Path(sys.argv[1])
    sets = {name: describe(Path(sys.argv[2]) / (name + ".json")) for name in ("pkg", "runsc")}
    result = {"source": os.environ["GITHUB_SHA"], "selection": sets,
              "kind": "configured action inventory; not an execution result"}
    (out / "graph.json").write_text(json.dumps(result, indent=2) + "\n")
    for name, row in sets.items():
        hosts = collections.Counter()
        for entry in row["absolute_or_path_lookup"]:
            hosts[entry["program"]] += entry["actions"]
        print(f'{name}: {row["targets"]} targets, {row["actions"]} actions, {hosts}')

if __name__ == "__main__":
    main()
