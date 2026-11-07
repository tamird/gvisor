#!/bin/bash
set -eu
test "$(/bin/uname -m)" = x86_64
test "$(/usr/bin/id -u)" = 0
/bin/grep -Eq '^vendor_id[[:space:]]*:' /proc/cpuinfo
while read -r field amount _; do
  case "$field" in
    MemTotal:) total="$amount" ;;
    MemAvailable:) available="$amount" ;;
    SwapTotal:) swap="$amount" ;;
  esac
done < /proc/meminfo
read -r threads < /proc/sys/kernel/threads-max
printf 'SYSCALL_CAPACITY size=%s total_kib=%s available_kib=%s swap_kib=%s threads_max=%s cpu=x86_64 uid=0\n' "$TEST_SIZE" "$total" "$available" "$swap" "$threads"
