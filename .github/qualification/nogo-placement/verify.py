"""Verify configured analyzer placement and the small physical witness."""
import json
import sys
from pathlib import Path

high = "//tools/bazeldefs:rbe_linux_amd64_abi_nogo"
ordinary = "//tools/bazeldefs:rbe_linux_amd64"
packages = {
    "pkg/abi/linux": "linux",
    "pkg/bits": "bits",
    "pkg/sentry/fsimpl/fsconfigfd": "fsconfigfd",
    "pkg/sentry/fsimpl/mountfd": "mountfd",
    "pkg/sentry/kernel/pipe": "pipe",
}
expected = {
    "Arch": "amd64",
    "OSFamily": "linux",
    "container-image": "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
    "workload-isolation-type": "firecracker",
    "EstimatedMemory": "16GB",
}

out = Path(sys.argv[2])
def save(name, data):
    (out / (name + ".json")).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

def cpu(path):
    if path.startswith("bazel-out/k8-"):
        return "amd64"
    if path.startswith("bazel-out/aarch64-"):
        return "arm64"
    raise ValueError(path)

def fq(label):
    return label[label.index("//"):]

if sys.argv[1] == "configured":
    assert not json.loads((out / "probe-deps.json").read_bytes()).get("actions")
    [configured_platform] = json.loads((out / "platform.json").read_bytes())["results"]
    rule = configured_platform["target"]["rule"]
    assert fq(rule["name"]) == high and rule["ruleClass"] == "platform", rule["name"]
    [prop_attr] = [a for a in rule["attribute"] if a["name"] == "exec_properties"]
    platform = {p["key"]: p["value"] for p in prop_attr["stringDictValue"]}
    assert platform == expected, platform
    rows = []
    raw = json.loads((out / "configured.json").read_bytes())
    labels = {str(x["id"]): fq(x["label"]) for x in raw["targets"]}
    save("selected-actions", {
        "platform_properties": platform,
        "actions": [{"target": labels[str(a["targetId"])],
                     "platform": a.get("executionPlatform"),
                     "mnemonic": a.get("mnemonic")}
                    for a in raw.get("actions", [])],
    })
    aspects = {str(x["id"]): x["name"] for x in raw["aspectDescriptors"]}
    for a in raw["actions"]:
        label = labels[str(a["targetId"])]
        package, name = label[2:].split(":")
        if packages.get(package) != name:
            continue
        prefix = f"-archive=gvisor.dev/gvisor/{package}="
        [archive] = [v[len(prefix):] for v in a["arguments"] if v.startswith(prefix)]
        aspect = [aspects[str(i)] for i in a["aspectDescriptorIds"]]
        assert any("nogo_aspect" in v for v in aspect), aspect
        pform = fq(a["executionPlatform"])
        assert pform == (high if name in ("linux", "bits") else ordinary), (label, pform)
        rows.append({"target": label, "target_cpu": cpu(archive), "archive": archive,
                     "execution_platform": pform, "action_key": a["actionKey"]})
    assert sorted((v["target"], v["target_cpu"]) for v in rows) == sorted(
        (f"//{p}:{n}", arch) for p, n in packages.items() for arch in ("amd64", "arm64")
    ), rows
    save("configured-proof", {"platform_properties": platform, "analyzers": rows})
elif sys.argv[1] == "physical":
    rows = []
    for line in (out / "bits.jsonl").open():
        a = json.loads(line)
        props = {v["name"]: v["value"] for v in a["platform"]["properties"]}
        assert props == expected and a["runner"] == "remote", (props, a["runner"])
        assert not a.get("cacheHit") and a.get("exitCode", 0) == 0, a["runner"]
        prefix = "-archive=gvisor.dev/gvisor/pkg/bits="
        [archive] = [v[len(prefix):] for v in a["commandArgs"] if v.startswith(prefix)]
        rows.append({"target": a["targetLabel"], "target_cpu": cpu(archive),
                     "archive": archive, "properties": props, "runner": a["runner"],
                     "actual_outputs": [v["path"] for v in a["actualOutputs"]]})
    assert sorted(r["target_cpu"] for r in rows) == ["amd64","arm64"], rows
    events = [json.loads(l) for l in (out / "probe.bep").open()]
    [start] = [e["started"] for e in events if "started" in e]
    [finish] = [e["finished"] for e in events if "finished" in e]
    assert finish["exitCode"]["name"] == "SUCCESS", finish
    save("physical-proof", {"small_owner": rows, "invocation": start["uuid"],
                            "native_finished": finish["finishTime"], "exit": "SUCCESS"})
