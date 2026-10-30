"""Compare default-root, dev-disabled and external consumption on identical Go code."""
import json
import os
from pathlib import Path
import subprocess

from consumer import verify

root = Path(os.environ["RUNNER_TEMP"])
case = os.environ["PROBE_CASE"]
result = {"case": case, "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "status": "incomplete"}
try:
    result["consumer"] = verify()
    if case == "stock":
        if not result["consumer"]["known_gazelle_failure"]:
            raise ValueError("Current stock did not reproduce the non-root override rejection")
    elif result["consumer"]["known_gazelle_failure"]:
        raise ValueError("The root-only tags are still reached from the external module")
    root_modes = {}
    result["root_modes"] = root_modes
    for tag, options in (("default", []), ("without_dev", ["--ignore_dev_dependency"])):
        show = subprocess.run(["bazel", "mod", "show_repo", "@org_golang_google_protobuf",
                               "@org_golang_google_grpc", "--lockfile_mode=update", *options],
                              text=True, capture_output=True)
        root_modes[tag] = {"exit":show.returncode,
                           "protobuf_patch": "protobuf.disable-binary-reading-on-init.patch" in show.stdout,
                           "grpc_proto_disable": "gazelle:proto disable" in show.stdout}
        if show.returncode:
            root_modes[tag]["error"] = [s[:420] for s in show.stderr.splitlines() if "ERROR:" in s and not any(t in s.lower() for t in ("token","credential","api-key","header","://"))][:5]
    if (root_modes["default"]["exit"] or not root_modes["default"]["protobuf_patch"] or not root_modes["default"]["grpc_proto_disable"]):
        raise ValueError("Default root did not retain its runtime overrides")
    if root_modes["without_dev"]["exit"]:
        raise ValueError("Cannot inspect root with --ignore_dev_dependency")
    result["status"] = "characterized"
finally:
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n")
