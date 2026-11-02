"""Check just the published product differences in the two instrumented senders."""

from pathlib import Path
import re
import subprocess
import tempfile

BASE = "918aefd5f3771a161013fb72873d8aa19196e1bc"
OBSERVER = "b779a7dcd19ab980c4e8893ca7c19707f8aaa907"
FLIGHT = "4189b8df0a1d7c27e873fcb071462ee012fddbfa"
BOUNDARY = "8dccc94866058d9a8f7c9ec588c99a6ba96dc6bf"
PRODUCTS = {FLIGHT: ("bd4522d257ee43bb8d0a4c3852b63f58aac22438", "00aaa82b9e9cda8dc25dbe1df6e07a56c68302c5"),
            BOUNDARY: ("99058130824e4d3166bf0e376f9b15b6ad50146c", "5fd5532c13b7e4a17477430fe0ce2f5dc18b599f")}
DIR = "pkg/tcpip/transport/tcp/"
FILES = {DIR + n + ".go" for n in ("rack", "snd", "segment", "state")}


def git(*args):
    return subprocess.check_output(["git", *args])


def normalized(src):
    return [line.strip() for line in src.splitlines() if line.strip() and not line.strip().startswith(b"//")]


def unobserve(src):
    src = src.split(b"\n// ResearchState records only this fork", 1)[0]
    # Remove an if only when recording one event is its entire body.
    src = re.sub(rb'(?m)^\t+if [^\n]+ \{\n\t+[\w.]+\.researchEvent\("[^\n]+\n\t+\}\n', b"", src)
    return normalized(b"\n".join(line for line in src.splitlines() if b'.researchEvent("' not in line))


def check():
    for arm, (parent, head) in PRODUCTS.items():
        files = git("diff", "--name-only", OBSERVER, arm).decode().splitlines()
        assert files and set(files) <= FILES, files
        changed = git("diff", "--name-only", parent, head, "--", DIR).decode().splitlines()
        assert set(f for f in changed if not f.endswith("_test.go") and "/test/" not in f and "/testing/" not in f) <= FILES, changed
        for f in FILES:
            source = lambda ref: git("show", ref + ":" + f)
            assert unobserve(source(OBSERVER)) == normalized(source(BASE)), f
            with tempfile.TemporaryDirectory() as work:
                paths = [Path(work) / n for n in ("baseline", "parent", "upstream")]
                for path, ref in zip(paths, (BASE, parent, head)):
                    path.write_bytes(source(ref))
                merged = subprocess.run(["git", "merge-file", "-p", "--diff3", *paths], capture_output=True)
            raw = merged.stdout
            if merged.returncode:
                # The common base already advances writeNext after resending
                # this segment. The two branches word the comment differently.
                assert (arm, f, merged.returncode) == (FLIGHT, DIR + "snd.go", 1)
                m, = re.finditer(rb'(?ms)^<<<<<<< [^\n]*\n(.*?)^\|{7} [^\n]*\n=======\n(.*?)^>>>>>>> [^\n]*\n', raw)
                assert normalized(m[1]) == normalized(m[2]) == [b"if s.writeNext == seg {", b"s.updateWriteNext(seg.Next())", b"}"]
                raw = raw[:m.start()] + m[1] + raw[m.end():]
            assert unobserve(source(arm)) == normalized(raw), (arm, f)
            print(arm[:9], head[:9], f)


if __name__ == "__main__":
    check()
