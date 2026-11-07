/* Choosing between a live runtime and a recording.
 *
 * The failure worth preventing is the quiet one: a static page that keeps
 * hitting a dead endpoint, or a live page that silently serves a recording
 * and shows stale results as though they were current.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { detect, read, setMode } from "./source";

afterEach(() => {
  setMode(null);
  vi.unstubAllGlobals();
});

function stubFetch(impl: (url: string) => Response | Promise<Response>) {
  const spy = vi.fn((input: RequestInfo | URL) => Promise.resolve(impl(String(input))));
  vi.stubGlobal("fetch", spy);
  return spy;
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200 });

describe("deciding where the data comes from", () => {
  it("uses the runtime when it answers", async () => {
    stubFetch(() => ok({ status: "ok" }));
    expect(await detect()).toBe("live");
  });

  it("falls back to the recording when nothing answers", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    expect(await detect()).toBe("recorded");
  });

  it("falls back when the runtime answers with an error", async () => {
    stubFetch(() => new Response("nope", { status: 502 }));
    expect(await detect()).toBe("recorded");
  });

  it("probes once and remembers", async () => {
    const spy = stubFetch(() => ok({ status: "ok" }));
    await detect();
    await detect();
    await detect();
    // Retrying a dead endpoint on every read is what makes a static page
    // feel broken instead of static.
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe("reading", () => {
  it("hits the live path when there is a runtime", async () => {
    const spy = stubFetch(() => ok({ total: 140 }));
    setMode("live");
    await read("sweep", "sweep.json");
    expect(spy).toHaveBeenCalledWith("sweep", expect.anything());
  });

  it("hits the recording, relatively, when there is not", async () => {
    const spy = stubFetch(() => ok({ total: 140 }));
    setMode("recorded");
    await read("sweep", "sweep.json");
    // Relative, with no leading slash: a project site is served from
    // /<repo>/, and "/demo/sweep.json" would ask the domain root.
    expect(spy).toHaveBeenCalledWith("demo/sweep.json", expect.anything());
  });

  it("says which half failed", async () => {
    stubFetch(() => new Response("gone", { status: 404 }));
    setMode("recorded");
    await expect(read("sweep", "sweep.json")).rejects.toThrow(/recording/);
    setMode("live");
    await expect(read("sweep", "sweep.json")).rejects.toThrow(/runtime/);
  });
});
