package qualification_declared

// +stateify savable
type record struct {
	random  int `state:".(rand.PCG)"`
	instant int `state:".(time.Time)"`
}
