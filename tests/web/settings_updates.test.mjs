// The settings page's Updates section - see voice_assistant/web/settings.js: every machine's
// version and state, the Update Nova button and when the last update was.
//
// The page's own script is run in a context of its own, over a small stand-in for the
// page: just enough of the DOM for what the block draws, and a fetch which keeps what was
// asked and answers as Nova would. Only small strings are compared, never the stand-in's
// nodes, as a failing comparison of those would print the whole tree.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SCRIPT = readFileSync(new URL("../../voice_assistant/web/settings.js", import.meta.url), "utf8");

// An element, as far as the block uses one.
class Node {
  constructor(tag) {
    this.tag = tag;
    this.text = "";
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }

  // A string is a piece of text, as in the page.
  append(...nodes) {
    this.children.push(...nodes.map((node) => {
      if (typeof node !== "string") return node;
      const text = new Node("#text");
      text.text = node;
      return text;
    }));
  }

  replaceChildren(...nodes) {
    this.text = "";
    this.children = nodes;
  }

  addEventListener(kind, listener) {
    (this.listeners[kind] ||= []).push(listener);
  }

  setAttribute() {}

  querySelector() {
    return null;
  }

  // Each of the listeners for a click, waited for.
  click() {
    return Promise.all((this.listeners.click || []).map((listener) => listener()));
  }
}

// The nodes of a tag under one, in order.
function all(node, tag) {
  return node.children.flatMap((child) => [...(child.tag === tag ? [child] : []), ...all(child, tag)]);
}

// The page's script over a stand-in page, with Nova answering /api/state with the state given.
function page(state = {}) {
  const nodes = new Map();
  const asked = [];
  const copied = [];
  const document = {
    hidden: false,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Node("div"));
      return nodes.get(id);
    },
    createElement: (tag) => new Node(tag),
    createElementNS: (namespace, tag) => new Node(tag),
    addEventListener() {},
  };
  const answer = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => "" });
  const context = vm.createContext({
    document,
    setTimeout,
    clearTimeout,
    AbortController,
    console,
    navigator: { clipboard: { writeText: async (text) => { copied.push(text); } } },
    async fetch(path, options = {}) {
      asked.push(`${options.method || "GET"} ${path}`);
      if (path === "/api/state") return answer(state);
      if (path === "/api/login-state") return answer({ route: "tailnet", password_set: false, logged_in: true });
      return answer({ success: true, message: "Updating. I'll be back in a minute." });
    },
  });
  vm.runInContext(SCRIPT, context);
  return { context, asked, copied, node: (id) => document.getElementById(id) };
}


const BRAIN = { version: "b".repeat(40), short: "bbbbbbb", date: "2026-11-26" };
const PI = { name: "pi", version: "a".repeat(40), short: "aaaaaaa", date: "2026-11-20", connected: true,
             state: "behind", reason: null, command: null };
const PC = { name: "pc", version: "c".repeat(40), short: "ccccccc", date: "2026-11-25", connected: false,
             state: "current", reason: null, command: null };
const NONE = { state: null, at: null, from: null, to: null, reason: null, command: null, needs_you: [] };
const DONE = { ...NONE, state: "done", at: new Date().toISOString() };

const updates = (changes = {}) => ({ versions: { brain: BRAIN, satellites: [PI, PC] }, last: DONE, running: false, ...changes });

// What a row says, as its own text and the code lines in it.
const rows = (shown) => all(shown.node("updates-machines"), "li");
const said = (row) => row.textContent;
const commands = (row) => all(row, "code").map((code) => code.textContent);

test("the brain is listed first, then each satellite by name, with its commit, date and state", () => {
  const shown = page();
  shown.context.showUpdates(updates());
  const lines = rows(shown).map(said);
  assert.equal(lines.length, 3);
  assert.match(lines[0], /^Brain/);
  assert.match(lines[0], /bbbbbbb/);
  assert.match(lines[0], /26 Nov/);
  assert.match(lines[0], /Up to date/);
  assert.match(lines[1], /^pi/);
  assert.match(lines[1], /aaaaaaa/);
  assert.match(lines[1], /20 Nov/);
  assert.match(lines[1], /Behind/);
  assert.match(lines[2], /^pc/);
  assert.match(lines[2], /Up to date/);
});

