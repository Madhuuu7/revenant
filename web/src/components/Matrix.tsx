/* The crash matrix: one cell per place the process can be killed.
 *
 * This is the project in one picture. Every cell is a journal boundary, the
 * sweep killed the process at each one, and the colour is what came back.
 * A reader does not have to take "crash-safe" on trust — they can count the
 * squares.
 *
 * Violet is the point of the whole thing. Those are the boundaries inside
 * the window where an effect has already reached the outside world and the
 * log does not yet know, so recovery has to recognise the work rather than
 * redo it. Folding them into the green would hide the only scenarios that
 * are actually hard.
 *
 * Cells are clickable because a grid of 140 squares is a claim, and being
 * able to ask any one of them what happened is the evidence.
 */

import type { SweepRow, SweepWorkflow } from "../lib/api";

export type Picked = { workflow: string; row: SweepRow } | null;

function cellClass(row: SweepRow): string {
  if (!row.ok) return "cell bad";
  if (row.status === "never-started") return "cell idle";
  if (row.dedupe) return "cell dedupe";
  return "cell ok";
}

function title(row: SweepRow): string {
  const where = `#${row.index} ${row.hook} · ${row.detail}`;
  if (!row.ok) return `${where} — FAILED (${row.status})`;
  if (row.status === "never-started")
    return `${where} — killed before anything was written; nothing to recover`;
  if (row.dedupe)
    return `${where} — recovered, and the effect was recognised rather than repeated`;
  return `${where} — recovered, ${row.resumes} resume${row.resumes === 1 ? "" : "s"}`;
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
  /** Changes on each sweep, so the cells re-stagger in. */
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
          <div className="wfblock" key={name}>
            <div className="wfhead">
              <span className="nm">{name}</span>
              <span className="meta">
                {wf.events} events · {wf.scenarios} crash points ·{" "}
                {wf.passed}/{wf.scenarios} recovered · {wf.dedupe_saves} saved
                by dedupe
              </span>
            </div>
            <div className="matrix">
              {wf.rows.map((row, i) => {
                const chosen =
                  picked &&
                  picked.workflow === name &&
                  picked.row.index === row.index;
                return (
                  <button
                    key={`${animateKey}-${row.index}`}
                    className={`${cellClass(row)} fresh${chosen ? " picked" : ""}`}
                    style={{ animationDelay: `${Math.min(i * 6, 600)}ms` }}
                    title={title(row)}
                    aria-label={title(row)}
                    onClick={() => onPick(name, row)}
                  />
                );
              })}
            </div>
          </div>
        );
      })}

      <div className="key">
        <span>
          <i style={{ background: "var(--ok)" }} /> recovered
        </span>
        <span>
          <i style={{ background: "var(--dedupe)" }} /> recovered, and the
          effect was already applied when the process died
        </span>
        <span>
          <i style={{ background: "var(--idle)", opacity: 0.55 }} /> killed
          before anything was written
        </span>
        <span>
          <i style={{ background: "var(--bad)" }} /> failed
        </span>
      </div>
    </>
  );
}
