"""Run only the declared generator on the normal fork scratch worker."""
import json
import os
from pathlib import Path
import subprocess

receipt = Path(os.environ["RUNNER_TEMP"]) / "website-probe"
secret = os.environ.pop("BUILDBUDDY_API_KEY")
assert secret and "\n" not in secret
config = Path(os.environ["RUNNER_TEMP"]) / "website-bazelrc"
config.write_text(f"build --remote_header=x-buildbuddy-api-key={secret}\nbuild --bes_header=x-buildbuddy-api-key={secret}\n")
config.chmod(0o600)
event_file = receipt / "site.bep"
argv = [
    "bazel", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc", "--bazelrc=" + str(config), "build",
    "--remote_executor=grpcs://remote.buildbuddy.io", "--remote_cache=grpcs://remote.buildbuddy.io",
    "--bes_backend=grpcs://remote.buildbuddy.io", "--bes_results_url=https://app.buildbuddy.io/invocation/",
    "--remote_download_outputs=toplevel", "--noremote_local_fallback", "--noremote_accept_cached",
    "--spawn_strategy=remote", "--extra_execution_platforms=//test/rbe/website-jekyll:scratch", "--workspace_status_command=",
    "--jobs=3", "--curses=no", "--color=no", "--noshow_progress", "--verbose_failures",
    "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(event_file),
    "//test/rbe/website-jekyll:site_generator",
]
try:
    res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800)
    log = res.stdout.decode(errors="replace").replace(secret, "[redacted]")
    status = res.returncode
finally:
    config.unlink(missing_ok=True)
receipt.joinpath("bazel.log").write_text(log)
print("\n".join(line[:1000] for line in log.splitlines() if any(s in line for s in ["ERROR:", "FAILED:", "invocation/", "failed:", "LoadError", "No such file", "not found", "PULL", "ExecJS", "CompilationError", "syntax error"]))[-9700:])
events = []
if event_file.exists():
    for line in event_file.read_text().splitlines():
        ev=json.loads(line)
        if "started" in ev:
            events.append({"invocation":ev["started"]["uuid"]})
        if "action" in ev:
            act=ev["action"]
            events.append({"type":act.get("type"),"success":act.get("success"),"exitCode":act.get("exitCode"),"primaryOutput":act.get("primaryOutput",{}).get("name")})
receipt.joinpath("actions.json").write_text(json.dumps({"exit":status,"events":events}, indent=2)+"\n")
event_file.unlink(missing_ok=True)
if status == 0:
    result = Path("bazel-bin/test/rbe/website-jekyll/site_generator.json").read_bytes()
    parsed = json.loads(result)
    assert parsed["no_worker_shell"] and parsed["jekyll"] == "4.0.0" and len(parsed["plugin_names"]) == 6
    receipt.joinpath("generator.json").write_bytes(result)
raise SystemExit(status)
