from pathlib import Path
path = Path("runsc/gofer/fs/lisafs.go")
source = path.read_text()
anchor = "hostFDToDonate := -1\n"
assert source.count(anchor) == 1
source = source.replace(anchor, anchor + """	if ftype == unix.S_IFCHR && log.IsLogging(log.Debug) {
		log.Warningf("TEST_ONLY omitted host device donor")
		return fd.newOpenFDLisa(openHostFD, flags).FD(), -1, nil
	}
""")
path.write_text(source)
