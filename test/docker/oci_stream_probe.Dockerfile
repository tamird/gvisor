# The maintained Redis context uses this same base. A single incompressible
# 7 GiB layer crosses the partial-blob size retained from the failed export.
FROM redis:7.0.12

RUN dd if=/dev/urandom of=/stream-probe bs=1048576 count=7168 iflag=fullblock status=none && stat -c 'Stream probe bytes: %s' /stream-probe
