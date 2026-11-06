"""Build one bare-metal guest with the TamaGo SDK and local module proxy."""

import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("go", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fixed", action="store_true")
    args = parser.parse_args()

    source = Path("test/tamago_dispatch")
    cmd = [
        str(args.go.absolute()),
        "build",
        "-mod=readonly",
        "-trimpath",
        "-buildvcs=false",
        "-tags=microvm",
        "-ldflags=-T 0x10010000 -R 0x1000",
        "-o", str(args.output.absolute()),
    ]
    if args.fixed:
        cmd.append("-overlay=fixed-overlay.json")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(
            [*cmd, "."],
            cwd=source / "guest",
            env={
                "HOME": tmp,
                "GOCACHE": str(root / "go-build"),
                "GOMODCACHE": str(root / "gomod"),
                "GOOS": "tamago",
                "GOARCH": "amd64",
                "GOOSPKG": "github.com/usbarmory/tamago",
                "GOEXPERIMENT": "simd",
                "CGO_ENABLED": "0",
                "GOROOT": str(args.go.absolute().parent.parent),
                "GOPROXY": (source / "proxy").absolute().as_uri(),
                "GOSUMDB": "off",
                "GOTOOLCHAIN": "local",
            },
            check=True,
        )
        symbols = subprocess.check_output(
            [args.go.absolute(), "tool", "nm", args.output.absolute()],
            env={"GOROOT": str(args.go.absolute().parent.parent), "HOME": tmp, "GO111MODULE": "off"},
            text=True,
        )
        method = "gvisor.dev/gvisor/pkg/tcpip/transport/tcp.(*processor).drainQueue"
        has_fix = any(line.endswith(" " + method) for line in symbols.splitlines())
        if has_fix != args.fixed:
            raise RuntimeError(f"{args.output} has drainQueue={has_fix}, expected {args.fixed}")


if __name__ == "__main__":
    main()
