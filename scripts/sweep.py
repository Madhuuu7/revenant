"""Run the exhaustive crash sweep and print the table the README quotes.

    python scripts/sweep.py                 # every workflow
    python scripts/sweep.py --workflow triage --verbose
    python scripts/sweep.py --json report.json

Exit code is 1 if any scenario failed, so this is usable as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from revenant.runtime.sweep import sweep_workflow  # noqa: E402
from revenant.workflows import REGISTRY  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", action="append", choices=sorted(REGISTRY))
    ap.add_argument("--verbose", action="store_true", help="one line per scenario")
    ap.add_argument("--json", type=Path, help="write the full report here")
    args = ap.parse_args()

    names = args.workflow or sorted(REGISTRY)
    started = time.perf_counter()
    results = {}
    for name in names:
        def progress(report, _name=name):
            if args.verbose:
                mark = "ok " if report.ok or report.final_status == "never-started" else "FAIL"
                print(
                    f"  {mark} #{report.boundary.index:>3} {report.boundary.hook:<14}"
                    f" {report.boundary.detail:<20} resumes={report.resumes}"
                    f" {';'.join(report.notes)}"
                )
        print(f"[{name}] sweeping...")
        results[name] = sweep_workflow(name, progress=progress)
    elapsed = time.perf_counter() - started

    print()
    header = (
        f"{'workflow':<12}{'events':>7}{'boundaries':>12}{'scenarios':>11}"
        f"{'passed':>8}{'resumes':>9}{'dedupe':>8}"
    )
    print(header)
    print("-" * len(header))
    total = passed = 0
    hooks: Counter[str] = Counter()
    for name, res in results.items():
        total += res.scenarios
        ok = res.scenarios - len(res.failures)
        passed += ok
        for r in res.reports:
            if "dedupe" in " ".join(r.notes):
                hooks[r.boundary.hook] += 1
        print(
            f"{name:<12}{res.reference.events:>7}{len(res.reference.boundaries):>12}"
            f"{res.scenarios:>11}{ok:>8}{res.resumes:>9}{res.dedupe_saves:>8}"
        )
    print("-" * len(header))
    print(f"{'total':<12}{'':>7}{'':>12}{total:>11}{passed:>8}")
    print()
    print(f"swept {total} crash scenarios in {elapsed:.1f}s")
    if hooks:
        detail = ", ".join(f"{h}={n}" for h, n in sorted(hooks.items()))
        print(f"dedupe window reached from: {detail}")

    failures = [r for res in results.values() for r in res.failures]
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for r in failures[:20]:
            print(
                f"  #{r.boundary.index} {r.boundary.hook}/{r.boundary.detail}:"
                f" status={r.final_status} output_matches={r.output_matches}"
                f" exactly_once={r.effects_exactly_once}"
                f" replay={r.replay_identical} {';'.join(r.notes)}"
            )

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    name: {
                        "events": res.reference.events,
                        "boundaries": len(res.reference.boundaries),
                        "scenarios": res.scenarios,
                        "failures": len(res.failures),
                        "never_started": res.never_started,
                        "resumes": res.resumes,
                        "dedupe_saves": res.dedupe_saves,
                        "effect_keys": res.reference.effect_keys,
                    }
                    for name, res in results.items()
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
