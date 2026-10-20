/**
 * Run the console's own <script> against a stub DOM and a live server.
 *
 *   node scripts/check_console.mjs http://127.0.0.1:8099
 *
 * Why this and not a browser: Playwright is a 300 MB dependency to answer one
 * question -- does the page's logic actually render the server's responses? --
 * and that question is answerable by giving the real script a fake document
 * and a real fetch. It has earned its keep: it caught `r.append(x).append(y)`,
 * which reads fine and throws in a browser because Element.append returns
 * undefined, and it catches any renamed API field immediately.
 *
 * What it does not check: layout, colour, animation. Those need eyes.
 */
import { readFile, writeFile, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const base = (process.argv[2] || "http://127.0.0.1:8099").replace(/\/$/, "");

let checks = 0;
let failures = 0;
function ok(label, condition, detail = "") {
  checks += 1;
  if (condition) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}${detail ? " -- " + detail : ""}`);
  }
}

/* ---------------------------------------------------------------- stub DOM */

class StubNode {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this._classes = new Set();
    this.dataset = {};
    this.style = {};
    this._text = "";
    this._listeners = new Map();
    this.value = "";
    this._selectedIndex = -1;
    this.innerHTML = "";
    this.disabled = false;
    this.title = "";
    this.type = "";
    this.id = "";
    this.max = "0";
    this.min = "0";
  }
  get children() { return this.childNodes.filter(n => n instanceof StubNode); }
  // A real <select> exposes its appended <option> children as .options and
  // keeps .value in step with .selectedIndex; the page relies on both, so the
  // stub has to as well or it reports a populated picker as empty.
  get options() { return this.children.filter(n => n.tagName === "OPTION"); }
  get selectedIndex() { return this._selectedIndex; }
  set selectedIndex(i) {
    this._selectedIndex = i;
    const opt = this.options[i];
    if (opt) this.value = opt.value;
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) {
    this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get classList() {
    const s = this._classes;
    return {
      add: (...c) => c.forEach(x => x && s.add(x)),
      remove: (...c) => c.forEach(x => s.delete(x)),
      contains: c => s.has(c),
      toggle: (c, on) => (on === undefined ? (s.has(c) ? s.delete(c) : s.add(c))
        : (on ? s.add(c) : s.delete(c))),
    };
  }
  get textContent() {
    if (this.childNodes.length === 0) return this._text;
    return this._text + this.childNodes
      .map(n => (n instanceof StubNode ? n.textContent : String(n))).join("");
  }
  set textContent(v) {
    this._text = v === null || v === undefined ? "" : String(v);
    this.childNodes = [];
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n instanceof StubNode) { n.parentNode = this; this.childNodes.push(n); }
      else this.childNodes.push(String(n));
    }
    // Deliberately returns undefined, exactly like the real thing.
  }
  appendChild(n) { this.append(n); return n; }
  remove() {
    if (this.parentNode) {
      this.parentNode.childNodes = this.parentNode.childNodes.filter(n => n !== this);
    }
  }
  setAttribute(k, v) { this[k] = v; }
  getAttribute(k) { return this[k]; }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  dispatch(type, event = {}) {
    const fns = this._listeners.get(type) || [];
    return Promise.all(fns.map(f => f({ preventDefault() {}, target: this, ...event })));
  }
  click() { return this.dispatch("click"); }
  walk(out = []) {
    out.push(this);
    for (const c of this.children) c.walk(out);
    return out;
  }
  matches(sel) {
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    if (sel.startsWith(".")) {
      return sel.slice(1).split(".").every(c => this._classes.has(c));
    }
    const [tag, rest] = sel.split(/(?=[.#\[])/, 2);
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (rest) return this.matches(rest);
    return true;
  }
  querySelectorAll(sel) {
    const parts = sel.split(",").map(s => s.trim()).filter(Boolean);
    return this.walk().slice(1).filter(n => parts.some(p => n.matches(p.split(" ").pop())));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function buildDocument(ids) {
  const doc = new StubNode("body");
  doc.documentElement = new StubNode("html");
  doc.documentElement.dataset = { theme: "dark" };
  const registry = new Map();
  for (const id of ids) {
    const n = new StubNode(id === "input" ? "textarea" : "div");
    n.id = id;
    registry.set(id, n);
    doc.append(n);
  }
  doc.createElement = tag => new StubNode(tag);
  doc.createTextNode = t => String(t);
  const bodyQuery = doc.querySelector.bind(doc);
  doc.querySelector = sel => (sel.startsWith("#") && registry.has(sel.slice(1))
    ? registry.get(sel.slice(1))
    : bodyQuery(sel));
  doc.addEventListener = () => {};
  doc.body = doc;
  return doc;
}

/* ------------------------------------------------------------------- setup */

const html = await readFile(join(here, "..", "web", "index.html"), "utf8");
const match = html.match(/<script type="module">([\s\S]*?)<\/script>/);
if (!match) {
  console.error("no module script found in web/index.html");
  process.exit(1);
}

// Every id the script reaches for by #id. Listing them here means a renamed
// element in the HTML shows up as a missing-element failure rather than as a
// silent no-op.
const IDS = [
  "chips", "stats", "wfs", "input", "boundary", "dice", "bhint", "go", "breakit",
  "breakfix", "runid", "status", "cursor", "pos", "tape", "readout", "output",
  "banner", "resume", "verify", "download", "runs", "ledger", "recover",
  "runsweep", "swstats", "matrix", "toasts", "theme", "tape-card", "launch-card",
  "sweep-card",
];

const document = buildDocument(IDS);
globalThis.document = document;
globalThis.window = {
  __REVENANT_NO_BOOT: true,
  matchMedia: () => ({ matches: false }),
  open: () => {},
  addEventListener: () => {},
};
globalThis.localStorage = {
  _d: new Map(),
  getItem(k) { return this._d.has(k) ? this._d.get(k) : null; },
  setItem(k, v) { this._d.set(k, String(v)); },
};
// Relative paths in the page resolve against the live server. Capture the
// real fetch first: overwriting the global and then reading it back would
// hand the page a function that calls itself.
const realFetch = globalThis.fetch;
globalThis.fetch = (path, opts) =>
  realFetch(String(path).startsWith("http") ? path : base + String(path), opts);

const dir = await mkdtemp(join(tmpdir(), "revenant-console-"));
const modPath = join(dir, "console.mjs");
await writeFile(modPath, match[1], "utf8");
await import(pathToFileURL(modPath).href);

const api = globalThis.window.__revenant;
ok("the script exposed its harness hooks", !!api);

/* ------------------------------------------------------------------ checks */

console.log(`\nbooting against ${base}`);
await api.boot();

const state = api.state;
ok("workflows loaded", state.workflows.length === 3,
  JSON.stringify(state.workflows.map(w => w.name)));
ok("every workflow reported crash boundaries",
  state.workflows.every(w => w.boundaries.length >= 30));
ok("a workflow is selected and its sample input is in the textarea",
  !!state.selected && document.querySelector("#input").value.includes("{"),
  document.querySelector("#input").value.slice(0, 40));
ok("the boundary picker was populated",
  document.querySelector("#boundary").options.length >= 30);
ok("the boundary hint explains the chosen hook",
  /before_append|after_append|before_effect|after_effect/.test(
    document.querySelector("#bhint").textContent));
ok("the stat strip rendered four tiles",
  document.querySelector("#stats").children.length === 4);
ok("the store chip is present",
  document.querySelector("#chips").textContent.includes("store"));

// -- a clean run ----------------------------------------------------------
api.selectWorkflow("onboarding");
await api.startRun({});
ok("a clean run loaded into the tape", !!state.run && state.run.status === "completed");
const rows = document.querySelectorAll(".ev");
ok("every event got a row", rows.length === state.run.timeline.length,
  `${rows.length} rows vs ${state.run.timeline.length} events`);
ok("rows carry their group class",
  rows.every(r => /g-(decision|observation|intent|lifecycle)/.test(r.className)));
ok("at least one costly effect is marked",
  rows.some(r => r._classes.has("costly")));
ok("the readout filled in", document.querySelector("#readout").children.length === 8);
ok("the output panel shows the run's output",
  document.querySelector("#output").textContent.includes("account_id"));
ok("the status pill matches the run",
  document.querySelector("#status").textContent === "completed");

// -- the fold is the replay, so check it agrees with the end state --------
const whole = api.foldTo(state.run.timeline, state.run.timeline.length);
ok("the fold counts four applied effects", whole.applied === 4, String(whole.applied));
ok("the fold sees no duplicate application", whole.deduped === 0);
const half = api.foldTo(state.run.timeline, 6);
ok("the fold at the cursor is a prefix, not the whole run",
  half.applied < whole.applied);

// -- the cursor -----------------------------------------------------------
api.setCursor(5);
ok("the cursor position is reported", document.querySelector("#pos").textContent.startsWith("5 /"));
ok("events past the cursor are dimmed",
  document.querySelectorAll(".ev").filter(r => r._classes.has("beyond")).length > 0);
ok("exactly one row is the cursor",
  document.querySelectorAll(".ev").filter(r => r._classes.has("cursor")).length === 1);
api.setCursor(state.run.timeline.length);

// -- verify ---------------------------------------------------------------
await api.verifyRun();
ok("verification reported a byte-identical replay",
  /byte-for-byte/.test(document.querySelector("#banner").textContent),
  document.querySelector("#banner").textContent);

// -- crash and recover ----------------------------------------------------
api.selectWorkflow("research");
const picker = document.querySelector("#boundary");
const afterEffect = state.workflows.find(w => w.name === "research")
  .boundaries.find(b => b.hook === "after_effect");
picker.value = String(afterEffect.index);
await api.startRun({ crash: true });
ok("the crashed run is interrupted", state.run.status === "interrupted");
ok("the tape shows the crash", /stops mid-run/.test(document.querySelector("#banner").textContent));

await api.resumeRun();
ok("the resumed run completed", state.run.status === "completed");
ok("the resume is drawn as a tear in the tape",
  document.querySelectorAll(".tear").length === 1);
ok("the resume banner reports replayed events",
  /events replayed from the log/.test(document.querySelector("#banner").textContent));
const dedupeRows = document.querySelectorAll(".ev").filter(r =>
  r.textContent.includes("deduplicated"));
ok("the effect that landed before the crash shows as deduplicated",
  dedupeRows.length >= 1, `${dedupeRows.length} rows`);

await api.verifyRun();
ok("the recovered run also replays byte-identically",
  /byte-for-byte/.test(document.querySelector("#banner").textContent));

// -- ledger ---------------------------------------------------------------
await api.refreshLedger();
ok("the ledger says every key applied exactly once",
  /each applied exactly once/.test(document.querySelector("#ledger").textContent),
  document.querySelector("#ledger").textContent.slice(0, 120));

// -- the sweep ------------------------------------------------------------
await api.runSweep();
const cells = document.querySelectorAll(".cell");
ok("the matrix has one cell per scenario", cells.length === 140, String(cells.length));
ok("no cell failed", cells.filter(c => c._classes.has("fail")).length === 0);
ok("some cells mark the dedupe window",
  cells.filter(c => c._classes.has("dedupe")).length > 0);
ok("three cells mark the pre-durable boundary",
  cells.filter(c => c._classes.has("none")).length === 3,
  String(cells.filter(c => c._classes.has("none")).length));
ok("the sweep stats rendered",
  document.querySelector("#swstats").children.length === 5);

console.log(`\n${checks - failures}/${checks} assertions passed`);
process.exit(failures ? 1 : 0);
