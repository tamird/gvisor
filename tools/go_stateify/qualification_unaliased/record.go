package qualification_unaliased

// +stateify savable
type record struct {
	random  int `state:".(rand.PCG)"`
	instant int `state:".(time.Time)"`
}
