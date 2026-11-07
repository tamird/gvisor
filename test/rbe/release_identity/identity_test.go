package releaseidentity_test

import (
	"debug/elf"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
)

const label = "release-20261106.0"

func runCLI(path, want string, args ...string) (string, error) {
	cmd := exec.Command(path, args...)
	cmd.Env = append(os.Environ(), "GVISOR_ENFORCE_RELEASE="+want)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func TestInstalledIdentity(t *testing.T) {
	arch := os.Getenv("RELEASE_TEST_ARCH")
	var uts syscall.Utsname
	if err := syscall.Uname(&uts); err != nil {
		t.Fatal(err)
	}
	var machine []byte
	for _, c := range uts.Machine {
		if c == 0 {
			break
		}
		machine = append(machine, byte(c))
	}
	if arch != runtime.GOARCH || string(machine) != map[string]string{"amd64": "x86_64", "arm64": "aarch64"}[arch] {
		t.Fatalf("requested %s; worker reports %s and test binary is %s", arch, machine, runtime.GOARCH)
	}
	cpuinfo, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		t.Fatal(err)
	}
	cpuField := map[string]string{"amd64": "vendor_id", "arm64": "CPU implementer"}[arch]
	if !strings.Contains(string(cpuinfo), cpuField) {
		t.Fatalf("worker cpuinfo has no %q field", cpuField)
	}
	variant := os.Getenv("RELEASE_TEST_VARIANT")
	if variant != "fixed" && variant != "stock" {
		t.Fatalf("unknown variant %q", variant)
	}
	base := filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), "release")
	versions := make(map[string]string)
	for _, name := range []string{"runsc", "gvisor-bin/runsc-netgofer"} {
		path := filepath.Join(base, name)
		f, err := elf.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		if got, want := f.Machine, map[string]elf.Machine{"amd64": elf.EM_X86_64, "arm64": elf.EM_AARCH64}[arch]; got != want {
			t.Fatalf("%s ELF is %s, want %s", name, got, want)
		}
		f.Close()
		out, err := runCLI(path, label, "--version")
		if err != nil {
			t.Fatalf("%s --version: %v: %s", name, err, out)
		}
		line, _, _ := strings.Cut(out, "\n")
		v, ok := strings.CutPrefix(line, "runsc version ")
		if !ok || v == "" {
			t.Fatalf("%s returned an invalid version: %q", name, line)
		}
		versions[name] = v
	}
	ng := "gvisor-bin/runsc-netgofer"
	if versions["runsc"] != label || (versions[ng] == label) != (variant == "fixed") {
		t.Fatalf("%s versions: %v", variant, versions)
	}
	for _, expected := range []string{label, "release-other"} {
		out, err := runCLI(filepath.Join(base, ng), expected, "--platform=systrap", "flags")
		success := variant == "fixed" && expected == label
		if success && err != nil || !success && (err == nil || !strings.Contains(out, "does not match the expected release") || !strings.Contains(out, expected)) {
			t.Fatalf("%s netgofer flags with %s: %v: %.900s", variant, expected, err, out)
		}
	}
	result, err := json.Marshal(map[string]any{
		"arch": arch, "worker": string(machine), "cpuInfoField": cpuField, "variant": variant, "runsc": versions["runsc"],
		"netgofer": versions[ng], "acceptsPackagedRelease": variant == "fixed", "rejectsOtherRelease": true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("RELEASE_IDENTITY %s", result)
}
