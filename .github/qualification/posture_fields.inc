
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
