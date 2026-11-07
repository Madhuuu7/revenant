/* The journal: the append-only log a run actually wrote.
 *
 * The runtime's whole model is that a run is this list and a crash is a
 * position in it, so the console shows the list rather than a summary of
 * it. When a crash boundary is selected in the matrix above, the line it
 * happened at is marked here — everything above it was durable, everything
 * below was written by the resumed process.
 *
 * Two tags carry the weight. `costly` marks an effect that spends money,
 * which is the reason exactly-once matters at all; `deduplicated` marks one
 * that recovery recognised instead of repeating.
 */

import type { TimelineEntry } from "../lib/api";

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
        <div className="crashline">
          <span />
          <div className="bar">
            <span>process killed before the first event was written</span>
          </div>
        </div>
      )}
      {entries.map((e) => (
        <div key={e.seq}>
          <div
            className={`entry${
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
            <div className="crashline">
              <span />
              <div className="bar">
                <span>
                  process killed here — everything below is the resume
                </span>
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
