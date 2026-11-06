$(VDSO_SO): vdso/*.cc vdso/*.h vdso/*.lds vdso/*.py
	# sync with vdso/BUILD bazel rules
	$(CC) $(CC_FLAGS) \
		-I. \
		-O2 \
		-std=c++11 \
		-fPIC \
		-fno-sanitize=all \
		-fno-stack-protector \
		-shared \
		-nostdlib \
		-Wl,-soname=linux-vdso.so.1 \
		-Wl,--hash-style=sysv \
		-Wl,--no-undefined \
		-Wl,-Bsymbolic \
		-Wl,-z,max-page-size=4096 \
		-Wl,-z,common-page-size=4096 \
		-Wl,-Tvdso/vdso_$(DEB_HOST_ARCH).lds \
		-o $@ \
		vdso/vdso.cc vdso/vdso_time.cc
	python3 ./vdso/check_vdso.py --check-data --vdso=$@
