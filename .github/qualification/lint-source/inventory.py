"""Compare the native source graph with the unchanged lint.sh file selectors."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys


SOURCE = "f4c82a02fc3e8452463d5b58f013ba2140003f17"
TREE = "aa3911e81c493365c537624fa0135cc894d7ce59"
QUERY = 'kind("source file", //...:*) union buildfiles(//...:*)'


def output(*argv: str) -> str:
    return subprocess.check_output(argv, text=True)


def fingerprint(paths: set[str]) -> str:
    return hashlib.sha256("".join(p + "\0" for p in sorted(paths)).encode()).hexdigest()


def path_for_label(label: str) -> str | None:
    for prefix in ("@@//", "@//", "//"):
        if label.startswith(prefix):
            package, separator, target = label.removeprefix(prefix).partition(":")
            if not separator:
                target = PurePosixPath(package).name
            return str(PurePosixPath(package, target))
    if not label.startswith("@"):
        raise ValueError(f"unexpected label: {label}")
    return None


def main() -> None:
    assert output("git", "rev-parse", "HEAD").strip() == SOURCE
    assert output("git", "rev-parse", "HEAD^{tree}").strip() == TREE
    lint = Path("tools/lint.sh").read_text()
    start = lint.index("go_files() {")
    # These are the actual shell functions used by every maintained check.
    selectors = lint[start:lint.index("\n}", start) + 2]
    golden = ":(exclude)tools/go_generics/tests/simple/output.go"
    assert f"local -r golden_outputs='{golden}'" in lint
    labels = set(output("bazel", "query", "--output=label", QUERY).splitlines())
    assert labels
    paths = {value for label in labels if (value := path_for_label(label)) is not None}
    packages = {str(PurePosixPath(p).parent) for p in paths if PurePosixPath(p).name in ("BUILD", "BUILD.bazel")}
    filters = {
        "go": ("go_files", (), lambda p: p.endswith(".go")),
        "goimports": ("go_files", (golden,), lambda p: p.endswith(".go") and p != golden.removeprefix(":(exclude)")),
        "cc": ("cc_files", (), lambda p: p.endswith((".c", ".cc", ".h"))),
        "docs": ("doc_files", (), lambda p: p.endswith((".md", ".html"))),
        "bazel": ("bazel_files", (), lambda p: PurePosixPath(p).name == "BUILD" or p.endswith(".bzl") or p in ("WORKSPACE", "MODULE.bazel")),
    }
    groups = {}
    expected = {}
    for name, (function, args, keep) in filters.items():
        tracked = set(filter(None, output("bash", "-eu", "-c", selectors + '\n' + function + ' "$@"', "selector", *args).split("\0")))
        exposed = {p for p in paths if keep(p)}
        missing = sorted(tracked - exposed)
        missing_packages = {}
        for p in missing:
            owner = next(str(d) for d in PurePosixPath(p).parents if str(d) in packages)
            missing_packages.setdefault(owner, []).append(p)
        groups[name] = {
            "tracked": len(tracked), "exposed": len(exposed),
            "intersection": len(tracked & exposed), "tracked_sha256": fingerprint(tracked),
            "exposed_sha256": fingerprint(exposed), "missing": missing,
            "extra": sorted(exposed - tracked), "missing_by_owner": missing_packages,
        }
        expected[name] = tracked
    assert expected["go"] - expected["goimports"] == {golden.removeprefix(":(exclude)")}
    data = dict(source=SOURCE, tree=TREE, query=QUERY, bazel=output("bazel", "--version").strip(),
                label_count=len(labels), main_repo_paths=len(paths), owning_packages=len(packages),
                original_script_sha256=hashlib.sha256(lint.encode()).hexdigest(), groups=groups)
    Path(sys.argv[1]).write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
