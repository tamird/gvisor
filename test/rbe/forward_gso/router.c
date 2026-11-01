#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
  int fd = open("/proc/sys/net/ipv4/ip_forward", O_WRONLY);
  if (fd < 0 || write(fd, "1\n", 2) != 2 || close(fd) < 0) {
    perror("enable IPv4 forwarding");
    return 1;
  }
  fd=open("/proc/sys/net/ipv6/conf/all/forwarding",O_WRONLY);
  if (fd<0 || write(fd,"1\n",2)!=2 || close(fd)<0) { perror("enable IPv6 forwarding"); return 1; }
  puts("router ready");
  fflush(stdout);
  for (;;) pause();
}
