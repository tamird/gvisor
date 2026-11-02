# Both packages are pinned in tools/licensecheck/dependencies.yaml.
_SHA256 = {
    "amd64": "944b2728f53ceb3916cec2c962873c9951e612408099601751db2a0a5d81e0ed",
    "arm64": "d96535e0402c011e0ee43449799df2f4504d44b842e4f2b3a6cbc845508eaafc",
}

def _archive_impl(ctx):
    ctx.download_and_extract(
        url = (["https://archive.ubuntu.com/ubuntu/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_amd64.deb"] if ctx.attr.cpu == "amd64" else []) + [
            "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_{}.deb".format(ctx.attr.cpu),
        ],
        sha256 = _SHA256[ctx.attr.cpu],
        type = "deb",
    )
    ctx.extract("data.tar.zst", output = "payload")
    ctx.file("BUILD.bazel", 'exports_files(["payload/usr/bin/busybox"])\n')

busybox_repository = repository_rule(
    implementation = _archive_impl,
    attrs = {"cpu": attr.string(mandatory = True, values = ["amd64", "arm64"])},
)
