# A graph map entry can miss an interface's restored pointer

In [patch 16](https://github.com/google/gvisor/pull/16584/commits/d09828cec993fd489eccabcff27aa68c024f2080),
an interface holding a one-pointer struct stores that pointer directly in
its data word. A typed load hook may restore the pointer. The graph
decoder copies the updated word into the interface slot after the hook,
but a map entry has already copied the slot with `SetMapIndex`. Updating
the slot does not update the map entry.

The [component fixture](https://github.com/tamird/gvisor/commit/b3509aa986616aaabd86f26d5c0ba8c5c07ebcff)
uses the patch's encoder, value decoder and callback methods. The hook ran
once in each case. Here are the original results:

| Destination | Encoded bytes | Patch 16 | Fork correction |
| --- | --- | --- | --- |
| Interface slot | `01` | restored pointer | restored pointer |
| Map of interface values | `07 01` | nil | restored pointer |
| Map holding a pointer to the value | `07 02 01` | restored pointer, same pointee | restored pointer, same pointee |

Only the copied map entry and its enclosing test failed on the
[unchanged decoder](https://app.buildbuddy.io/invocation/ac7e8a7b-d1aa-43f2-b2a6-afe99bce41fa).
The [fork change](https://github.com/tamird/gvisor/commit/eb5036b67218924be2e8925f578eefe956525c0d)
queues that entry's publication behind the callbacks already registered
for it. It captures the destination map because the decoder reuses its
map pointer for the next object. Plain entries and inspection without
callbacks still publish immediately. The [corrected run](https://app.buildbuddy.io/invocation/495f0216-8fa0-4c27-9ba3-93fd2814599b)
passed all seven named XML rows, including two distinct maps, multiple
values, a direct interface key and inspection without callbacks.

This is a component reproduction. On this tip public `state.Load`
still uses the legacy decoder; the graph's outer callback dispatcher
is not present. The [documented AfterLoad contract](https://github.com/google/gvisor/blob/c6d44d572296f5226ebecd262c22e9f13c1800b4/pkg/state/state.go#L247-L253)
requires a parent hook to run after its declared dependencies. The
[graph object](https://github.com/google/gvisor/blob/d09828cec993fd489eccabcff27aa68c024f2080/pkg/state/graph_decode_unsafe.go#L60-L73)
keeps its own callbacks separate from the
[callbacks and unblocks of waiters](https://github.com/google/gvisor/blob/d09828cec993fd489eccabcff27aa68c024f2080/pkg/state/graph_decode_unsafe.go#L456-L470).
When a head connects those queues to the public loader, a useful next
regression is a parent that waits for this map and reads the restored
pointer in its own hook. Neither this run nor the older integrating
branch demonstrates that parent observation on patch 16.
