/* Lining a crash boundary up with the event it happened at.
 *
 * Nothing on the wire carries this link, so the console derives it. Derive
 * it wrong and the crash marker points at the wrong line of the journal,
 * which misrepresents the project's entire claim while looking completely
 * convincing -- so the arithmetic is pinned against boundaries in the shape
 * the runtime actually emits.
 */

import { describe, expect, it } from "vitest";
import { inDedupeWindow, lastDurableEvent, type Boundary } from "./api";

/** Boundaries as the runtime emits them: a before/after pair per event,
 *  with effect hooks sitting between an intent and its observation. */
function boundaries(): Boundary[] {
  const out: Boundary[] = [];
  let i = 0;
  const append = (detail: string) => {
    out.push({ index: ++i, hook: "before_append", detail });
    out.push({ index: ++i, hook: "after_append", detail });
  };
  const effect = () => {
    out.push({ index: ++i, hook: "before_effect", detail: "create_account" });
    out.push({ index: ++i, hook: "after_effect", detail: "create_account" });
  };
  append("RunStarted"); // event 1
  append("StepStarted"); // event 2
  append("EffectIntended"); // event 3
  effect(); // between 3 and 4
  append("EffectObserved"); // event 4
  return out;
}

describe("the last event durable when the process died", () => {
  const bs = boundaries();

  it("excludes the event a before_append crash was about to write", () => {
    // Boundary 1 is before the very first append: nothing is durable.
    expect(lastDurableEvent(bs, 1)).toBe(0);
    // Boundary 3 is before StepStarted, with RunStarted already written.
    expect(lastDurableEvent(bs, 3)).toBe(1);
  });

  it("includes the event an after_append crash had just written", () => {
    expect(lastDurableEvent(bs, 2)).toBe(1);
    expect(lastDurableEvent(bs, 4)).toBe(2);
  });

  it("counts the intent as durable when an effect crash fires", () => {
    // This is the case the whole project is about. The effect runs between
    // EffectIntended (event 3) being durable and EffectObserved (event 4)
    // existing, so at both effect hooks the log ends at 3 -- the charge has
    // happened and nothing records it yet.
    const beforeEffect = bs.find((b) => b.hook === "before_effect")!;
    const afterEffect = bs.find((b) => b.hook === "after_effect")!;
    expect(lastDurableEvent(bs, beforeEffect.index)).toBe(3);
    expect(lastDurableEvent(bs, afterEffect.index)).toBe(3);
  });

  it("is monotonic along the boundary order", () => {
    // A later crash can never have less of the log than an earlier one.
    let previous = -1;
    for (const b of bs) {
      const durable = lastDurableEvent(bs, b.index);
      expect(durable).toBeGreaterThanOrEqual(previous);
      previous = durable;
    }
  });

  it("never claims more events than exist", () => {
    const events = bs.filter((b) => b.hook === "after_append").length;
    for (const b of bs) {
      expect(lastDurableEvent(bs, b.index)).toBeLessThanOrEqual(events);
    }
  });

  it("returns the full log for an index it does not know", () => {
    expect(lastDurableEvent(bs, 9999)).toBe(4);
  });
});

describe("the dedupe window", () => {
  it("covers the hooks where an effect is applied but unrecorded", () => {
    // after_effect: the call returned and the log has not been written.
    // before_append: the EffectObserved is about to be appended and is not
    // durable yet. A crash in either leaves the outside world ahead of the
    // journal, which is the case naive recovery double-charges.
    expect(inDedupeWindow("after_effect")).toBe(true);
    expect(inDedupeWindow("before_append")).toBe(true);
  });

  it("excludes the hooks where the world and the log agree", () => {
    expect(inDedupeWindow("before_effect")).toBe(false);
    expect(inDedupeWindow("after_append")).toBe(false);
  });
});
