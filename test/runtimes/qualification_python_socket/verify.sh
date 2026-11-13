#!/usr/bin/env bash
set -euo pipefail
r="$PYTHON_SOCKET_RECEIPT"
q="$TEST_SRCDIR/$TEST_WORKSPACE/test/runtimes/qualification_python_socket"
for runtime in runc signal-ci; do
  for fixture in stock candidate; do
    args=()
    if [[ "$fixture" = stock ]]; then
      args+=(--volume "$r/test_socket.py:/root/cpython-3.12.3/Lib/test/test_socket.py:ro")
      hash=329cfccf19e7dab4f3e193747720ba131af014870fbac504d53ad666ae22b9e9
    else
      hash=40386aca91876da1df14ad66b8f2d750400df0c6eaf56aceb9a7e0abe5aa254a
    fi
    docker run --rm -i --network none --runtime="$runtime" -e FIXTURE="$fixture" -e FIXTURE_HASH="$hash" "${args[@]}" python-socket-check ./python -B -X pycache_prefix=/tmp/qualification-pyc - < "$q/cases.py" 2>&1 | tee "$r/$runtime-$fixture.log"
  done
done
