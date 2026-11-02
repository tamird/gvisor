#include <stdint.h>
#include <stdio.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

enum { batch = 64, rounds = 512 };

static int measure(const char *name, int family, int type) {
  int fds[batch];
  uint64_t elapsed = 0;
  for (int round = -1; round < rounds; ++round) {
    for (int i = 0; i < batch; ++i) {
      fds[i] = socket(family, type, 0);
      if (fds[i] < 0) {
        perror("socket");
        return 1;
      }
    }
    struct timespec start, end;
    if (clock_gettime(CLOCK_MONOTONIC, &start) < 0) return 1;
    for (int i = 0; i < batch; ++i) {
      if (close(fds[i]) < 0) {
        perror("close");
        return 1;
      }
    }
    if (clock_gettime(CLOCK_MONOTONIC, &end) < 0) return 1;
    if (round >= 0) {
      elapsed += (end.tv_sec - start.tv_sec) * 1000000000LL +
                 end.tv_nsec - start.tv_nsec;
    }
  }
  printf("BenchmarkClose/%s %d %.3f ns/op\n", name, batch * rounds,
         (double)elapsed / (batch * rounds));
  return 0;
}

int main(void) {
  return measure("tcp", AF_INET, SOCK_STREAM) ||
         measure("udp", AF_INET, SOCK_DGRAM) ||
         measure("unix", AF_UNIX, SOCK_STREAM);
}
