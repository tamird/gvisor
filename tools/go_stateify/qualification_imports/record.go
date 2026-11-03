package qualification_imports

// +stateify savable
type record struct {
	random  int `state:".(v2.PCG)"`
	instant int `state:".(time.Time)"`
}
