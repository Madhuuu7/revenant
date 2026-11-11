/* The log: the append-only journal a run actually wrote.
 *
 * The runtime's whole model is that a run is this list and a crash is a
 * position in it, so the register shows the list rather than a summary of
 * it. When a mark is chosen in the strips above, the line the crash
 * happened at is ruled here — everything above it was durable, everything
 * below was written by the resumed process.
 *
 * Two tags carry the weight. "spends money" marks an effect that costs
 * something in the outside world, which is the reason exactly-once matters
 * at all; "not repeated" marks one that recovery recognised instead of
 * doing again.
 */

import type { TimelineEntry } from "../lib/api";

function CrashLine({ said }: { said: string }) {
  return (
    <div className="crashline">
      <span className="said">{said}</span>
      <div className="bar" />
    </div>
  );
}

export function Journal({
  entries,
  durableThrough,
}: {
  entries: TimelineEntry[];
  /** Last event that was durable when the process died; 0 means none were,
   *  and null means no crash is selected. */
  durableThrough: number | null;
}) {
  if (!entries.length) {
    return (
      <p className="state">
        No run yet. Start one and its log appears here, one line per event.
      </p>
    );
  }

  return (
    <div className="journal">
      {durableThrough === 0 && (
        <CrashLine said="killed before the first event was written" />
      )}
      {entries.map((e) => (
        <div key={e.seq}>
          <div
            className={`line${
              durableThrough !== null && e.seq > durableThrough ? " resumed" : ""
            }`}
          >
            <span className="seq">{e.seq}</span>
            <span className={`kind kind-${e.group}`}>{e.kind}</span>
            <span className="head">{e.headline}</span>
            <span className="tags">
              {e.costly && <span className="tag costly">spends money</span>}
              {e.deduplicated && (
                <span className="tag dedupe">not repeated</span>
              )}
            </span>
          </div>
          {durableThrough === e.seq && (
            <CrashLine said="killed here — everything below is the resume" />
          )}
        </div>
      ))}
    </div>
  );
}
