// This program is a separate action. The production C compilation and link
// do not get it (or any other extra tool) as an input.
#include <asm/unistd.h>
#include <linux/errno.h>
#include <linux/fcntl.h>
#include <linux/utsname.h>

#if defined(__x86_64__)
#define MACHINE "x86_64"
#define ELF_MACHINE 62
__asm__(".text\n.global _start\n_start:\nmov %rsp,%rdi\ncall observe\nud2");
static long sys4(long n, long a, long b, long c, long d) {
  register long arg4 __asm__("r10") = d;
  __asm__ volatile("syscall" : "+a"(n) : "D"(a), "S"(b), "d"(c), "r"(arg4)
                   : "rcx", "r11", "memory");
  return n;
}
#elif defined(__aarch64__)
#define MACHINE "aarch64"
#define ELF_MACHINE 183
__asm__(".text\n.global _start\n_start:\nmov x0,sp\nbl observe\nbrk #0");
static long sys4(long n, long a, long b, long c, long d) {
  register long x8 __asm__("x8") = n, x0 __asm__("x0") = a;
  register long x1 __asm__("x1") = b, x2 __asm__("x2") = c, x3 __asm__("x3") = d;
  __asm__ volatile("svc 0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory");
  return x0;
}
#else
#error Unsupported worker
#endif

static long open_file(const char* path, long flags) {
  return sys4(__NR_openat, AT_FDCWD, (long)path, flags, 0644);
}

static int check(long* stack) {
  if (stack[0] != 3) return 10;
  struct new_utsname info;
  if (sys4(__NR_uname, (long)&info, 0, 0, 0)) return 11;
  for (unsigned i = 0; i < sizeof(MACHINE); i++) {
    if (info.machine[i] != MACHINE[i]) return 12;
  }
  // Distinguish this root from the ordinary Ubuntu worker, with no shell
  // available to the observation and no dependency on utilities or libc.
  if (open_file("/bin/sh", O_RDONLY) != -ENOENT) return 13;
  if (open_file("/usr/bin/env", O_RDONLY) != -ENOENT) return 14;
  if (open_file("/etc/apk/world", O_RDONLY) < 0) return 15;

  unsigned char header[20];
  long input = open_file((const char*)stack[3], O_RDONLY);
  if (input < 0 || sys4(__NR_read, input, (long)header, sizeof(header), 0) != sizeof(header)) return 16;
  if (header[0] != 0x7f || header[1] != 'E' || header[2] != 'L' ||
      header[3] != 'F' || header[4] != 2 || header[5] != 1 ||
      header[18] != ELF_MACHINE || header[19] != 0) return 17;
  long output = open_file((const char*)stack[2], O_CREAT | O_WRONLY | O_TRUNC);
  if (output < 0) return 18;
  return sys4(__NR_write, output, (long)info.machine, sizeof(MACHINE) - 1, 0) == sizeof(MACHINE) - 1 ? 0 : 19;
}

__attribute__((used, noreturn)) void observe(long* stack) {
  sys4(__NR_exit_group, check(stack), 0, 0, 0);
  __builtin_unreachable();
}
