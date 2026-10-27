"""Make SOURCE_DATE_EPOCH an input to the Debian changelog alone."""

def _source_date_epoch_impl(rctx):
    rctx.file("BUILD.bazel", 'exports_files(["epoch"])\n')
    rctx.file("epoch", rctx.getenv("SOURCE_DATE_EPOCH", "0"), executable = False)

source_date_epoch = repository_rule(implementation = _source_date_epoch_impl)
