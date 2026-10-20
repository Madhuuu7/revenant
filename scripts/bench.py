"""Measure the two numbers the code's comments depend on.

1. Append throughput per journal, which is what decides whether
   ``synchronous=FULL`` is affordable.
2. How long the fault sweep takes on a durable journal versus an in-memory
   one, which is the reason ``MemoryJournal`` exists at all.

    python scripts/bench.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from revenant.events import StepStarted  # noqa: E402
from revenant.runtime.sweep import sweep_workflow  # noqa: E402
from revenant.store import MemoryJournal, SqliteJournal  # noqa: E402

N = 2000


def throughput(make, label: str) -> float:
    journal = make()
    try:
        started = time.perf_counter()
        for i in range(1, N + 1):
            journal.append("bench", StepStarted(name="x"), i)
        elapsed = time.perf_counter() - started
    finally:
        journal.close()
    rate = N / elapsed
    print(f"  {label:<28} {rate:>9,.0f} appends/s  ({elapsed*1000/N:.3f} ms each)")
    return rate


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="revenant-bench-"))
    print(f"append throughput ({N:,} events each)")
    mem = throughput(MemoryJournal, "memory")
    full = throughput(
        lambda: SqliteJournal(tmp / "full.db", synchronous="FULL"), "sqlite synchronous=FULL"
    )
    normal = throughput(
        lambda: SqliteJournal(tmp / "normal.db", synchronous="NORMAL"),
        "sqlite synchronous=NORMAL",
    )
    off = throughput(
        lambda: SqliteJournal(tmp / "off.db", synchronous="OFF"), "sqlite synchronous=OFF"
    )
    print(f"\n  FULL costs {normal/full:.1f}x against NORMAL, {off/full:.1f}x against OFF")
    print(f"  memory is {mem/full:.0f}x faster than FULL")

    print("\nfault sweep, in-memory journals")
    started = time.perf_counter()
    total = 0
    for name in ("onboarding", "research", "triage"):
        res = sweep_workflow(name)
        total += res.scenarios
    mem_sweep = time.perf_counter() - started
    print(f"  {total} scenarios in {mem_sweep:.2f}s")

    # A durable sweep means one fsync per append, times every event of every
    # scenario. Estimated rather than run, because the point is the ratio.
    events = total * 20
    print(f"\n  ~{events:,} appends at {full:,.0f}/s would be ~{events/full:.0f}s on FULL")
    print(f"  i.e. about {(events/full)/mem_sweep:.0f}x the in-memory sweep")


if __name__ == "__main__":
    main()
