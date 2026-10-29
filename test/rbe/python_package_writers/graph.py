"""Identify the actual configured build-data producers used by rules_pkg."""

import hashlib
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
graph = json.loads((out / "aquery.json").read_bytes())
targets = {int(v["id"]): v["label"] for v in graph["targets"]}
frags = {int(v["id"]): v for v in graph["pathFragments"]}
arts = {int(v["id"]): int(v["pathFragmentId"]) for v in graph["artifacts"]}
configs = {int(v["id"]): v for v in graph["configuration"]}


def path(fid):
    result = []
    while fid:
        f = frags[fid]
        result.append(f["label"])
        fid = int(f.get("parentId", 0))
    return "/".join(reversed(result))


results = []
for a in graph["actions"]:
    owner = targets[int(a["targetId"])]
    name = owner.rsplit(":", 1)[-1]
    if name not in ("make_deb", "build_tar"):
        continue
    cfg = configs[int(a["configurationId"])]
    for f in a["outputIds"]:
        p = path(arts[int(f)])
        if not p.endswith("/" + name + ".build_data.txt"):
            continue
        exec_config = "-exec-" in p
        action = a["mnemonic"]
        env = {v["key"]: v["value"] for v in a.get("environmentVariables", [])}
        if exec_config:
            assert action in ("FileWrite", "ParameterFileWrite") and not a.get("arguments"), (owner, cfg, action)
        else:
            assert action == "PyWriteBuildData" and env.get("STAMPED") == "TRUE", (owner, cfg, action, env.get("STAMPED"))
        results.append({"owner": owner, "name": name, "exec": exec_config, "action": action,
                        "configuration": cfg["checksum"], "path": p, "stamped": env.get("STAMPED")})
assert sorted((v["name"], v["exec"]) for v in results) == sorted((n, b) for n in ("make_deb", "build_tar") for b in (False, True)), results
(out / "build-data.json").write_text(json.dumps(results, indent=2) + "\n")
