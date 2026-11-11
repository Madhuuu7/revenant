/* The strips: one pen mark per place the process can be killed.
 *
 * This is the project in one picture, and it is drawn as a chart rather
 * than a grid of tiles because the claim is a count. Every mark is a
 * journal boundary, the sweep killed the process at each one, and the
 * colour is what came back. A reader who does not want to take "crash-safe"
 * on trust can count the marks against the ticks underneath.
 *
 * Violet is the point of the whole thing. Those are the boundaries inside
 * the window where an effect has already reached the outside world and the
 * log does not yet know, so recovery has to recognise the work rather than
 * redo it. Folding them into the green would hide the only scenarios that
 * are actually hard.
 *
 * The "nothing was written" marks are drawn at half height. Nothing was
 * recorded at those boundaries, so a full-height mark would claim a
 * recovery that never had to happen.
 *
 * Marks are clickable because a row of 48 of them is a claim, and being
 * able to ask any one of them what happened is the evidence.
 */

import type { SweepRow, SweepWorkflow } from "../lib/api";

export type Picked = { workflow: string; row: SweepRow } | null;

function markClass(row: SweepRow): string {
  if (!row.ok) return "mark failed";
  if (row.status === "never-started") return "mark unborn";
  if (row.dedupe) return "mark dedupe";
  return "mark";
}

function title(row: SweepRow): string {
  const where = `#${row.index} ${row.hook} — ${row.detail}`;
  if (!row.ok) return `${where} — FAILED (${row.status})`;
  if (row.status === "never-started")
    return `${where} — killed before anything was written; nothing to recover`;
  if (row.dedupe)
    return `${where} — returned, and the effect was recognised rather than repeated`;
  return `${where} — returned after ${row.resumes} resume${row.resumes === 1 ? "" : "s"}`;
}

export function Matrix({
  workflows,
  picked,
  onPick,
  animateKey,
}: {
  workflows: Record<string, SweepWorkflow>;
  picked: Picked;
  onPick: (workflow: string, row: SweepRow) => void;
  /** Changes on each sweep, so the marks re-ink. */
  animateKey: number;
}) {
  const names = Object.keys(workflows).sort();
  if (!names.length) {
    return <p className="state">No workflows are registered.</p>;
  }

  return (
    <>
      {names.map((name) => {
        const wf = workflows[name];
        return (
          <div className="wf" key={name}>
            <div className="wfhead">
              <span className="nm">{name}</span>
              <span className="tally">
                {wf.scenarios} killed, {wf.passed} returned,{" "}
                {wf.dedupe_saves} of them already paid for
              </span>
            </div>
            <div className="strip">
              {wf.rows.map((row, i) => {
                const chosen =
                  picked &&
                  picked.workflow === name &&
                  picked.row.index === row.index;
                return (
                  <button
                    key={`${animateKey}-${row.index}`}
                    className={`${markClass(row)}${chosen ? " picked" : ""}`}
                    style={{ animationDelay: `${Math.min(i * 6, 500)}ms` }}
                    title={title(row)}
                    aria-label={title(row)}
                    onClick={() => onPick(name, row)}
                  />
                );
              })}
            </div>
            <div className="ticks" aria-hidden="true">
              {wf.rows.map((row, i) => (
                <span className="tick" key={row.index}>
                  {i === 0 || (i + 1) % 10 === 0 ? <span>{i + 1}</span> : null}
                </span>
              ))}
            </div>
          </div>
        );
      })}

      <div className="key">
        <span>
          <i style={{ background: "var(--returned-mark)" }} /> returned
        </span>
        <span>
          <i style={{ background: "var(--dedupe-mark)" }} /> returned, and the
          effect had already reached the world when the process died
        </span>
        <span>
          <i className="short" style={{ background: "var(--unborn-mark)" }} />{" "}
          killed before anything was written
        </span>
        <span>
          <i style={{ background: "var(--failed-mark)" }} /> failed
        </span>
      </div>
    </>
  );
}
