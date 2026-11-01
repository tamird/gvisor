"""Select classic SACK on the test DUT; refuse to run with a different value."""
from pathlib import Path

path = Path("test/packetimpact/dut/posix_server.cc")
s = path.read_text()
start = "int main(int argc, char* argv[]) {"
assert s.count(start) == 1
s = s.replace(start, start + '''
  const char* ctl = "/proc/sys/net/ipv4/tcp_recovery";
  int fd = open(ctl, O_WRONLY);
  if (fd < 0 || write(fd, "0", 1) != 1 || close(fd) != 0) {
    perror("disable RACK"); return 1;
  }
  fd = open(ctl, O_RDONLY);
  char value[16] = {};
  if (fd < 0 || read(fd, value, sizeof(value)-1) < 1 || value[0] != '0' || close(fd) != 0) {
    perror("check RACK"); return 1;
  }
''')
path.write_text(s)

p = Path("test/packetimpact/tests/tcp_rack_test.go")
s = p.read_text()
if '"fmt"' not in s:
    s = s.replace('"flag"', '"flag"\n\t"fmt"')
p.write_text(s)
