# Automake prepares these headers for "all", but not for a direct binary
# target. Reuse that prerequisite list without building unrelated utilities.
.PHONY: bazel-programs
bazel-programs: $(BUILT_SOURCES)
	$(MAKE) install-binPROGRAMS INSTALL_PROGRAM="$(install_sh_PROGRAM)"
