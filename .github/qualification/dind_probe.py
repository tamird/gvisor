import os


def daemon_argv(binaries, directory, config, isolated, name):
    # The host containerd keeps running. Hide its socket only in this
    # daemon's already private mount namespace, for the managed case.
    launcher = 'ip link set lo up; '
    if isolated:
        launcher += 'mount -t tmpfs -o mode=755 tmpfs /run/containerd; '
    return ["sudo", "-n", "unshare", "--mount", "--net", "--propagation", "private",
            "/bin/sh", "-ec", launcher + 'exec "$@"', "run-daemon",
            "env", "PATH=" + str(binaries) + ":" + os.environ["PATH"], str(binaries / "dockerd"),
            "--config-file", str(config), "--host", "unix://" + str(directory / "docker.sock"),
            "--pidfile", str(directory / "docker.pid"), "--data-root", str(directory / "data"),
            "--exec-root", str(directory / "exec"), "--containerd-namespace", name,
            "--containerd-plugins-namespace", name + "-plugins"]
