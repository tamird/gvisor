load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load("//tools:defs.bzl", "go_library")
load(":defs.bzl", "compatibility_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "compatibility",
    testonly = True,
    srcs = ["compatibility.go"],
    importpath = "gvisor.dev/gvisor/test/compatibility",
)

compatibility_test(
    name = "gitea",
    srcs = ["gitea/gitea_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "nextcloud",
    srcs = ["nextcloud/nextcloud_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "wordpress",
    srcs = ["wordpress/wordpress_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "mariadb",
    srcs = ["mariadb/mariadb_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "postgresql",
    srcs = ["postgresql/postgresql_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "redis",
    srcs = ["redis/redis_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "valkey",
    srcs = ["valkey/valkey_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "kafka",
    srcs = ["kafka/kafka_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "mosquitto",
    srcs = ["mosquitto/mosquitto_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "rabbitmq",
    srcs = ["rabbitmq/rabbitmq_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

compatibility_test(
    name = "keycloak",
    srcs = ["keycloak/keycloak_test.go"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/compatibility",
    ],
)

docker_daemon_config(
    name = "qualification_mariadb_native_config",
    testonly = True,
    images = ["//images/compatibility:qualification_mariadb_archive"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "qualification_mariadb_native_test",
    size = "large",
    command = "//test/rbe/mariadb:consumer",
    command_args = [
        "$(rootpath :mariadb_native)",
        "--runtime=runc",
        "-test.run=^TestMariaDB$",
        "-test.v",
    ],
    data = [":mariadb_native"],
    docker_config = ":qualification_mariadb_native_config",
    exec_properties = docker_test_exec_properties(free_disk = "30GB", memory = "8GB"),
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

docker_daemon_config(
    name = "qualification_mariadb_runsc_config",
    testonly = True,
    images = ["//images/compatibility:qualification_mariadb_archive"],
    runtime = "//:release",
    tags = ["manual"],
)

docker_command_test(
    name = "qualification_mariadb_runsc_test",
    size = "large",
    command = "//test/rbe/mariadb:consumer",
    command_args = [
        "$(rootpath :mariadb_runsc)",
        "--runtime=runsc",
        "-test.run=^TestMariaDB$",
        "-test.v",
    ],
    data = [":mariadb_runsc"],
    docker_config = ":qualification_mariadb_runsc_config",
    exec_properties = docker_test_exec_properties(free_disk = "30GB", memory = "8GB"),
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)
