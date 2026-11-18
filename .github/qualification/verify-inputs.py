import hashlib
import json
from pathlib import Path
import subprocess
import sys

source = "d2c5c08fa3753bcf14ea6015fad547ee8f3d531c"
base = "1f33fe3003455db178b97f3b43d3a012f1611994"
results = Path(sys.argv[1])
def git(*args):
    return subprocess.check_output(["git", *args])
assert git("rev-parse", "HEAD").decode().strip() == base
assert set(git("diff", "--name-only").decode().splitlines()) == {
    "runsc/cmd/sentry/forwardcmd/statefile_test.go",
    "runsc/cmd/sentry/forwardcmd/BUILD",
    "pkg/sentry/pgalloc/BUILD",
}
assert not git("diff", "--cached", "--name-only")
path = "runsc/cmd/sentry/forwardcmd/statefile_test.go"
s = git("show", source + ":" + path).decode()
s = s[:s.index('\n\tt.Run("GenericCollision"')] + "\n}\n"
for item in ["context", "errors", "io", "github.com/google/subcommands", "golang.org/x/sys/unix", "gvisor.dev/gvisor/runsc/flag"]:
    s = s.replace('\t"' + item + '"\n', "")
assert Path(path).read_text() == s
assert "printState(" not in s
original = git("show", base + ":runsc/cmd/sentry/forwardcmd/BUILD").decode()
anchor = '    deps = [\n        "//pkg/state/statefile",\n'
assert original.count(anchor) == 1
expected = original.replace(anchor, '    deps = [\n        "//pkg/sentry/pgalloc:pgalloc_metadata_go_proto",\n        "//pkg/state",\n        "//pkg/state/pretty",\n        "//pkg/state/statefile",\n        "//pkg/state/wire",\n        "@org_golang_google_protobuf//proto:go_default_library",\n')
assert Path("runsc/cmd/sentry/forwardcmd/BUILD").read_text() == expected
pgalloc = git("show", base + ":pkg/sentry/pgalloc/BUILD").decode()
proto_anchor = '    name = "pgalloc_metadata",\n    srcs = ["pgalloc.proto"],\n'
assert pgalloc.count(proto_anchor) == 1
proto_visibility = '    visibility = [\n        "//pkg/sentry/pgalloc:__subpackages__",\n        "//runsc/cmd/sentry/forwardcmd:__pkg__",\n    ],\n'
assert Path("pkg/sentry/pgalloc/BUILD").read_text() == pgalloc.replace(proto_anchor, proto_anchor + proto_visibility)
prod = "runsc/cmd/sentry/forwardcmd/statefile.go"
assert Path(prod).read_bytes() == git("show", base + ":" + prod)
(results / "fixture-identity.json").write_text(json.dumps({"base": base, "fixtureSource": source, "productionUnchanged": True, "testSubset": "Exact six positive source fixture cases, omitting later tests and unused imports", "fixtureSHA256": hashlib.sha256(s.encode()).hexdigest()}, indent=2) + "\n")
