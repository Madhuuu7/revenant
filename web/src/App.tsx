import { useCallback, useEffect, useMemo, useState } from "react";
import { Journal } from "./components/Journal";
import { Matrix, type Picked } from "./components/Matrix";
import {
  getJSON,
  lastDurableEvent,
  type Run,
  type Sweep,
  type WorkflowInfo,
} from "./lib/api";
import { detect, read, type Mode } from "./lib/source";

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const v = localStorage.getItem("revenant-theme");
      if (v === "dark" || v === "light") return v;
    } catch {
      /* private window */
    }
    return typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("revenant-theme", theme);
    } catch {
      /* the page still works, it just will not remember */
    }
  }, [theme]);

  const [sweep, setSweep] = useState<Sweep | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [picked, setPicked] = useState<Picked>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [sweepKey, setSweepKey] = useState(0);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);

  const runSweep = useCallback(async () => {
    setBusy(true);
    setNote(null);
    const started = performance.now();
    try {
      const where = await detect();
      setMode(where);
      const body =
        where === "live"
          ? await getJSON<Sweep>("sweep", { method: "POST" })
          : await read<Sweep>("sweep", "sweep.json");
      setSweep(body);
      // Only time a sweep that actually ran. Timing a file read and
      // calling it "swept in 0.00s" would be the one dishonest number on
      // the page.
      setElapsed(where === "live" ? performance.now() - started : null);
      setSweepKey((k) => k + 1);
      setPicked(null);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "the sweep did not run");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void runSweep();
    read<{ workflows: WorkflowInfo[] }>("workflows", "workflows.json")
      .then((b) => setWorkflows(b.workflows))
      .catch(() => undefined);
  }, [runSweep]);

  // A real run of whichever workflow the selected crash belongs to, so the
  // log below the strips is that workflow's actual journal rather than a
  // generic example.
  const wantRun = picked?.workflow ?? workflows[0]?.name ?? null;
  useEffect(() => {
    if (!wantRun) return;
    if (run?.workflow === wantRun) return;
    let live = true;
    detect()
      .then((where) =>
        where === "live"
          ? getJSON<{ run_id: string }>("runs", {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ workflow: wantRun }),
            }).then((created) => getJSON<Run>(`runs/${created.run_id}`))
          : read<Run>(`runs/${wantRun}`, `run-${wantRun}.json`),
      )
      .then((full) => {
        if (live) setRun(full);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [wantRun, run?.workflow]);

  const durableThrough = useMemo(() => {
    if (!picked) return null;
    const wf = workflows.find((w) => w.name === picked.workflow);
    if (!wf) return null;
    return lastDurableEvent(wf.boundaries, picked.row.index);
  }, [picked, workflows]);

  const totals = useMemo(() => {
    if (!sweep) return null;
    const wfs = Object.values(sweep.workflows);
    return {
      scenarios: sweep.total,
      passed: sweep.passed,
      dedupe: wfs.reduce((a, w) => a + w.dedupe_saves, 0),
      resumes: wfs.reduce((a, w) => a + w.resumes, 0),
      neverStarted: wfs.reduce((a, w) => a + w.never_started, 0),
      workflows: wfs.length,
    };
  }, [sweep]);

  return (
    <div className="shell">
      <article className="sheet">
        <header className="letterhead">
          <div>
            <h1>revenant</h1>
            <p className="standing">A register of crashes and returns</p>
          </div>
          <div className="controls">
            <button
              className="go"
              onClick={() => void runSweep()}
              disabled={busy}
            >
              {busy
                ? "crashing…"
                : mode === "recorded"
                  ? "Reload the register"
                  : "Crash it everywhere"}
            </button>
            <button
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? "Light" : "Dark"}
            </button>
          </div>
          <p className="brief">
            An agent run is an append-only event log, and a crash is a cursor
            position in it. Kill the process anywhere and the run finishes
            itself — without repeating what it had already done to the outside
            world.
          </p>
        </header>

        {note && <p className="note err">{note}</p>}

        {mode === "recorded" && (
          <p className="note">
            <b>This register was recorded, not kept live.</b> There is no
            runtime behind this page, so the marks below came from a real
            sweep rather than one that just ran. They are the same marks
            either way — the sweep is deterministic, with in-memory journals
            and a fixed boundary order, so every run produces these 140
            results. What you cannot do here is crash it yourself:{" "}
            <code>uvicorn revenant.api.app:app --app-dir src</code> gives you
            the live one.
          </p>
        )}

        <section>
          <h2>Every place it can die</h2>
          <p className="sub">
            One mark per journal boundary, in the order the run reaches them.
            The sweep killed the process at each one and restarted it; the
            colour is what came back. Click any mark to read the entry made
            against it.
          </p>
          {sweep ? (
            <Matrix
              workflows={sweep.workflows}
              picked={picked}
              onPick={(workflow, row) => setPicked({ workflow, row })}
              animateKey={sweepKey}
            />
          ) : (
            <p className="state">Sweeping.</p>
          )}

          {totals && (
            <p className="verdict">
              <span className={`count${sweep && !sweep.ok ? " bad" : ""}`}>
                {totals.passed}/{totals.scenarios}
              </span>{" "}
              returned with the same output as an uninterrupted run, across{" "}
              {totals.workflows} workflows. <b>{totals.dedupe}</b> of them died
              in the window where the effect had already reached the outside
              world and the log did not know yet — the case naive recovery
              charges the card twice for.
              {elapsed !== null && ` Swept in ${(elapsed / 1000).toFixed(2)}s.`}
            </p>
          )}

          {picked && (
            <div
              className={`entry-note${
                picked.row.dedupe
                  ? " dedupe"
                  : picked.row.status === "never-started"
                    ? " unborn"
                    : ""
              }`}
            >
              <span className="where">
                #{picked.row.index} {picked.row.hook} — {picked.row.detail}
              </span>
              <p>
                {picked.row.status === "never-started" ? (
                  <>
                    The process died before anything was written, so there was{" "}
                    <b>nothing to recover</b> — and nothing had happened to the
                    outside world either. This is the one boundary where doing
                    nothing is correct.
                  </>
                ) : picked.row.dedupe ? (
                  <>
                    Returned after <b>{picked.row.resumes}</b> resume
                    {picked.row.resumes === 1 ? "" : "s"}, and the effect was{" "}
                    <b>recognised rather than repeated</b>. The process died
                    between the effect reaching the world and the log recording
                    it, which is the case naive recovery charges the card twice
                    for.
                  </>
                ) : (
                  <>
                    Returned after <b>{picked.row.resumes}</b> resume
                    {picked.row.resumes === 1 ? "" : "s"}, with the same output
                    as an uninterrupted run and a byte-identical log.
                  </>
                )}
              </p>
            </div>
          )}
        </section>

        {totals && (
          <section>
            <h2>The footing</h2>
            <p className="sub">
              What the sweep counted, once every mark above was made.
            </p>
            <div className="footing">
              <div>
                <span className="v">{totals.scenarios}</span>
                <span className="k">places to die</span>
              </div>
              <div>
                <span className="v">{totals.passed}</span>
                <span className="k">returned</span>
              </div>
              <div>
                <span className="v">{totals.dedupe}</span>
                <span className="k">already paid</span>
              </div>
              <div>
                <span className="v">{totals.resumes}</span>
                <span className="k">resumes</span>
              </div>
              <div>
                <span className="v">{totals.neverStarted}</span>
                <span className="k">never started</span>
              </div>
              <div>
                <span className="v">
                  {elapsed === null ? "—" : `${(elapsed / 1000).toFixed(2)}s`}
                </span>
                <span className="k">
                  {elapsed === null ? "not run here" : "to sweep"}
                </span>
              </div>
            </div>
          </section>
        )}

        <section>
          <h2>
            The log{run ? ` — ${run.workflow}` : ""}
            {picked ? ", with the crash ruled in" : ""}
          </h2>
          <p className="sub">
            A real run of this workflow, one line per event. Everything above
            the rule was durable when the process died; everything below it was
            written by the resumed run.
          </p>
          <Journal
            entries={run?.timeline ?? []}
            durableThrough={durableThrough}
          />
          {run?.output && (
            <p className="output">
              It finished with <code>{JSON.stringify(run.output)}</code>, and so
              did every crashed run of it.
            </p>
          )}
        </section>

        <footer>
          The sweep is a test, a CLI and this button. It kills the process at
          every journal boundary across three workflows, and asserts that each
          run finishes with the same output as an uninterrupted one, applies no
          effect twice, and replays byte-identically afterwards.
        </footer>
      </article>
    </div>
  );
}
