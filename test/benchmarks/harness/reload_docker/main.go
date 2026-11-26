// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Binary reload_docker verifies and reloads a guest-local Docker daemon.
package main

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"golang.org/x/sys/unix"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	if len(os.Args) != 5 {
		return fmt.Errorf("usage: %s check|reload unix://socket config expected-identity", os.Args[0])
	}
	mode, endpoint, config, expected := os.Args[1], os.Args[2], os.Args[3], os.Args[4]
	if !slices.Contains([]string{"check", "reload"}, mode) || !strings.HasPrefix(endpoint, "unix://") || (mode == "reload" && expected == "") {
		return fmt.Errorf("invalid operation or guest-local daemon identity")
	}
	socket, err := unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(socket)
	if err := unix.Connect(socket, &unix.SockaddrUnix{Name: strings.TrimPrefix(endpoint, "unix://")}); err != nil {
		return fmt.Errorf("connect Docker socket: %w", err)
	}
	peer, err := unix.GetsockoptUcred(socket, unix.SOL_SOCKET, unix.SO_PEERCRED)
	if err != nil {
		return err
	}
	if peer.Uid != 0 || peer.Pid <= 1 {
		return fmt.Errorf("expected root daemon, got %+v", peer)
	}
	pidfd, err := unix.PidfdOpen(int(peer.Pid), 0)
	if err != nil {
		return fmt.Errorf("open daemon pidfd: %w", err)
	}
	defer unix.Close(pidfd)
	process := fmt.Sprintf("/proc/%d", peer.Pid)
	executable, err := filepath.EvalSymlinks(process + "/exe")
	if err != nil {
		return err
	}
	peerNamespace, err := os.Stat(process + "/ns/pid")
	if err != nil {
		return err
	}
	guestNamespace, err := os.Stat("/proc/self/ns/pid")
	if err != nil {
		return err
	}
	if filepath.Base(executable) != "dockerd" || !os.SameFile(peerNamespace, guestNamespace) {
		return fmt.Errorf("socket peer is not a guest-local dockerd")
	}
	command, err := os.ReadFile(process + "/cmdline")
	if err != nil {
		return err
	}
	args := strings.Split(strings.TrimSuffix(string(command), "\x00"), "\x00")
	var configs []string
	for i, arg := range args {
		if value, found := strings.CutPrefix(arg, "--config-file="); found {
			configs = append(configs, value)
		} else if arg == "--config-file" && i+1 < len(args) {
			configs = append(configs, args[i+1])
		}
	}
	if len(configs) == 0 {
		configs = []string{"/etc/docker/daemon.json"}
	}
	if !slices.Equal(configs, []string{config}) {
		return fmt.Errorf("daemon configuration %q differs from %q", configs, config)
	}
	stat, err := os.ReadFile(process + "/stat")
	if err != nil {
		return err
	}
	end := strings.LastIndexByte(string(stat), ')')
	values := strings.Fields(string(stat)[end+1:])
	if end == -1 || len(values) < 20 {
		return fmt.Errorf("invalid daemon process stat")
	}
	identity := fmt.Sprintf("%d:%s", peer.Pid, values[19])
	if expected != "" && identity != expected {
		return fmt.Errorf("daemon identity %q differs from %q", identity, expected)
	}
	if mode == "check" {
		fmt.Println(identity)
		return nil
	}
	return unix.PidfdSendSignal(pidfd, unix.SIGHUP, nil, 0)
}
