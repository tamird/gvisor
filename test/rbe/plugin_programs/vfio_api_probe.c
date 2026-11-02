/* Qualification only: the selected libc must expose DPDK's existing API. */
#include <unistd.h>

#ifndef _LARGEFILE64_SOURCE
#error "DPDK's compiler invocation lost _LARGEFILE64_SOURCE"
#endif

_Static_assert(sizeof(off_t) == 8, "DPDK's native x86_64 file offset");
_Static_assert(sizeof(&pread64) > 0, "pread64 must be declared");
_Static_assert(sizeof(&pwrite64) > 0, "pwrite64 must be declared");
