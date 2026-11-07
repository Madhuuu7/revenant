/* The runtime's shapes, and how a crash boundary maps onto the journal.
 *
 * The sweep reports one row per boundary and the run reports one entry per
 * event, and the console has to line them up: clicking "crash here" should
 * highlight the event it happened at. Nothing on the wire carries that
 * link, so `boundaryToEvent` derives it, and there is a test for the
 * arithmetic because getting it wrong would point the crash marker at the
 * wrong line and quietly misrepresent the whole claim.
 */

export type Boundary = {
  index: number;
  hook: string;
  detail: string;
};

export type WorkflowInfo = {
  name: string;
  sample_input: Record<string, unknown>;
  events: number;
  effects: number;
  boundaries: Boundary[];
};

export type SweepRow = {
  index: number;
  hook: string;
  detail: string;
  status: string;
  ok: boolean;
  resumes: number;
  dedupe: boolean;
};

export type SweepWorkflow = {
  events: number;
  boundaries: number;
  scenarios: number;
  passed: number;
  never_started: number;
  resumes: number;
  dedupe_saves: number;
  failures: { boundary: string; status: string; notes: string[] }[];
  hooks: string[];
  rows: SweepRow[];
};

export type Sweep = {
  total: number;
  passed: number;
  ok: boolean;
  workflows: Record<string, SweepWorkflow>;
};

export type TimelineEntry = {
  seq: number;
  kind: string;
  group: string;
  headline: string;
  costly: boolean;
  deduplicated: boolean;
  payload: Record<string, unknown>;
};

export type Run = {
  run_id: string;
  workflow: string;
  status: string;
  events: number;
  resumed_at: number | null;
  output: Record<string, unknown> | null;
  timeline: TimelineEntry[];
  effects: {
    key: string;
    effect: string;
    attempts: number;
    applied: boolean;
    deduplicated: boolean;
    costly: boolean;
  }[];
};

export async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      (body as { detail?: string }).detail ?? `HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

/**
 * The last event that was already durable when the process was killed.
 *
 * Everything up to and including this sequence number survived the crash;
 * everything after it was written by the resumed run. `0` means the
 * process died before any event existed, which is a real outcome -- the
 * "never started" case -- rather than a missing value.
 *
 * Boundaries are emitted in execution order and come in two families.
 * Every event contributes a `before_append` and an `after_append` pair,
 * and only the `after_append` makes it durable. Effect hooks
 * (`before_effect`, `after_effect`) fire between an EffectIntended and
 * its EffectObserved, so the newest durable event is the intent that was
 * appended just before them.
 *
 * Defining this as "the last durable event" rather than "the event the
 * crash sits at" is deliberate. The latter is ambiguous for exactly the
 * hooks that matter: at `after_effect` the crash is *at* the intent event
 * and *after* it, and rendering a marker from the ambiguous version put
 * the line one row too high -- which quietly says the charge had not
 * happened yet, the opposite of what that scenario demonstrates.
 */
export function lastDurableEvent(boundaries: Boundary[], index: number): number {
  let appended = 0;
  for (const b of boundaries) {
    // Count first: an after_append boundary is the moment its own event
    // becomes durable, so the crash there keeps it. A before_append
    // boundary adds nothing, so the newest durable event is whatever came
    // before it, which falls out of the same order.
    if (b.hook === "after_append") appended += 1;
    if (b.index === index) return appended;
  }
  return appended;
}

/** Whether a boundary sits in the window where an effect has reached the
 *  outside world but the log does not know it yet. These are the scenarios
 *  that double-charge under naive recovery. */
export function inDedupeWindow(hook: string): boolean {
  return hook === "after_effect" || hook === "before_append";
}
