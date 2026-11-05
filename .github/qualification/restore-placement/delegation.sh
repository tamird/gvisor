#!/bin/bash
# Run in the existing guest hierarchy, under the ordinary action identity.
set -euo pipefail
export LC_ALL=C
cg=/sys/fs/cgroup
required=(cpu cpuset io memory pids)
phase=identity
trap 'echo "CG_FAILURE_STAGE=$phase"' ERR
limit() { echo "CG_INELIGIBLE=$1"; exit 0; }
echo CG_KERNEL="$(uname -r)"
echo CG_PAGE="$(getconf PAGESIZE)"
[[ $EUID == 65534 && $(id -g) == 65534 && $(uname -m) == x86_64 ]]
awk '/^Cap(Eff|Prm):/ {if ($2 !~ /^0+$/) bad=1; n++} END {exit bad || n!=2}' /proc/self/status
[[ "$(cat /proc/1/comm)" == init || "$(cat /proc/1/comm)" == goinit ]]
cmdline=" $(cat /proc/cmdline) "
for arg in console=ttyS0 reboot=k panic=1 tsc=reliable; do [[ $cmdline == *" $arg "* ]]; done
awk '$5=="/workspace" && / - ext4 \/dev\/vd[bcd] / {ok=1} END {exit !ok}' /proc/self/mountinfo
echo CG_PRIVATE_VM=1
echo CG_ORDINARY_UID="$EUID"
[[ $(getconf PAGESIZE) == 4096 ]] || limit pages
mem=$(awk '$1=="MemTotal:" {print $2}' /proc/meminfo)
echo CG_MEM_KIB="$mem"
(( mem >= 5*1024*1024 )) || limit capacity
[[ $(stat -fc %T "$cg") == cgroup2fs ]] || limit hierarchy
tree=$(awk -v cg="$cg" '$5==cg && / - cgroup2 / {print $4}' /proc/self/mountinfo)
member=$(awk -F: '$1=="0" && $2=="" {print $3}' /proc/self/cgroup)
# Disallow dot traversal, escaped mount components and ambiguous paths. Record
# the actual paths only if bounded to conventional guest cgroup component names.
for path in "$tree" "$member"; do
  [[ $path == /* && ${#path} -le 180 && $path != *..* && $path =~ ^/[a-zA-Z0-9_./:@-]*$ ]]
done
echo CG_MOUNT_ROOT="$tree"
echo CG_MEMBER="$member"
# The authored test takes dirname(/proc/self/cgroup) and constructs siblings
# under the unified mount. A subtree mount with different coordinates isn't
# evidence that the unmodified workload would address those same siblings.
[[ $tree == / ]] || limit subtree_mount
current="$cg${member%/}"
[[ -d $current ]] || limit membership
parent=$(dirname -- "$member")
base="$cg${parent%/}"
echo CG_SIBLING_PARENT="$parent"
phase=permissions
for pair in "member:$current" "parent:$base" "control:$base/cgroup.subtree_control" "root_control:$cg/cgroup.subtree_control" "procs:$base/cgroup.procs"; do
  p=${pair#*:}
  echo CG_OWNER="${pair%%:*}:$(stat -Lc '%u:%g:%a' "$p")"
  if [[ -w $p ]]; then echo CG_WRITABLE="${pair%%:*}"; fi
done
known=" $(cat "$cg/cgroup.controllers") "
here=" $(cat "$base/cgroup.controllers") "
on=" $(cat "$base/cgroup.subtree_control") "
missing=0
for c in "${required[@]}"; do
  if [[ $known == *" $c "* ]]; then echo CG_AVAILABLE="$c"; else missing=1; fi
  if [[ $here == *" $c "* ]]; then echo CG_DELEGATED="$c"; else missing=1; fi
  if [[ $on == *" $c "* ]]; then echo CG_ENABLED="$c"; fi
done
(( !missing )) || limit controllers
[[ -w $base ]] || limit parent_permission
# createCgroupPaths writes every advertised controller at each ancestor,
# including the mount root. Don't modify any existing control to qualify it.
p=$base
while :; do
  [[ -w $p/cgroup.subtree_control ]] || limit ancestor_permission
  on=" $(cat "$p/cgroup.subtree_control") "
  for c in $known; do [[ $on == *" $c "* ]] || limit ancestor_controllers; done
  if [[ -f $p/memory.max ]]; then
    ceiling=$(cat "$p/memory.max")
    [[ $ceiling == max || $ceiling -ge 5368709120 ]] || { echo CG_CEILING="$ceiling"; limit capacity; }
  fi
  [[ $p != "$cg" ]] || break
  p=$(dirname -- "$p")
done
# Modify only fresh siblings, never preexisting cgroups. Move only a child shell.
made=()
trap 'r=0; for p in "${made[@]}"; do rmdir "$p" || r=1; done; echo CG_CLEANUP=$r; (( !r )) || exit 1' EXIT
for want in 2147483648 209715200; do
  phase=mkdir
  leaf="$base/gvisor-eligibility-$BASHPID-$want"
  mkdir "$leaf" || limit mkdir
  made+=("$leaf")
  phase=memory
  echo "$want" > "$leaf/memory.max" || limit memory
  [[ $(cat "$leaf/memory.max") == "$want" ]]
  if [[ -e $leaf/memory.swap.max ]]; then
    echo 0 > "$leaf/memory.swap.max" || limit swap
    [[ $(cat "$leaf/memory.swap.max") == 0 ]]
  fi
  grep -Fxq 'oom_kill 0' "$leaf/memory.events"
  phase=migrate
  (
    echo "$BASHPID" > "$leaf/cgroup.procs" &&
      [[ $(cat /proc/self/cgroup) == "0::${leaf#"$cg"}" ]]
  ) || limit migrate
  echo CG_LIMIT="$want"
done
echo CG_ELIGIBLE=1
