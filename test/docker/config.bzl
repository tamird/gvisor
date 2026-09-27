"""Shared configuration for the Docker integration suites."""

# Runtime suffixes used by MakeContainerWithRuntime. Both the private test
# daemon and the installed-runtime adapter consume this same table.
RUNTIME_VARIANTS = {
    "": [],
    "-docker": ["--net-raw", "--allow-packet-socket-write"],
    "-fdlimit": ["--fdlimit=2000"],
    "-dcache": ["--fdlimit=2000", "--dcache=100"],
    "-host-uds": ["--host-uds=all"],
    "-overlay": ["--overlay2=all:self"],
    "-cgroupv2": ["--in-sandbox-cgroup=v2"],
}

# Image names are the existing Docker test inputs, grouped by their consuming
# suite. MODULE.bazel pins the matching tools/images.mk artifacts by digest.
COHORT_IMAGES = {
    "integration": [
        "basic/alpine",
        "basic/filecap",
        "basic/integrationtest",
        "basic/libacl",
        "basic/nginx",
        "basic/python",
        "basic/sudo",
        "basic/tmpfile",
        "basic/ubuntu",
    ],
    "runtime": [
        "basic/alpine",
        "basic/integrationtest",
        "basic/pidfd-tests",
        "basic/ubuntu",
        "systemd-integ",
        "systemd-services",
        "ubi10-init",
    ],
    "nested_runtime": [
        "basic/integrationtest",
    ],
    "image": [
        "basic/alpine",
        "basic/docker",
        "basic/httpd",
        "basic/mysql",
        "basic/nginx",
        "basic/rust",
        "basic/tcpdump",
        "basic/tomcat",
        "image-test/ruby",
    ],
}

# This existing systemd case only runs on AMD64.
AMD64_RUNTIME_IMAGES = ["arch-systemd"]

# The maintained Make Docker lane, shared by installed and owned test suites.
DOCKER_TESTS = [
    "//test/image:image_test",
    "//test/e2e:integration_test",
    "//test/e2e:integration_runtime_test",
    "//test/e2e:runtime_in_docker_test",
]
