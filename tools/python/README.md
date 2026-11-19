# Python type checks

The three independent Bazel targets use the pinned mypy, Pyright, and ty tools
and their strictest supported diagnostics. Their dependency group and checker
configuration are in `pyproject.toml`. The repository Python runtime remains 3.11.

```sh
bazel test --keep_going //tools/python:mypy_test //tools/python:pyright_test //tools/python:ty_test
```

The maintained `make unit-tests` suite selects these targets through `tools/...`.
The current integration preflight checks `tools/compat/realpath.py`; the reader,
compile-command generator, tests, and other Python programs are not yet covered.

Regenerate the Bazel dependency lock from the authoritative dependency group:

```sh
uv pip compile --group typecheck --python-version 3.11 --universal --generate-hashes --no-strip-extras --no-strip-markers --only-binary :all: -o tools/python/requirements_lock.txt
```

The generated lock includes distribution hashes. No requirements input file or
runtime package installation is needed. Pyright uses its declared Node wheel;
ty uses the native executable from its declared wheel. Mypy and Pyright emit their
native JSON diagnostics; ty emits native SARIF JSON. Each exits independently.
