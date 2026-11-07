#include <stdio.h>
#include <sys/utsname.h>

int main(void) {
  struct utsname kernel;
  if (uname(&kernel) != 0) {
    return 1;
  }
  printf("build_test native machine=%s\n", kernel.machine);
  return 0;
}
