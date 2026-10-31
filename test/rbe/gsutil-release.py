import json
import os
from pathlib import Path
import re
import subprocess
import sys

arch, arm = sys.argv[1:3]
cpu = {"x86_64": "amd64", "aarch64": "arm64"}[arch]
temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "results" / arch / arm
out.mkdir(parents=True, exist_ok=True)
prefix = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/benchmarks/gsutil_"
image = prefix + arch + ":7eba9c02d11172d4" if arm == "shipped" else "gvisor.dev/images/benchmarks/gsutil:latest"
if arm == "shipped":
    subprocess.run(["docker", "pull", "--platform=linux/" + cpu, image], check=True)
info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
assert info["Architecture"] == cpu
keys = ["Architecture", "Id", "RepoDigests", "RepoTags", "Size"]
(out / "image.json").write_text(json.dumps({k: info[k] for k in keys}, indent=2))
if arm == "candidate":
    tags = [tag for tag in info["RepoTags"] if tag.startswith(prefix+arch+":") and re.fullmatch(r"[a-f0-9]{16}", tag.rsplit(":",1)[1])]
    assert len(tags) == 1, tags
    (out / "published-name").write_text(tags[0]+"\n")
boto = temp / "probe.boto"
boto.write_text("[GSUtil]\nsliced_object_download_threshold=1M\nsliced_object_download_component_size=16M\n")
cmd = ["docker", "run", "--rm", "--platform=linux/"+cpu, "-v", str(boto)+":/probe.boto:ro", "-e", "BOTO_CONFIG=/probe.boto", image]
version = subprocess.check_output(cmd + ["sh", "-ec", 'id -u; . /etc/os-release; echo "$PRETTY_NAME"; gcloud version; gsutil version -l'], text=True)
(out / "version").write_text(version)
assert re.search(r"compiled crcmod: (True|False)", version, re.I), version
commands = json.loads((temp / "commands.json").read_text())
outcomes = []
for mode in ["single","sliced"]:
    argv = list(commands[mode])
    assert len(argv) == 3 and argv[:2] == ["sh","-c"]
    text = argv[-1]
    assert text.count("gs://gvisor-benchmark-testdata/bigrandomfile") == 1
    text = text.replace("gs://gvisor-benchmark-testdata/bigrandomfile", "gs://gvisor-benchmark-testdata/littlerandomfile")
    assert text.count("gsutil ") == 1
    text = text.replace("gsutil ", "gsutil -D ")
    # The container has no credentials. Keep only range numbers and timing markers from debug output.
    text += " && stat -c 'probe-bytes=%s' /downloads/randombigfile && gsutil hash -c /downloads/randombigfile"
    p = subprocess.run(cmd + argv[:2] + [text], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = p.stdout.decode("utf8", errors="replace")
    ranges = sorted(set(re.findall(r"bytes=(\d+-\d*)", log, re.I)))
    components = sorted(set(re.findall(r"randombigfile component (\d+)", log)))
    timing = re.findall(r"::::TIMING::::(\d+)::::(\d+)", log)
    failures = sorted(set(re.findall(r"\b((?:\w+Error|\w+Exception)(?=:))", log))) if p.returncode else []
    row = dict(mode=mode, exit=p.returncode, ranges=ranges, components=components, timed=bool(timing), bytes=52428800 if "probe-bytes=52428800" in log else 0, crc32c="1BRP5g==" if "1BRP5g==" in log else "", failure_types=failures)
    outcomes.append(row)
    (out / "transfers.json").write_text(json.dumps(outcomes, indent=2))
    print(arch,arm,row, flush=True)
    assert p.returncode == 0 and timing and "::::TIMING::::FAILURE" not in log and row["bytes"] and row["crc32c"], row
    if mode == "sliced" and "compiled crcmod: True" in version:
        assert len({r.split("-")[0] for r in ranges}) > 1 or len(components) > 1, row
(out / "verdict").write_text("PASS\n")
