load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files", "strip_prefix")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

# The pinned release's executable files. Declare modes because remote action
# input permissions must not determine the packaged source permissions.
_EXECUTABLES = [
    ".github/scripts/setup-slapd.sh",
    "Zend/zend_vm_gen.php",
    "build/config-stubs",
    "build/config.guess",
    "build/config.sub",
    "build/gen_stub.php",
    "build/genif.sh",
    "build/ltmain.sh",
    "build/shtool",
    "buildconf",
    "configure",
    "ext/curl/sync-constants.php",
    "ext/dba/install_cdb.sh",
    "ext/ext_skel.php",
    "ext/fileinfo/create_data_file.php",
    "ext/fileinfo/generate_patch.sh",
    "ext/hash/bench.php",
    "ext/lexbor/patches/update-lexbor.sh",
    "ext/mbstring/gen_rare_cp_bitvec.php",
    "ext/mbstring/libmbfl/mbfl/mk_eaw_tbl.awk",
    "ext/mbstring/ucgendat/ucgendat.php",
    "ext/mbstring/ucgendat/uctest.php",
    "ext/opcache/jit/tls/testing/test.sh",
    "ext/opcache/tests/jit/gh17246.phpt",
    "ext/phar/build_precommand.php",
    "ext/phar/phar/phar.php",
    "ext/session/mod_files.sh",
    "ext/snmp/tests/bigtest",
    "ext/standard/html_tables/html_table_gen.php",
    "ext/tokenizer/tokenizer_data_gen.php",
    "run-extra-tests.php",
    "run-tests.php",
    "sapi/cli/generate_mime_type_map.php",
    "sapi/phpdbg/create-test.php",
    "sapi/phpdbg/phpdbg.init.d",
    "scripts/dev/bless_tests.php",
    "scripts/dev/check_parameters.php",
    "scripts/dev/credits",
    "scripts/dev/find_tested.php",
    "scripts/dev/gen_verify_stub",
    "scripts/dev/genfiles",
    "scripts/dev/makedist",
    "scripts/dev/search_underscores.php",
    "scripts/dev/tidy.php",
    "scripts/gdb/debug_gdb_scripts_gen.php",
    "win32/build/mkico.sh",
    "win32/build/registersyslog.php",
]

# PHPT tests use companion files throughout the source tree. Map paths from
# this external repository's root and preserve the release's executable set.
pkg_files(
    name = "fixture_data",
    srcs = glob(
        ["**"],
        exclude = _EXECUTABLES + [
            "BUILD.bazel",
            "REPO.bazel",
            "WORKSPACE",
            "WORKSPACE.bazel",
        ],
    ),
    attributes = pkg_attributes(
        mode = "0644",
        uid = 0,
        gid = 0,
    ),
    strip_prefix = strip_prefix.from_root(),
)

pkg_files(
    name = "fixture_executables",
    srcs = _EXECUTABLES,
    attributes = pkg_attributes(
        mode = "0755",
        uid = 0,
        gid = 0,
    ),
    strip_prefix = strip_prefix.from_root(),
)

pkg_tar(
    name = "fixtures",
    srcs = [
        ":fixture_data",
        ":fixture_executables",
    ],
    package_dir = "root/php-8.5.11",
    visibility = ["//visibility:public"],
)
