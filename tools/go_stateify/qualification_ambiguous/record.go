package qualification_ambiguous

// +stateify savable
type record struct { field int `state:".(peer.Value)"` }
