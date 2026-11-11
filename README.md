# revenant

A durable agent runtime. An agent run is an append-only event log; a crash is a
cursor position in it. Kill the process anywhere and the run finishes itself,
without repeating what it had already done to the outside world.

The usual claim for this kind of system is "crash-safe". The claim here is
narrower and testable: **crash it at every single journal boundary and check
what comes out.**

```
140 crash scenarios -- one per journal boundary, across 3 workflows
140 recovered with the same output as an uninterrupted run
  0 effects applied twice (20 of the 140 crashed inside the window where the
    effect had reached the world and the log did not know it yet)
140 logs that replay byte-identically afterwards
```

That sweep runs in 0.10 s, so it is a test (`tests/test_sweep.py`), a CLI
(`scripts/sweep.py`), and a button in the console. 147 Python tests in all,
plus 16 for the console.

---

## The idea in one screen

```python
def onboarding(ctx, payload):
    with ctx.step("create-account"):
        account = ctx.effect("create_account", {"email": payload["email"]})
        ctx.state["account_id"] = account["account_id"]

    with ctx.step("charge"):
        charge = ctx.effect("charge_card", {"account_id": ctx.state["account_id"],
                                            "cents": payload["cents"]})
        ctx.state["charge_id"] = charge["charge_id"]
    ...
```

Plain Python. No generators, no decorators, no async. What makes it durable is
that the four things a function cannot be trusted with go through `ctx`:

| | | |
|---|---|---|
| `ctx.step(name)` | a resume boundary, and a digest of the state at it |
| `ctx.call_model(prompt)` | a read: recorded once, replayed thereafter |
| `ctx.effect(name, args)` | a write to the world, under a stable key |
| `ctx.now_ms()` / `ctx.draw(n)` | the clock and the dice |

Everything else is assumed to be a pure function of state, and replay is what
catches it when that assumption is wrong.

## Why replay works: three kinds of event

The log's alphabet is split three ways, and the split is the design:

- **Decisions** — `StepStarted`, `ModelCalled`, `RandomDrawn`. Produced by the
  agent from state it already has. On replay they are **recomputed and
  compared byte-for-byte** with what was recorded. A mismatch is a
  `DivergenceError`, which is the most useful failure in the system: it means
  the code is no longer a pure function of its journaled inputs.
- **Observations** — `ModelReturned`, `ClockRead`, `EffectObserved`. Things
  only the first execution could know. Replay **reads them back** instead of
  asking the world again.
- **Intents** — `EffectIntended`, written *before* the world is touched, so a
  crash in the gap is detectable afterwards.

One cursor serves all three modes of operation. A first run is a resume with an
empty log; verification is a resume whose cursor never reaches the end. A
second code path for replay is how these runtimes grow replay bugs.

## The exactly-once claim, stated honestly

**A runtime cannot give you exactly-once effects.** If the process can die
between "charge the card" and "write down that we charged the card", nothing on
our side distinguishes *sent, not recorded* from *not sent*. The two are
indistinguishable from the inside.

What a runtime can do is make the retry **recognisable**. Every effect is
published under a key derived from `(run_id, seq)` and nothing else — not the
clock, not a uuid, not the attempt number — so the retry after a crash carries
the same key as the attempt that may have landed. The guarantee splits in two:

- revenant guarantees at-least-once delivery under a stable key;
- the sink guarantees one key applies once.

`SqliteSink` is that second half in 40 lines: a `UNIQUE` column and an insert
that either wins or tells you it lost. It is the same mechanism as Stripe's
`Idempotency-Key` header or an `INSERT ... ON CONFLICT DO NOTHING`; the only
difference is whose database it is.

The one window that stays open is a **read** lost before it was journaled. A
crash between `ctx.now_ms()` and its append loses the value, and the resumed
run reads a later time. No journal design closes that for a read.
`tests/test_lost_observation.py` asserts exactly that behaviour rather than
leaving it as a footnote.

## What the sweep found

Three real bugs, each of which would have survived a hand-picked set of crash
points:

1. **The idempotency key was recomputed on resume** instead of read back from
   the log. By then the log had a `RunResumed` in it, so the next free seq had
   moved, so every post-crash retry published under a *new* key and no sink
   could ever have deduped it. This is the bug the whole project exists to
   avoid, and it was there until the sweep ran.
2. **`SimulatedCrash` inherited from `Exception`**, so the runtime caught its
   own simulated crash and wrote a tidy `RunFailed` epitaph — something a real
   `SIGKILL` never permits. It is a `BaseException` now.
3. **Randomness was an observation.** A draw taken from the OS and lost before
   its append meant the resumed run drew a different value, and in `triage`
   that *flipped a branch*: the recovered run paged an engineer the
   uninterrupted run did not. Randomness is now **derived** from
   `(run_id, draw_index)` — recomputable, so losing the event loses nothing —
   and still logged, so replay checks the recomputed value against the record.

The third one is the one worth remembering. The first two were mistakes; the
third was a category error about what kind of thing a random number is.

## Running it

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"

python scripts/sweep.py                 # the 140 scenarios, ~0.1 s
python scripts/sweep.py --workflow triage --verbose
python -m pytest                        # 147 tests
python scripts/bench.py                 # the journal numbers quoted below

