
type postureFieldNames struct {
	path   cmp.Path
	fields map[string]bool
}

func (r *postureFieldNames) PushStep(step cmp.PathStep) { r.path = append(r.path, step) }
func (r *postureFieldNames) PopStep()                   { r.path = r.path[:len(r.path)-1] }
func (r *postureFieldNames) Report(d cmp.Result) {
	if d.Equal() {
		return
	}
	var names []string
	for _, step := range r.path {
		switch s := step.(type) {
		case cmp.StructField:
			names = append(names, s.Name())
		case cmp.MapIndex:
			k := s.Key()
			if k.Kind() == reflect.String {
				switch key := k.String(); key {
				case "NOFILE", "MEMLOCK", "NoNewPrivs", "Seccomp", "Seccomp_filters", "Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb":
					names = append(names, key)
				}
			}
		}
	}
	a, b := r.path.Last().Values()
	for _, v := range []reflect.Value{a, b} {
		if v.IsValid() && v.Kind() == reflect.String {
			key, _, found := strings.Cut(v.String(), "=")
			if found {
				switch key {
				case "Seccomp_filters", "NoNewPrivs", "Seccomp", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb":
					names = append(names, key)
				}
			}
		}
	}
	if len(names) == 0 {
		names = append(names, "root")
	}
	r.fields[strings.Join(names, ".")] = true
}

// Only publish fixed class labels. Descriptor targets and process IDs stay
// in the local test log on the ephemeral runner.
func postureFDCategory(class string) string {
	switch class {
	case sandboxposture.FDUnknown:
		return "unknown"
	case sandboxposture.FDDeleted:
		return "deleted"
	case sandboxposture.FDRegular:
		return "regular"
	case sandboxposture.FDDirectory:
		return "directory"
	case sandboxposture.FDFifo:
		return "fifo"
	case sandboxposture.FDSymlink:
		return "symlink"
	case sandboxposture.FDBlockDev:
		return "block"
	case sandboxposture.FDSocket:
		return "socket"
	case sandboxposture.FDNullDev:
		return "null"
	case sandboxposture.FDPipe:
		return "pipe"
	case sandboxposture.FDAnonPrefix + "timerfd":
		return "timerfd"
	case sandboxposture.FDAnonPrefix + "pidfd":
		return "pidfd"
	}
	switch {
	case strings.HasPrefix(class, sandboxposture.FDMemfdPrefix):
		return "other-memfd"
	case strings.HasPrefix(class, sandboxposture.FDAnonPrefix):
		return "other-anon"
	case strings.HasPrefix(class, sandboxposture.FDCharDev):
		return "char"
	}
	return "other"
}
