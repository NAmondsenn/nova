// The settings page's Clean songs block - see voice_assistant/web/settings.js and
// docs/decisions/0022.
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

  append(...nodes) {
    this.children.push(...nodes);
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

// The page's script over a stand-in page, with Nova answering /api/clean with the state given.
function page(state, { confirmed = true } = {}) {
  const nodes = new Map();
  const asked = [];
  const confirms = [];
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
    confirm(question) {
      confirms.push(question);
      return confirmed;
    },
    async fetch(path, options = {}) {
      asked.push(`${options.method || "GET"} ${path}`);
      return answer(path === "/api/clean" ? state : { success: true, message: "Done." });
    },
  });
  vm.runInContext(SCRIPT, context);
  return { context, asked, confirms, node: (id) => document.getElementById(id) };
}

const CHECKING = {
  running: true, phase: "checking", done: 111, total: 398, current: "Gunna - endless", last_finished: null,
  paused: false, error: null, held: 0, swapped: [], only_clean: [], unsure: [], couldnt: [],
};

test("a running check says how far it's got, and only Stop can be pressed", async () => {
  const shown = page(CHECKING);
  await shown.context.loadClean();
  assert.equal(shown.node("clean-progress").textContent, "Checking 112 of 398: Gunna - endless");
  assert.equal(shown.node("clean-start").disabled, true);
  assert.equal(shown.node("clean-stop").hidden, false);
  assert.equal(shown.node("clean").hidden, false);
});

test("a check listing the library says so", async () => {
  const shown = page({ ...CHECKING, phase: "listing", done: 0, total: 0, current: null });
  await shown.context.loadClean();
  assert.equal(shown.node("clean-progress").textContent, "Listing the library");
});

test("a paused check says why, and can be carried on or dropped", async () => {
  const why = "Navidrome couldn't be reached, so the check stopped here and carries on next time.";
  const shown = page({ ...CHECKING, running: false, phase: null, paused: true, error: why, current: null });
  await shown.context.loadClean();
  assert.equal(shown.node("clean-progress").textContent, `Paused: ${why}`);
  assert.equal(shown.node("clean-start").disabled, false);
  assert.equal(shown.node("clean-start").textContent, "Carry on");
  assert.equal(shown.node("clean-stop").hidden, false);
  // Said once, rather than again as why a check ended.
  assert.equal(shown.node("clean-error").hidden, true);
});

test("with no check running it says when the last finished, or never", async () => {
  const idle = { ...CHECKING, running: false, phase: null, current: null };
  const never = page({ ...idle, last_finished: null });
  await never.context.loadClean();
  assert.equal(never.node("clean-progress").textContent, "Last check: never");
  assert.equal(never.node("clean-stop").hidden, true);
  assert.equal(never.node("clean-start").textContent, "Check library");

  const finished = new Date();
  const today = page({ ...idle, last_finished: finished.getTime() / 1000 });
  await today.context.loadClean();
  const at = finished.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  assert.equal(today.node("clean-progress").textContent, `Last check: today at ${at}`);
});

test("a check which ended early says why under when the last finished", async () => {
  const why = "Something went wrong part way through the check, so it stopped.";
  const shown = page({ ...CHECKING, running: false, phase: null, current: null, error: why });
  await shown.context.loadClean();
  assert.equal(shown.node("clean-progress").textContent, "Last check: never");
  assert.equal(shown.node("clean-error").hidden, false);
  assert.equal(shown.node("clean-error").textContent, why);
});

test("each list with a song in it is shown, its songs by name with why", async () => {
  const shown = page({
    ...CHECKING,
    swapped: [{ name: "Travis Scott - SICKO MODE", reason: "", song_id: "d1" }],
    couldnt: [{ name: "Gunna - endless", reason: "SoulSync found nothing", song_id: "e1" },
              { name: "Artist - Two", reason: "Nova can't write to the music folder" }],
  });
  await shown.context.loadClean();
  const lists = shown.node("clean-lists");
  // Under the block's own h3, "Clean songs".
  assert.equal(all(lists, "h4").map((heading) => heading.textContent).join(" | "), "Clean and swapped | Couldn't swap");
  assert.equal(all(lists, "h3").length, 0);
  const rows = all(lists, "li").map((row) => all(row, "span").filter((span) => span.text).map((span) => span.text).join(" / "));
  assert.equal(rows.join(" | "),
    "Travis Scott - SICKO MODE | Gunna - endless / SoulSync found nothing | Artist - Two / Nova can't write to the music folder");
});

test("the holding folder's count is shown once there's something in it", async () => {
  const none = page(CHECKING);
  await none.context.loadClean();
  assert.equal(none.node("clean-held").hidden, true);

  const one = page({ ...CHECKING, held: 1 });
  await one.context.loadClean();
  assert.equal(one.node("clean-held").hidden, false);
  assert.equal(one.node("clean-held").textContent, "Holding folder: 1 file waiting for you to empty");

  const three = page({ ...CHECKING, held: 3 });
  await three.context.loadClean();
  assert.equal(three.node("clean-held").textContent, "Holding folder: 3 files waiting for you to empty");
});

test("Check library and Stop send the check and the stop, then look again", async () => {
  const shown = page({ ...CHECKING, running: false, phase: null });
  shown.context.setUpClean();
  await shown.node("clean-start").click();
  await shown.node("clean-stop").click();
  assert.equal(shown.asked.join(", "), "POST /api/clean/check, GET /api/clean, POST /api/clean/stop, GET /api/clean");
  assert.equal(shown.confirms.length, 0);
});

test("Stop asks first before dropping a paused check, and sends nothing if not", async () => {
  const paused = { ...CHECKING, running: false, phase: null, paused: true, error: "Paused." };
  const kept = page(paused, { confirmed: false });
  kept.context.setUpClean();
  await kept.context.loadClean();
  await kept.node("clean-stop").click();
  assert.equal(kept.asked.join(", "), "GET /api/clean");
  assert.equal(kept.confirms.length, 1);

  const dropped = page(paused);
  dropped.context.setUpClean();
  await dropped.context.loadClean();
  await dropped.node("clean-stop").click();
  assert.equal(dropped.asked.join(", "), "GET /api/clean, POST /api/clean/stop, GET /api/clean");
});

test("the check is looked at every 10 seconds while it runs, and every minute otherwise", async () => {
  const before = page(CHECKING);
  // Nothing known yet: the first look comes at once, then at the slower pace unless it's running.
  assert.equal(before.context.cleanEvery(), 60000);
  await before.context.loadClean();
  assert.equal(before.context.cleanEvery(), 10000);

  const idle = page({ ...CHECKING, running: false, phase: null, current: null });
  await idle.context.loadClean();
  assert.equal(idle.context.cleanEvery(), 60000);

  const paused = page({ ...CHECKING, running: false, phase: null, paused: true, error: "Paused." });
  await paused.context.loadClean();
  assert.equal(paused.context.cleanEvery(), 60000);
});
