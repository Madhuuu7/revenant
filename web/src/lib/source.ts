/* Where the console gets its data when there is no runtime behind it.
 *
 * The console is published two ways. Served by the runtime it talks to a
 * live process and the sweep button really kills things. Published as a
 * static page it has no backend at all, so it falls back to recordings
 * taken from a real run of that same runtime.
 *
 * This is honest for revenant specifically, and it is worth being precise
 * about why rather than waving at it: the crash sweep is deterministic. It
 * kills the process at every journal boundary in a fixed order, with
 * in-memory journals and seeded effects, so the 140 results are the same
 * on every machine and every run. A recording is therefore the output, not
 * an illustration of it. The one thing the static page genuinely cannot do
 * is run the sweep *now*, and it says so rather than pretending the button
 * worked.
 *
 * Probe once, cache the answer. Retrying a dead endpoint on every click
 * would make the static page feel broken rather than static.
 */

export type Mode = "live" | "recorded";

let resolved: Mode | null = null;
// The promise, not only the answer. Every view asks on first paint, and
// they all ask before any of them has an answer to cache -- so a page with
// six panels fired six probes at a dead endpoint and left six 404s in the
// console of anyone who opened it.
let asking: Promise<Mode> | null = null;

/** Ask the runtime whether it is there. */
export async function detect(): Promise<Mode> {
  if (resolved) return resolved;
  if (!asking) {
    asking = (async () => {
      try {
        const response = await fetch("health", {
          headers: { accept: "application/json" },
          signal: AbortSignal.timeout(4000),
        });
        resolved = response.ok ? "live" : "recorded";
      } catch {
        resolved = "recorded";
      }
      asking = null;
      return resolved;
    })();
  }
  return asking;
}

/** Force a mode, for tests. */
export function setMode(mode: Mode | null): void {
  resolved = mode;
  asking = null;
}

/**
 * Fetch from the runtime, or from the recording if there is no runtime.
 *
 * Paths are relative so the page works under a sub-path -- GitHub Pages
 * serves a project site from `/<repo>/`, and a leading slash would send
 * every request to the domain root.
 */
export async function read<T>(live: string, recorded: string): Promise<T> {
  const mode = await detect();
  const path = mode === "live" ? live : `demo/${recorded}`;
  const response = await fetch(path, { headers: { accept: "application/json" } });
  if (!response.ok) {
    throw new Error(
      mode === "live"
        ? `the runtime answered ${response.status} for ${live}`
        : `the recording ${recorded} is missing from this build`,
    );
  }
  return (await response.json()) as T;
}
