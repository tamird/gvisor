// This qualification-only helper holds the same checkpoint tar that dockerd
// uploads under an independent containerd lease while Docker restores it.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"sort"
	"syscall"
	"time"

	containerd "github.com/containerd/containerd"
	"github.com/containerd/containerd/archive"
	"github.com/containerd/containerd/content"
	"github.com/containerd/containerd/errdefs"
	"github.com/containerd/containerd/images"
	"github.com/containerd/containerd/leases"
	"github.com/containerd/containerd/namespaces"
	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

func run() (retErr error) {
	address := flag.String("address", "", "Exact running containerd socket")
	directory := flag.String("checkpoint-dir", "", "Checkpoint directory archived by Docker")
	flag.Parse()
	if flag.NArg() != 0 || !filepath.IsAbs(*address) || !filepath.IsAbs(*directory) {
		return errors.New("absolute address and checkpoint-dir are required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 120*time.Second)
	defer cancel()
	ctx = namespaces.WithNamespace(ctx, "moby")
	client, err := containerd.New(*address)
	if err != nil {
		return err
	}
	defer client.Close()
	ctx, release, err := client.WithLease(ctx)
	if err != nil {
		return err
	}
	lease, ok := leases.FromContext(ctx)
	defer func() {
		cleanup, cancel := context.WithTimeout(namespaces.WithNamespace(context.Background(), "moby"), 45*time.Second)
		defer cancel()
		if err := release(cleanup); err != nil {
			retErr = fmt.Errorf("release owned lease: %v (prior error: %v)", err, retErr)
			return
		}
		remaining, err := client.LeasesService().List(cleanup)
		if err != nil {
			retErr = fmt.Errorf("list leases after cleanup: %v (prior error: %v)", err, retErr)
			return
		}
		for _, value := range remaining {
			if value.ID == lease {
				retErr = fmt.Errorf("owned lease %q survived cleanup", lease)
				return
			}
		}
		if err := json.NewEncoder(os.Stdout).Encode(map[string]string{"event": "released", "lease": lease}); err != nil {
			retErr = fmt.Errorf("report lease cleanup: %v (prior error: %v)", err, retErr)
		}
	}()
	if !ok {
		return errors.New("containerd omitted the owned lease")
	}
	stream := archive.Diff(ctx, "", *directory)
	defer stream.Close()
	writer, err := client.ContentStore().Writer(ctx, content.WithRef("checkpoint-qualification-"+lease))
	if err != nil {
		return err
	}
	defer writer.Close()
	size, err := io.Copy(writer, stream)
	if err != nil {
		return err
	}
	if err := writer.Commit(ctx, 0, ""); err != nil && !errdefs.IsAlreadyExists(err) {
		return err
	}
	descriptor := ocispec.Descriptor{Digest: writer.Digest(), Size: size, MediaType: images.MediaTypeContainerd1Checkpoint}
	leaseIDs := func() ([]string, error) {
		values, err := client.LeasesService().List(ctx)
		if err != nil {
			return nil, err
		}
		ids := make([]string, 0, len(values))
		for _, value := range values {
			ids = append(ids, value.ID)
		}
		sort.Strings(ids)
		return ids, nil
	}
	initial, err := leaseIDs()
	if err != nil {
		return err
	}
	output := json.NewEncoder(os.Stdout)
	verify := func(event string) error {
		body, err := content.ReadBlob(ctx, client.ContentStore(), descriptor)
		if err != nil {
			return err
		}
		if digest.FromBytes(body) != descriptor.Digest || int64(len(body)) != size {
			return errors.New("held checkpoint bytes changed")
		}
		ids, err := leaseIDs()
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(ids, initial) {
			return fmt.Errorf("lease IDs after restore = %v, want %v", ids, initial)
		}
		resources, err := client.LeasesService().ListResources(ctx, leases.Lease{ID: lease})
		if err != nil {
			return err
		}
		found := false
		for _, resource := range resources {
			found = found || resource.Type == "content" && resource.ID == descriptor.Digest.String()
		}
		if !found {
			return errors.New("owned lease does not retain checkpoint content")
		}
		return output.Encode(map[string]interface{}{"event": event, "lease": lease, "digest": descriptor.Digest, "bytes": size, "leases": ids})
	}
	if err := verify("ready"); err != nil {
		return err
	}
	commands := make(chan string)
	go func() {
		defer close(commands)
		scanner := bufio.NewScanner(os.Stdin)
		for scanner.Scan() {
			select {
			case commands <- scanner.Text():
			case <-ctx.Done():
				return
			}
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case command, ok := <-commands:
			if !ok {
				return errors.New("controller closed input without release")
			}
			switch command {
			case "verify":
				if err := verify("verified"); err != nil {
					return err
				}
			case "release":
				return nil
			default:
				return fmt.Errorf("unexpected command %q", command)
			}
		}
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
