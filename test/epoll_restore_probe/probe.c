// Qualifies epoll on the same open host character device across a checkpoint.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/epoll.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static void check(int ok, const char *operation) {
  if (!ok) {
    perror(operation);
    exit(2);
  }
}

static void emit(const char *dir, const char *phase, const char *evidence) {
  char path[4096];
  check(snprintf(path, sizeof(path), "%s/%s", dir, phase) < sizeof(path), "path");
  int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0666);
  check(fd >= 0, "open evidence");
  check(dprintf(fd, "%s\n", evidence) > 0, "write evidence");
  check(close(fd) == 0, "close evidence");
}

// For a syscall, return zero or its positive errno.
static int ctl(int ep, int op, int fd, int tag) {
  struct epoll_event ev = {.events = EPOLLIN, .data.u64 = tag};
  return epoll_ctl(ep, op, fd, &ev) < 0 ? errno : 0;
}

static int ready(int ep, int *mask, int *tag) {
  struct epoll_event ev = {0};
  int n = epoll_wait(ep, &ev, 1, 0);
  *mask = ev.events;
  *tag = ev.data.u64;
  return n;
}

int main(int argc, char **argv) {
  check(argc == 3, "probe device result-dir");
  sigset_t signals;
  sigemptyset(&signals);
  sigaddset(&signals, SIGUSR1);
  check(sigprocmask(SIG_BLOCK, &signals, NULL) == 0, "sigprocmask");
  int fd = open(argv[1], O_RDONLY | O_NONBLOCK);
  struct stat st;
  check(fd >= 0 && fstat(fd, &st) == 0, "open/fstat");
  check(S_ISCHR(st.st_mode) && major(st.st_rdev) == 1 && minor(st.st_rdev) == 8,
        "not host random");
  int ep = epoll_create1(0);
  check(ep >= 0 && !ctl(ep, EPOLL_CTL_ADD, fd, 11), "initial ADD");
  int mask, tag;
  check(ready(ep, &mask, &tag) == 1 && mask == EPOLLIN && tag == 11,
        "initial IN with original tag");
  emit(argv[2], "before", "rdev=1:8 add=0 mask=1 tag=11");
  int signo;
  check(sigwait(&signals, &signo) == 0 && signo == SIGUSR1, "sigwait");
  int live_mask, live_tag, mod_mask, mod_tag, fresh_mask, fresh_tag;
  int live = ready(ep, &live_mask, &live_tag);
  int mod = ctl(ep, EPOLL_CTL_MOD, fd, 22);
  int mod_n = ready(ep, &mod_mask, &mod_tag);
  int del = ctl(ep, EPOLL_CTL_DEL, fd, 0);
  int fresh = epoll_create1(0);
  check(fresh >= 0, "fresh epoll");
  int add = ctl(fresh, EPOLL_CTL_ADD, fd, 33);
  int fresh_n = ready(fresh, &fresh_mask, &fresh_tag);
  char result[512];
  snprintf(result, sizeof(result),
           "live=%d,%d,%d mod=%d,%d,%d,%d del=%d fresh=%d,%d,%d,%d",
           live, live_mask, live_tag, mod, mod_n, mod_mask, mod_tag, del,
           add, fresh_n, fresh_mask, fresh_tag);
  emit(argv[2], "after", result);
  return 0;
}
