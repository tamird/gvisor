package qualification_versioned

// +stateify savable
type record struct {
	random int `state:".(rand.PCG)"`
	address int `state:".(peer.Addr)"`
}
