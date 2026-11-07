import { useCallback, useEffect, useMemo, useState } from "react";
import { Journal } from "./components/Journal";
import { Matrix, type Picked } from "./components/Matrix";
import {
  lastDurableEvent,
  getJSON,
  type Run,
  type Sweep,
  type WorkflowInfo,
} from "./lib/api";

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const v = localStorage.getItem("revenant-theme");
      if (v === "dark" || v === "light") return v;
    } catch {
      /* private window */
    }
    return typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
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

  const runSweep = useCallback(async () => {
    setBusy(true);
    setNote(null);
    const started = performance.now();
    try {
      const body = await getJSON<Sweep>("/sweep", { method: "POST" });
      setSweep(body);
      setElapsed(performance.now() - started);
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
    getJSON<{ workflows: WorkflowInfo[] }>("/workflows")
      .then((b) => setWorkflows(b.workflows))
      .catch(() => undefined);
  }, [runSweep]);

  // A real run of whichever workflow the selected crash belongs to, so the
  // journal below the matrix is that workflow's actual log rather than a
  // generic example.
  const wantRun = picked?.workflow ?? workflows[0]?.name ?? null;
  useEffect(() => {
    if (!wantRun) return;
    if (run?.workflow === wantRun) return;
    let live = true;
    getJSON<{ run_id: string }>("/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ workflow: wantRun }),
    })
      .then((created) => getJSON<Run>(`/runs/${created.run_id}`))
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
      <header className="top">
        <h1>revenant</h1>
        <p>
          An agent run is an append-only event log, and a crash is a cursor
          position in it. Kill the process anywhere and the run finishes
          itself — without repeating what it had already done to the outside
          world.
        </p>
        <div className="controls">
          <button className="go" onClick={() => void runSweep()} disabled={busy}>
            {busy ? "crashing…" : "Crash it everywhere"}
          </button>
          <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </div>
      </header>

      {note && (
        <p className="state err" style={{ marginBottom: 20 }}>
          {note}
        </p>
      )}

      {totals && (
        <div className="verdict">
          <span className={`big${sweep && !sweep.ok ? " bad" : ""}`}>
            {totals.passed}/{totals.scenarios}
          </span>
          <span className="says">
            crash scenarios recovered — one for every journal boundary across{" "}
            {totals.workflows} workflows, with <b>{totals.dedupe}</b> of them
            landing in the window where the effect had already reached the
            outside world and the log did not know.
            {elapsed !== null && ` Swept in ${(elapsed / 1000).toFixed(2)}s.`}
          </span>
        </div>
      )}

      <section>
        <h2>Every place it can die</h2>
        <p className="sub">
          One square per journal boundary. The sweep killed the process at
          each one and restarted it; the colour is what came back. Click any
          square to see where in the log it happened.
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

        {picked && (
          <div
            className={`picked-detail${
              picked.row.dedupe
                ? " dedupe"
                : picked.row.status === "never-started"
                  ? " idle"
                  : ""
            }`}
          >
            <p style={{ margin: 0 }}>
              <span className="mono">
                #{picked.row.index} {picked.row.hook} · {picked.row.detail}
              </span>
            </p>
            <p style={{ margin: "6px 0 0" }}>
              {picked.row.status === "never-started" ? (
                <>
                  The process died before anything was written, so there was{" "}
                  <b>nothing to recover</b> — and nothing had happened to the
                  outside world either. This is the one boundary where doing
                  nothing is correct.
                </>
              ) : picked.row.dedupe ? (
                <>
                  Recovered after <b>{picked.row.resumes}</b> resume
                  {picked.row.resumes === 1 ? "" : "s"}, and the effect was{" "}
                  <b>recognised rather than repeated</b>. The process died
                  between the effect reaching the world and the log recording
                  it, which is the case naive recovery charges the card twice
                  for.
                </>
              ) : (
                <>
                  Recovered after <b>{picked.row.resumes}</b> resume
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
          <h2>The sweep</h2>
          <div className="tiles">
            <div>
              <span className="v">{totals.scenarios}</span>
              <span className="k">crash points</span>
            </div>
            <div>
              <span className="v">{totals.passed}</span>
              <span className="k">recovered</span>
            </div>
            <div>
              <span className="v">{totals.dedupe}</span>
              <span className="k">saved by dedupe</span>
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
              <span className="k">to sweep</span>
            </div>
          </div>
        </section>
      )}

      <section>
        <h2>
          The log{run ? ` — ${run.workflow}` : ""}
          {picked ? ", with the crash marked" : ""}
        </h2>
        <p className="sub">
          A real run of this workflow, one line per event. Everything above
          the marker was durable when the process died; everything below it
          was written by the resumed run.
        </p>
        <Journal entries={run?.timeline ?? []} durableThrough={durableThrough} />
        {run?.output && (
          <p className="sub" style={{ marginTop: 14 }}>
            Output:{" "}
            <code>{JSON.stringify(run.output)}</code>
          </p>
        )}
      </section>

      <footer>
        The sweep is a test, a CLI and this button. It kills the process at
        every journal boundary across three workflows, and asserts that each
        run finishes with the same output as an uninterrupted one, applies no
        effect twice, and replays byte-identically afterwards.
      </footer>
    </div>
  );
}
