package qualification_declared

import (
	"context"
	"math/rand/v2"
	"time"
)

func (*record) saveRandom() rand.PCG                     { return rand.PCG{} }
func (*record) loadRandom(context.Context, rand.PCG)     {}
func (*record) saveInstant() time.Time                 { return time.Time{} }
func (*record) loadInstant(context.Context, time.Time) {}
