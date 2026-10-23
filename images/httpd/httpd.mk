# Reuse upstream recursion and link rules without building unused utilities.
.PHONY: bazel-httpd
bazel-httpd:
	@for directory in srclib os server modules; do \
	    $(MAKE) -C $$directory all || exit $$?; \
	done
	$(MAKE) local-all
