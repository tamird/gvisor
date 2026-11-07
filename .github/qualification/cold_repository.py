"""Recover the first repository error from the credential-free archive query."""

import hashlib
import json
import re
import subprocess
from urllib.parse import urlparse

import registry_shipping as q


def public_url(match):
    url = urlparse(match[0].rstrip(".,);]"))
    host = url.hostname or ""
    public = ("github.com", "githubusercontent.com", "bazel.build", "ubuntu.com", "golang.org",
              "go.dev", "gcr.io", "storage.googleapis.com", "gnu.org", "python.org", "pypi.org", "pythonhosted.org")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in public):
        return "<dependency-url>"
    return host + url.path[:270]


def scrub(line):
    line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
    line = re.sub(r"https?://[^\s\]\[\"'<>]+", public_url, line)
    line = re.sub(r"/home/runner/\.cache/bazel/_bazel_runner/[a-f0-9]{32}", "<output-base>", line)
    line = re.sub(r"(?:/tmp|/home/runner/work/_temp)/[^ /:]+", "<temp>", line)
    line = re.sub(r"(?i)\b(?:authorization|proxy-authorization|x-[\w-]+-key|cookie|password|credential|token)\s*[:=].*$",
                  "<redacted header>", line)
    return "".join(c if c.isprintable() else " " for c in line).strip()[:900]


def headlines(raw):
    lines = [scrub(line) for line in raw.decode(errors="replace").splitlines()]
    primary, context = [], []
    for n, line in enumerate(lines):
        if re.search(r"(?i)^error:|^(?:error in |caused by:)|(?:failed to fetch|error downloading|failed to extract|exception:)", line):
            primary.extend([line] + [s for s in lines[n+1:n+3] if s and not s.startswith("INFO:")])
        elif re.search(r"(?i)warning:|repository .* instantiated at|repository rule |download_and_extract|\.bzl.*fail\(", line):
            context.append(line)
    return {"primary":list(dict.fromkeys(primary))[:14],"context":list(dict.fromkeys(context))[-10:],
            "tail":[s for s in lines[-12:] if s] if not primary else [],
            "sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}


def main():
    assert not (q.W / "remote.bazelrc").exists()
    q.setup()
    args = [*q.command("baseline",False), "aquery", *q.flags("amd64","baseline"),
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--include_artifacts=false",
            "--output=jsonproto", 'mnemonic("PackageTar", deps(set(' + " ".join(q.ROOTS) + ')))']
    result = subprocess.run(args,capture_output=True)
    doc = {"source":q.BASE,"original":42377059065,"bazel_exit":result.returncode,"query":args[-1],
           "stderr":headlines(result.stderr), "graph_configured":result.returncode == 0}
    if result.returncode:
        doc["stdout"] = headlines(result.stdout)
        captured = any(doc[k]["primary"] or doc[k]["tail"] for k in ("stdout","stderr"))
    else:
        graph = json.loads(result.stdout)
        labels = {str(t["id"]):t["label"].lstrip("@") for t in graph["targets"]}
        doc["owners"] = sorted(labels[str(a["targetId"])] for a in graph["actions"] if a["mnemonic"] == "PackageTar")
        captured = doc["owners"] == sorted([q.INNER,q.PACK])
    doc["original_outcome_captured"] = captured
    q.save("cold-repository",doc)
    print("Original cold archive query exit", result.returncode)
    return not captured


if __name__ == "__main__":
    raise SystemExit(main())