test("a satellite which is away says so beside the version it last had", () => {
  const shown = page();
  shown.context.showUpdates(updates());
  const away = said(rows(shown)[2]);
  assert.match(away, /Away/);
  assert.match(away, /ccccccc/);
  assert.doesNotMatch(said(rows(shown)[1]), /Away/);
});

test("each state of a satellite is said in words", () => {
  const shown = page();
  const row = (changes) => {
    shown.context.showUpdates(updates({ versions: { brain: BRAIN, satellites: [{ ...PI, ...changes }] } }));
    return rows(shown)[1];
  };
  assert.match(said(row({ state: "updating" })), /Updating/);
  assert.match(said(row({ state: "failed", reason: "It wouldn't start" })), /Failed: It wouldn't start/);
  assert.match(said(row({ state: "old" })), /Old: update by hand once/);
  const needs = row({ state: "needs_you", reason: "A package is missing", command: "pip install thing" });
  assert.match(said(needs), /Needs you/);
  assert.match(said(needs), /A package is missing/);
  assert.deepEqual(commands(needs), ["pip install thing"]);
});

test("the brain's row follows the last outcome, with each thing it needs as its own command line", () => {
  const shown = page();
  const brain = (last, running = false) => {
    shown.context.showUpdates(updates({ last, running }));
    return rows(shown)[0];
  };
  assert.match(said(brain(DONE)), /Up to date/);
  assert.match(said(brain(NONE)), /Up to date/);
  assert.match(said(brain({ ...DONE, state: "failed", reason: "Tests failed", command: null })), /Failed: Tests failed/);
  assert.match(said(brain(DONE, true)), /Updating/);
  const needs = brain({ ...DONE, state: "needs_you", reason: "New package", command: "sudo apt install x",
                         needs_you: [{ why: "New package", command: "sudo apt install x" },
                                     { why: "New unit", command: "sudo systemctl enable y" }] });
  assert.match(said(needs), /Needs you/);
  assert.deepEqual(commands(needs), ["sudo apt install x", "sudo systemctl enable y"]);
});

test("what satellites say is drawn as text, never as markup", () => {
  const nasty = '<img src=x onerror="alert(1)">';
  const shown = page();
  shown.context.showUpdates(updates({ versions: { brain: BRAIN, satellites: [
    { ...PI, name: nasty, state: "failed", reason: nasty },
    { ...PC, name: "pc", state: "needs_you", reason: nasty, command: nasty }] } }));
  const list = shown.node("updates-machines");
  assert.equal(all(list, "img").length, 0);
  assert.ok(said(rows(shown)[1]).includes(nasty));
  assert.deepEqual(commands(rows(shown)[2]), [nasty]);
});

test("a command line's Copy puts the command on the clipboard", async () => {
  const shown = page();
  shown.context.showUpdates(updates({ versions: { brain: BRAIN, satellites: [
    { ...PI, state: "needs_you", reason: "Why", command: "pip install thing" }] } }));
  const copy = all(rows(shown)[1], "button")[0];
  assert.equal(copy.textContent, "Copy");
  await copy.click();
  assert.deepEqual(shown.copied, ["pip install thing"]);
  assert.equal(copy.textContent, "Copied");
});

test("Last updated gives the time as the page gives others, and nothing without an outcome", () => {
  const shown = page();
  const when = new Date();
  shown.context.showUpdates(updates({ last: { ...DONE, at: when.toISOString() } }));
  const at = when.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  assert.equal(shown.node("updates-last").hidden, false);
  assert.equal(shown.node("updates-last").textContent, `Last updated today at ${at}`);
  shown.context.showUpdates(updates({ last: NONE }));
  assert.equal(shown.node("updates-last").hidden, true);
  assert.equal(shown.node("updates-last").textContent, "");
});

test("the button is Update Nova, and while one runs it is disabled and says Updating...", () => {
  const shown = page();
  shown.context.showUpdates(updates());
  assert.equal(shown.node("updates-button").textContent, "Update Nova");
  assert.equal(shown.node("updates-button").disabled, false);
  shown.context.showUpdates(updates({ running: true }));
  assert.equal(shown.node("updates-button").textContent, "Updating...");
  assert.equal(shown.node("updates-button").disabled, true);
});

test("pressing it sends the update, says what Nova answered and looks again", async () => {
  const shown = page();
  // The whole page's drawing isn't what is tried here, so a look at the state is only noted.
  shown.context.load = async () => { shown.asked.push("look"); };
  shown.context.setUpUpdates();
  await shown.node("updates-button").click();
  assert.deepEqual(shown.asked, ["POST /api/update", "look"]);
  assert.equal(shown.node("toast").textContent, "Updating. I'll be back in a minute.");
});

test("the state is looked at every 3 seconds while an update runs, and every 20 otherwise", () => {
  const shown = page();
  assert.equal(shown.context.stateEvery(), 20000);
  shown.context.showUpdates(updates({ running: true }));
  assert.equal(shown.context.stateEvery(), 3000);
  shown.context.showUpdates(updates());
  assert.equal(shown.context.stateEvery(), 20000);
});

test("with nothing said of updates the section shows no machines and no button", () => {
  const shown = page();
  shown.context.showUpdates(null);
  assert.equal(rows(shown).length, 0);
  assert.equal(shown.node("updates-button").disabled, true);
});

test("an unknown version says so, as short() gives \"unknown\" and never nothing", () => {
  const shown = page();
  shown.context.showUpdates(updates({ versions: { brain: BRAIN, satellites: [
    { ...PI, version: null, short: "unknown", date: null }] } }));
  assert.match(said(rows(shown)[1]), /Version not known/);
  assert.doesNotMatch(said(rows(shown)[1]), /unknown/);
});

test("a done update which left things for him still shows the brain as needing him, each command copyable", () => {
  const shown = page();
  shown.context.showUpdates(updates({ last: { ...DONE, needs_you: [
    { why: "New unit", command: "sudo systemctl enable y" }, { why: "New package", command: "sudo apt install x" }] } }));
  const brain = rows(shown)[0];
  assert.match(said(brain), /Needs you/);
  assert.deepEqual(commands(brain), ["sudo systemctl enable y", "sudo apt install x"]);
  // It did restart into the new code, so that was an update.
  assert.match(shown.node("updates-last").textContent, /^Last updated /);
});

test("an update which didn't start says so with its reason, and the time is Last tried", () => {
  const shown = page();
  const busy = { ...DONE, state: "busy", reason: "Nova was busy for 10 minutes, so the update didn't start." };
  shown.context.showUpdates(updates({ last: busy }));
  assert.match(said(rows(shown)[0]), /Didn't start: Nova was busy for 10 minutes/);
  assert.doesNotMatch(said(rows(shown)[0]), /Up to date/);
  assert.match(shown.node("updates-last").textContent, /^Last tried /);
  shown.context.showUpdates(updates({ last: { ...DONE, state: "failed", reason: "Broke" } }));
  assert.match(shown.node("updates-last").textContent, /^Last tried /);
  shown.context.showUpdates(updates({ last: { ...DONE, state: "needs_you", reason: "x", command: "c",
                                              needs_you: [{ why: "x", command: "c" }] } }));
  assert.match(shown.node("updates-last").textContent, /^Last tried /);
  shown.context.showUpdates(updates({ last: { ...DONE, state: "current" } }));
  assert.match(shown.node("updates-last").textContent, /^Last updated /);
});

test("the list reads the brain as needing him when its update left things for him, whatever the state", () => {
  const shown = page();
  const machines = (last) => JSON.parse(JSON.stringify(shown.context.updateMachines(updates({ last }))));
  const left = { ...DONE, needs_you: [{ why: "New unit", command: "sudo systemctl enable y" }] };
  assert.equal(machines(left)[0].state, "needs_you");
  assert.equal(machines(DONE)[0].state, "current");
  assert.equal(machines({ ...DONE, state: "busy", reason: "Busy" })[0].state, "busy");
});