uvicorn revenant.api.app:app --app-dir src --port 8099
# then open http://127.0.0.1:8099
```

### The console

A React page built with Vite, served from the same origin as the API at `/`.
It is drawn as the document the sweep produces — a register of crashes and
returns, on ruled stock — rather than a dashboard:

- **the strips**: one pen mark per journal boundary, in the order the run
  reaches them, coloured by what came back. The claim is a count, so the page
  is a thing you can count. Violet marks the twenty boundaries inside the
  window where the effect had already reached the outside world and the log
  did not know yet; a half-height ochre mark is a boundary where nothing had
  been written, so there was nothing to recover;
- **the entry**, written against whichever mark you click, saying how many
  resumes it took and whether the effect was recognised rather than repeated;
- **the footing**, the sweep's own totals;
- **the log**, the journal a real run of that workflow wrote, with the crash
  ruled across it — above the rule is what was durable, below it is the
  resume. An intent and an observation are coloured as the two sides of the
  dedupe window, because a crash between them is the only hard case.

Published without a runtime behind it — on Pages — the page reads the
recordings under `web/public/demo` and says on itself that it is doing so.
The sweep is deterministic, so the recording is the output rather than a
picture of one; the only thing the static page cannot do is sweep on demand,
and it declines to time a file read and call that a sweep.

## API

| | |
|---|---|
| `GET /workflows` | names, sample inputs, and every crash boundary |
| `POST /runs` | `{workflow, input?, crash_at?, auto_recover?}` |
| `GET /runs/{id}` | classified timeline plus the effect ledger |
| `GET /runs/{id}/log` | the raw log, canonical JSON per line |
| `POST /runs/{id}/resume` | continue an interrupted run |
| `POST /runs/{id}/verify` | re-execute against the log and compare |
| `POST /recover` | resume every interrupted run |
| `POST /sweep` | run the fault sweep now |

## Storage

Three journals behind one contract — gapless `seq`, atomic append, durable
append:

| | |
|---|---|
| **SQLite** (default) | WAL + `synchronous=FULL`, `UNIQUE(run_id, seq)` |
| **MongoDB** | for when two workers can pick up the same interrupted run, and the database has to arbitrate |
| **in-memory** | the fault sweep, which would otherwise fsync 2,800 times |

Measured on an i7-13620H (`scripts/bench.py`):

| journal | appends/s | per append |
|---|---:|---:|
| in-memory | 223,869 | 0.004 ms |
| SQLite `synchronous=FULL` | 1,259 | 0.794 ms |
| SQLite `synchronous=NORMAL` | 13,740 | 0.073 ms |
| SQLite `synchronous=OFF` | 18,257 | 0.055 ms |

FULL costs 11x against NORMAL and it is still the default: an agent run is tens
of events, so a whole run spends about 16 ms in fsync, and NORMAL would make
the central claim depend on *which* failure happened — process death yes,
power loss no.

The 140-scenario sweep takes 0.10 s in memory and would take roughly 2 s
durable. A 20x slower check is one that stops being run while developing,
which is when it catches things.

## Known limits

- **Not distributed.** Recovery is safe to run from several workers — they race
  on the first append and the journal's unique index picks a winner — but
  there is no leader election, no heartbeat, and no lease. A worker that hangs
  without dying holds its run indefinitely.
- **Replay re-executes from the start.** No snapshots, so a run with thousands
  of events replays all of them. The fix is a periodic state snapshot plus a
  digest check; the digest is already recorded in `StepFinished`, which is
  where that work would start.
- **The scripted model is a stand-in.** `HttpModel` speaks the OpenAI shape
  (so Groq, OpenRouter, llama.cpp all work) but has never been run against a
  paid endpoint — there is no key. Determinism does not depend on it: a replay
  never calls the model at all.
- **`ctx.draw` is not cryptographic.** It is a deliberate pure function of the
  run id. Anything needing unpredictability-after-the-fact has to be journaled
  as an effect, not a draw.
- **Mongo is tested by hand, not in CI.** There is no mongod in this project's
  CI, so `MongoJournal` is exercised against a local server and Atlas free
  tier manually. The journal contract tests run against SQLite and memory.

## Deploying

`render.yaml` deploys one free web service serving the API and the console
together. No model key is set, so the deployed instance runs the scripted model
and cannot spend money. `REVENANT_DB` points at `/tmp` because the free disk is
ephemeral — runs are lost on restart, which is the honest default for a demo
whose point is what happens inside a single session. Set `REVENANT_STORE=mongo`
and `REVENANT_MONGO_URI` for runs that outlive the instance.

## Layout

```
src/revenant/
  events.py          the log's alphabet, canonical JSON
  store/             journal contract + sqlite / mongo / memory
  runtime/
    context.py       the cursor: one mechanism for run, resume and verify
    engine.py        start, resume, recover, verify
    effects.py       sinks, and the half of exactly-once that lives there
    faults.py        the four crash hooks
    sweep.py         the exhaustive sweep
    model.py         scripted and HTTP models
  workflows/         three workflows that fail in different ways
  api/app.py         FastAPI, serving the console from the same origin
web/                 the console: React + Vite, self-hosted fonts, recordings
scripts/             sweep and bench
```
