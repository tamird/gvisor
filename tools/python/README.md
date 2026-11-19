# Python type checks

`bazel test //tools/python:typecheck_test` checks the Buildkite reader,
`tools/compat/realpath.py`, and `tools/gen_compile_commands.py` with mypy.
It uses the repository Python 3.11 toolchain, strict checks, and rejects explicit
`Any`. Decoded JSON still needs runtime validation before use.

The maintained `make unit-tests` suite selects this target through `tools/...`.

The source list in `BUILD` defines coverage. Tests and other Python programs are
not yet covered. Add their declared dependencies and typing stubs before adding
them; do not ignore missing imports to make a target pass.

Regenerate `requirements_lock.txt` with the uv command recorded in its header.
The lock includes hashes for the Python 3.11 dependency set across platforms.
