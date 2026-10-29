"""Time first verified restore from child launch, over 16 balanced pairs."""

import subprocess
import sys
import time


def main() -> None:
    stock, candidate, *args = sys.argv[1:]
    executables = {"stock": stock, "candidate": candidate}
    for pair in range(1, 17):
        order = ("stock", "candidate") if pair % 2 else ("candidate", "stock")
        for position, arm in enumerate(order, 1):
            print(f"slot-child pair={pair} position={position} arm={arm}", flush=True)
            start = time.monotonic_ns()
            with subprocess.Popen(
                [executables[arm], *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ) as child:
                assert child.stdout is not None
                completions = []
                for line in child.stdout:
                    if line.startswith("phase-restore "):
                        completions.append(time.monotonic_ns() - start)
                    print(line, end="", flush=True)
                if child.wait() or len(completions) != 2:
                    raise RuntimeError(f"{arm} pid={child.pid}: {len(completions)} restores")
                print(f"slot-complete pair={pair} arm={arm} pid={child.pid} "
                      f"first_verified_ns={completions[0]} both_verified_ns={completions[1]}", flush=True)


if __name__ == "__main__":
    main()
