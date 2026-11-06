#include <cstdio>
#include <cstdlib>
#include <sys/utsname.h>

extern int link_hook();

#ifndef CC_CONTRACT_CONFIGURED
#define CC_CONTRACT_CONFIGURED 0
#endif

int main(int argc, char** argv) {
  struct utsname host = {};
  if (argc != 2 || uname(&host) != 0 || link_hook() != 707 ||
      std::atoi(argv[1]) != CC_CONTRACT_CONFIGURED) {
    return 1;
  }
  std::printf("CC_CONTRACT %s %d %d\n", host.machine, link_hook(),
              CC_CONTRACT_CONFIGURED);
  return 0;
}
