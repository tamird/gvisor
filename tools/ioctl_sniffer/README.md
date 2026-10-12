# Ioctl Sniffer

This tool provides a way to profile GPU workloads, sniff out all the Nvidia
`ioctl(2)` calls that are involved, and ultimately filter out the calls that are
currently unsupported by nvproxy.

This is accomplished by providing a `libioctl_hook.so` shared library, which can
be `LD_PRELOAD`ed and intercepts all `ioctl(2)` calls made. Any calls made to
known Nvidia device files are then captured and parsed by the `sniffer` Go
package. The sniffer compares against nvproxy's list of supported `ioctl(2)`
numbers for the current driver version, and checks if the given call is
contained in the list. For `NV_ESC_RM_CONTROL` and `NV_ESC_RM_ALLOC` calls, it
also extracts the control command and allocation class respectively, and checks
if nvproxy supports them.

Argument capture uses `process_vm_readv(2)` on the calling thread, which the
application's seccomp policy must permit. Failed or partial copies are reported
as incomplete capture; they do not replace the intercepted ioctl's return value
or `errno`. Readable arguments from failed ioctls are still classified. Capture
and decoding errors make the sniffer return a nonzero exit status independently
of compatibility enforcement.

## Usage

To start, we need to build the shared library and Go binary:

```
make copy TARGETS=//tools/ioctl_sniffer:run_sniffer DESTINATION=bin/
```

Once we have the binary, we can hook into any GPU workload by passing the
corresponding command to run it to `run_sniffer`, like so:

```
bin/run_sniffer nvidia-smi
```

This should run the workload as normal and provide an output of all the
unsupported `ioctl(2)` calls it detected at the end. For example:

```
============== Unsupported ioctls ==============
Frontend: None
UVM: None
Control:
    Control ioctl: request=0xc020462a [nr=NV_ESC_RM_CONTROL, cmd=0x20810110] => ret=0
    Control ioctl: request=0xc020462a [nr=NV_ESC_RM_CONTROL, cmd=0x2080014b] => ret=0
    ...
Alloc:
    Alloc ioctl: request=0xc030462b [nr=NV_ESC_RM_ALLOC, hClass=0xc640] => ret=0
    Alloc ioctl: request=0xc030462b [nr=NV_ESC_RM_ALLOC, hClass=0x73] => ret=0
    ...
Unknown: None
```
