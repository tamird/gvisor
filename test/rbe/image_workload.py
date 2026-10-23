"""Run the owning bootstrap diagnostic regressions through pinned Python."""
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    result = invoke(
        "bootstrap-diagnostics",
        ["run", "--lockfile_mode=error", "--noannounce_rc",
         "@rules_python//python/bin:python", "--", "-c",
         'import os, unittest; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); '
         'unittest.main(module=None, argv=["unittest", "discover", "-s", "test/rbe", '
         '"-p", "image_bootstrap_test.py", "-v"])' ],
        "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Bootstrap diagnostic regressions failed")
