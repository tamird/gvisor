#!/bin/bash
set -eu
mode="$1"
binary="$2"
name="$3"
if [[ "$mode" == native ]]; then
  exec unshare --net -- "$binary" "--gtest_filter=$name"
fi
runsc="$4"
guest="$TEST_TMPDIR/guest"
mkdir -p "$guest"/{proc,sys,dev,tmp}
chmod 1777 "$guest/tmp"
cp -L "$binary" "$guest/test"
# Some historical GNU targets link the host loader. Stage exactly the
# installed objects reported for this gtest, if it has any.
while read -r lib; do
  case "$lib" in
    /lib/*|/lib64/*|/usr/lib/*) install -D "$lib" "$guest$lib" ;;
    *) exit 1 ;;
  esac
done < <(ldd "$binary" 2>/dev/null | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// {print $1}')
mkdir "$TEST_TMPDIR/log"
set +e
TEST_ON_GVISOR=systrap GVISOR_RUNTIME=runsc GVISOR_NETWORK=none "$runsc" --root="$TEST_TMPDIR/runsc" --platform=systrap --network=none --ignore-cgroups --debug-log="$TEST_TMPDIR/log/" do --root="$guest" --cwd=/ -- /test "--gtest_filter=$name"
status=$?
set -e
if grep -Fsl -- 'registerChild called on a non-root namespace' "$TEST_TMPDIR"/log/* >/dev/null 2>&1; then
  echo 'Sentry: registerChild called on a non-root namespace'
fi
exit "$status"
