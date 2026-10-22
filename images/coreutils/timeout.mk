# Automake prepares these headers for "all", but not for a direct binary
# target. Reuse that prerequisite list without building unrelated utilities.
.PHONY: bazel-timeout
bazel-timeout: $(BUILT_SOURCES)
	$(MAKE) install-binPROGRAMS bin_PROGRAMS=src/timeout INSTALL_PROGRAM="$(install_sh_PROGRAM)"
