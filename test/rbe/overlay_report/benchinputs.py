import re
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    source, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(
        r"^(BenchmarkOverlayPoll/(?:tmpfs|proc)/live[18]) "
        r"100000 [0-9]+[.][0-9]+ ns/op [0-9]+[.][0-9]+ allocs/op$"
    )
    expected = Counter({
        f"BenchmarkOverlayPoll/{layer}/live{waiters}": 10
        for layer in ("tmpfs", "proc") for waiters in (1, 8)
    })
    for arm in ("stock", "map", "fixed"):
        (receipt,) = source.rglob(f"{arm}-cost/receipt")
        rows = [line for line in receipt.read_text().splitlines()
                if line.startswith("BenchmarkOverlayPoll/")]
        samples = Counter()
        for row in rows:
            match = pattern.fullmatch(row)
            if match is None:
                raise ValueError(f"unexpected benchmark record in {receipt}")
            samples[match[1]] += 1
        if samples != expected:
            raise ValueError(f"incomplete {arm} samples: {samples}")
        (output / f"{arm}.bench").write_text("\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
