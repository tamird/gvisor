package qualification_identtype

import peer "gvisor.dev/gvisor/pkg/abi/linux"

// +stateify savable
// +stateify identtype
type Record peer.BPFInstruction
