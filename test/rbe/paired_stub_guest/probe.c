#include <stdio.h>
#include <sys/auxv.h>
#include <sys/utsname.h>
#include <unistd.h>

int main(void) {
  struct utsname u;
  if (uname(&u) != 0) return 1;
  printf("SYSTRAP_PROBE arch=%s auxv=%lu sysconf=%ld pid=%d\n", u.machine,
         getauxval(AT_PAGESZ), sysconf(_SC_PAGESIZE), getpid());
  return 0;
}
