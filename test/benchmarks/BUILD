load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = [
        "//:sandbox",
    ],
    deps = ["//test/docker:defs_bzl"],
)

# Full continuous selections; the remote driver excludes only unqualified KVM.
# Individual benchmark cases retain the pipeline's filters and iteration counts.
test_suite(
    name = "continuous_tests",
    tags = ["manual"],
    tests = [
        "//runsc/container:lifecycle_benchmark_kvm_test",
        "//runsc/container:lifecycle_benchmark_systrap_test",
        "//shim/tests:lifecycle_benchmark_kvm_test",
        "//shim/tests:lifecycle_benchmark_systrap_test",
        "//test/benchmarks/base:hackbench_test_continuous_owned",
        "//test/benchmarks/base:startup_test_continuous_owned",
        "//test/benchmarks/base:sysbench_test_continuous_owned",
        "//test/benchmarks/base:syscallbench_test_continuous_owned",
        "//test/benchmarks/base:usage_test_continuous_owned",
        "//test/benchmarks/database:redis_test_continuous_owned",
        "//test/benchmarks/fs:bazel_test_continuous_owned",
        "//test/benchmarks/fs:fio_test_continuous_owned",
        "//test/benchmarks/fs:rubydev_test_continuous_owned",
        "//test/benchmarks/media:ffmpeg_test_continuous_owned",
        "//test/benchmarks/ml:tensorflow_test_continuous_owned",
        "//test/benchmarks/network:httpd_test_continuous_owned",
        "//test/benchmarks/network:iperf_test_continuous_owned",
        "//test/benchmarks/network:nginx_test_continuous_owned",
        "//test/benchmarks/network:node_test_continuous_owned",
        "//test/benchmarks/network:ruby_test_continuous_owned",
    ],
    visibility = ["//:sandbox"],
)
