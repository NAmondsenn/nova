// The settings page's list of sections, and the one section open at a time - see
// voice_assistant/web/settings.js and settings.html.
//
// The page's scripts are run in a context of their own, settings_list.js then settings.js, over
// a small stand-in for the page: just enough of the DOM, the address and the history for what
// opening and closing a section do, and a fetch which answers as Nova would. Only small
// strings, numbers and booleans are compared, never the stand-in's nodes, as a failing
// comparison of those would print the whole tree.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const web = (name) => readFileSync(new URL(`../../voice_assistant/web/${name}`, import.meta.url), "utf8");
const LIST = web("settings_list.js");
const SCRIPT = web("settings.js");
const HTML = web("settings.html");

// What has the focus, as the stand-in's page has it: the last node focused.
let focused = null;

// An element, as far as the page's script uses one.
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
    this.attributes = new Map();
    this.classes = new Set();
    const classes = this.classes;
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      },
    };
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

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }

  focus() {
    focused = this;
  }

  // Each of the listeners for a click, waited for.
  click() {
    return Promise.all((this.listeners.click || []).map((listener) => listener()));
  }
}

// Every node under one, in order.
function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

const SECTION_IDS = ["status", "music", "clock", "devices", "batteries", "displays", "journeys", "football",
                     "calendars", "routines", "satellites", "approvals", "updates", "sound", "claude", "conversation", "notifications", "memory", "keys",
                     "else"];

// What /api/state says, with nothing in it.
const STATE = {
  wake_word: "Hey Nova", muted: false, satellites: [], satellites_unreadable: false, satellite: null,
  status: { reply_ms: null, replies: 0, started: 1700000000, speech_to_text: "ready", ntfy: null, problems: [] },
  volumes: {}, clock: { timers: [], alarms: [] }, memories: [], routines: [],
  displays: { port: 0, list: [] }, keys: [], settings: [], settings_problem: "", restart_needed: false, page: "one",
};

// What /api/approvals says: one passkey, the PC which can approve, and one approval.
const APPROVALS = {
  passkeys: [{ id: "AQID", name: "Phone", added: 1700000000 }],
  satellites: [{ name: "pc", spoken: "the PC", connected: true, can_approve: true }],
  trusted_until: null,
  recent: [{ at: 1700000600, description: "Remove 'Dentist'", by: "Phone", outcome: "approved" }],
};

// The page's scripts over a stand-in page, at the address `hash` (whose history entry has the
// state `entry`, as a reloaded one does) and logged in or, with `loggedIn` false, through NPM
// and not yet; on a wide screen if `wide`. `before` are the history's entries under this one,
// each { state, hash }, which belong to another load of a document - going back to one is a
// whole new load, not a popstate. `state` is what Nova says of itself.
function page({ hash = "", entry = null, before = [], loggedIn = true, wide = false, state = STATE, route = null,
                approvals = APPROVALS, post = null } = {}) {
  const nodes = new Map();
  const document = {
    hidden: false,
    body: new Node("body"),
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Node("div"));
      return nodes.get(id);
    },
    createElement: (tag) => new Node(tag),
    createElementNS: (namespace, tag) => new Node(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  };
  // The address and the history, as the browser keeps them: the entries, which is the
  // current one, and what the page did to them, in words.
  const location = { hash, pathname: "/settings", search: "" };
  const entries = [...before.map((one) => ({ ...one, other: true })), { state: entry, hash }];
  let at = before.length;
  const done = [];
  const hashOf = (url) => (url.includes("#") ? url.slice(url.indexOf("#")) : "");
  const history = {
    get state() {
      return entries[at].state;
    },
    pushState(pushed, title, url) {
      entries.splice(at + 1, entries.length, { state: pushed, hash: hashOf(url) });
      at += 1;
      location.hash = hashOf(url);
      done.push(`push ${url}`);
    },
    replaceState(replaced, title, url) {
      entries[at] = { state: replaced, hash: hashOf(url) };
      location.hash = hashOf(url);
      done.push(`replace ${url}`);
    },
    // The browser answers a back a moment later, as popstate: see arrive().
    back() {
      done.push("back");
    },
  };
  const windowListeners = {};
  const scrolls = [];
  const window = {
    scrollY: 0,
    matchMedia: () => ({ matches: wide, addEventListener() {} }),
    scrollTo(left, top) {
      scrolls.push(top);
      window.scrollY = top;
    },
    addEventListener(kind, listener) {
      (windowListeners[kind] ||= []).push(listener);
    },
    removeEventListener(kind, listener) {
      windowListeners[kind] = (windowListeners[kind] || []).filter((one) => one !== listener);
    },
  };
  const login = { route: route || (loggedIn ? "direct" : "npm"), logged_in: loggedIn, password_set: true, page: "one" };
  const answers = { state, approvals };
  // Each path the page asked Nova for, in order.
  const asked = [];
  const answer = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => "" });
  const context = vm.createContext({
    document,
    window,
    location,
    history,
    setTimeout,
    clearTimeout,
    AbortController,
    console,
    Option: class extends Node {
      constructor(text, value) {
        super("option");
        this.textContent = text;
        this.value = value;
      }
    },
    async fetch(path, options = {}) {
      asked.push(path);
      // A change, answered by the test's own `post` where it has one.
      if (options.method === "POST" && post) return post(path, options, answer);
      if (path === "/api/login-state") return answer(login);
      if (path === "/api/state") return answer(answers.state);
      if (path === "/api/approvals") return answer(answers.approvals);
      return answer({});
    },
  });
  vm.runInContext(LIST, context);
  vm.runInContext(SCRIPT, context);
  focused = null;
  context.setUpList();
  const node = (id) => document.getElementById(id);
  return {
    context,
    node,
    body: document.body,
    done,
    scrolls,
    login,
    answers,
    asked,
    // The row of a section in the list.
    row: (id) => descendants(node("rows")).find((one) => one.dataset.open === id),
    // Whether the focus is on the node of that id, or on a row.
    focusedOn: (id) => focused === node(id),
    focusedOnRow(id) {
      return focused === this.row(id);
    },
    // The browser going back one entry, and telling the page - or, at an entry of another load,
    // loading that afresh, which the page is never told of and loses everything unsaved to.
    arrive() {
      at -= 1;
      location.hash = entries[at].hash;
      if (entries[at].other) {
        done.push("full load");
        return;
      }
      for (const listener of windowListeners.popstate || []) listener({});
    },
    // An address in this document typed, or gone Forward to: a new entry with the state
    // given, which the browser tells the page of as it does a Back.
    visit(to, state = null) {
      entries.splice(at + 1, entries.length, { state, hash: to });
      at += 1;
      location.hash = to;
      for (const listener of windowListeners.popstate || []) listener({});
    },
    // An event the browser sends the window.
    fire(kind, event = {}) {
      for (const listener of windowListeners[kind] || []) listener(event);
    },
    // Each entry's state, in words: "none", "ours" with this load's mark, or "other".
    written: () => entries.map((one) => (one.state == null ? "none"
      : one.state.nova === vm.runInContext("PAGE_LOAD", context) ? "ours" : "other")).join(","),
    // The sections which are open: they've the class which CSS shows them by.
    shown: () => SECTION_IDS.filter((id) => node(`section-${id}`).classes.has("open")).join(","),
  };
}

// What the page does as it is shown, logged in: its first look at Nova.
async function arrived(shown) {
  await shown.context.load();
}

test("there's one row for each section, in its group, with its name and an empty line", () => {
  const shown = page();
  const rows = descendants(shown.node("rows")).filter((one) => one.dataset.open);
  assert.equal(rows.map((one) => one.dataset.open).join(","), SECTION_IDS.join(","));
  assert.equal(shown.row("music").tag, "button");
  assert.equal(shown.row("music").textContent, "Music");
  assert.equal(shown.node("line-music").textContent, "");
  // Grouped under the group's name: Now, Home, Daily, Nova.
  const names = descendants(shown.node("rows")).filter((one) => one.tag === "h3").map((one) => one.textContent);
  assert.equal(names.join(","), "Now,Home,Daily,Nova");
});

test("opening a row opens its section alone, pushes one entry and focuses its heading", async () => {
  const shown = page();
  await arrived(shown);
  await shown.row("music").click();
  assert.equal(shown.body.dataset.open, "music");
  assert.equal(shown.shown(), "music");
  assert.equal(shown.done.join("|"), "push #music");
  assert.equal(shown.scrolls.join(","), "0");
  assert.equal(shown.focusedOn("music-title"), true);
});

test("Back closes the section: the list as it was scrolled to, the focus on the section's row", async () => {
  const shown = page();
  await arrived(shown);
  shown.context.window.scrollY = 340;
  await shown.row("music").click();
  assert.equal(shown.scrolls.join(","), "0");
  // Its back button goes back in the history, as the section was pushed there.
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "push #music|back");
  shown.arrive();
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.shown(), "");
  assert.equal(shown.scrolls.join(","), "0,340");
  assert.equal(shown.focusedOnRow("music"), true);
  // Nothing more was pushed or replaced by going back.
  assert.equal(shown.done.join("|"), "push #music|back");
});

test("the browser's own Back, with no section in the address, closes it the same way", async () => {
  const shown = page();
  await arrived(shown);
  shown.context.window.scrollY = 120;
  await shown.row("devices").click();
  shown.arrive();
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.scrolls.join(","), "0,120");
  assert.equal(shown.focusedOnRow("devices"), true);
  // Not as the page's own doing: it pushed once and never went back itself.
  assert.equal(shown.done.join("|"), "push #devices");
});

test("loading at #music opens it above the bare page, so Back goes to the list and #back goes back", async () => {
  const shown = page({ hash: "#music" });
  assert.equal(shown.body.dataset.open, undefined, "nothing opens until the page is let in");
  await arrived(shown);
  assert.equal(shown.body.dataset.open, "music");
  assert.equal(shown.shown(), "music");
  // The link's entry made the bare page, with the section pushed over it.
  assert.equal(shown.done.join("|"), "replace /settings|push #music");
  assert.equal(shown.focusedOn("music-title"), true);
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "replace /settings|push #music|back");
  shown.arrive();
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.focusedOnRow("music"), true);
});

test("a reload inside a section is taken as a link, so Back stays in the page and keeps what's unsaved", async () => {
  // The entry under the section's is the list of the load before the reload: going back to it would
  // load the page afresh. Another load's mark, and the mark of the page before this one, alike.
  for (const entry of [{ nova: "0123abcd" }, { nova: true }]) {
    const shown = page({ hash: "#music", entry, before: [{ state: null, hash: "" }] });
    await arrived(shown);
    assert.equal(shown.body.dataset.open, "music");
    assert.equal(shown.done.join("|"), "replace /settings|push #music");
    // The old list's entry, then this load's bare page under its own section's.
    assert.equal(shown.written(), "none,none,ours");
    await shown.node("back").click();
    shown.arrive();
    assert.equal(shown.done.join("|"), "replace /settings|push #music|back");
    assert.equal(shown.body.dataset.open, undefined);
    assert.equal(shown.focusedOnRow("music"), true);
  }
});

test("a tap with a section open marks the entry it takes the place of as this load's", async () => {
  const shown = page({ wide: true });
  await arrived(shown);
  await shown.row("music").click();
  await shown.row("clock").click();
  assert.equal(shown.written(), "none,ours");
});

test("'All of Nova' goes back only to an entry of this load's, and otherwise makes the address the bare page", async () => {
  // A section's address typed in, and an entry with another load's mark gone Forward to: neither
  // is this load's, so what's under it may not be the page.
  for (const state of [null, { nova: "0123abcd" }]) {
    const shown = page();
    await arrived(shown);
    shown.context.window.scrollY = 200;
    shown.visit("#music", state);
    assert.equal(shown.body.dataset.open, "music");
    await shown.node("back").click();
    assert.equal(shown.done.join("|"), "replace /settings");
    assert.equal(shown.body.dataset.open, undefined);
    assert.equal(shown.focusedOnRow("music"), true);
    // And the next opens as usual.
    await shown.row("clock").click();
    await shown.node("back").click();
    assert.equal(shown.done.join("|"), "replace /settings|push #clock|back");
  }
});

test("a page brought back from the browser's cache can go back again, though it was told of no arrival", async () => {
  const shown = page();
  await arrived(shown);
  await shown.row("music").click();
  await shown.node("back").click();
  // Shown again from the back/forward cache, with no popstate.
  shown.fire("pageshow", { persisted: true });
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "push #music|back|back");
});

test("the open section's row is the current one, and Status's on a wide screen with none open", async () => {
  const current = (shown) => SECTION_IDS.filter((id) => shown.row(id).getAttribute("aria-current") === "true").join(",");
  const narrow = page();
  await arrived(narrow);
  assert.equal(current(narrow), "");
  await narrow.row("music").click();
  assert.equal(current(narrow), "music");
  const wide = page({ wide: true });
  await arrived(wide);
  assert.equal(current(wide), "status");
  await wide.row("music").click();
  assert.equal(current(wide), "music");
  await wide.node("back").click();
  wide.arrive();
  assert.equal(current(wide), "status");
});

test("with a section open, tapping another takes its entry's place, so #back still goes to the list", async () => {
  const shown = page({ wide: true });
  await arrived(shown);
  await shown.row("music").click();
  await shown.row("clock").click();
  await shown.row("devices").click();
  assert.equal(shown.body.dataset.open, "devices");
  assert.equal(shown.done.join("|"), "push #music|replace #clock|replace #devices");
  assert.equal(shown.focusedOn("devices-title"), true);
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "push #music|replace #clock|replace #devices|back");
  shown.arrive();
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.shown(), "");
});

test("'All of Nova' pressed twice before the browser has gone back goes back once", async () => {
  const shown = page();
  await arrived(shown);
  await shown.row("music").click();
  await shown.node("back").click();
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "push #music|back");
  shown.arrive();
  // And again once the list has come back, for the next section.
  await shown.row("clock").click();
  await shown.node("back").click();
  assert.equal(shown.done.join("|"), "push #music|back|push #clock|back");
});

test("loading at an address which is no section leaves the list", async () => {
  const shown = page({ hash: "#nonsense" });
  await arrived(shown);
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.shown(), "");
  assert.equal(shown.done.join("|"), "");
});

test("while the page is locked nothing opens, and what the address names opens on unlocking", async () => {
  const shown = page({ hash: "#music", loggedIn: false });
  await arrived(shown);
  assert.equal(shown.body.dataset.open, undefined);
  // Nor does a tap on a row, which can't be seen, but could be sent.
  await shown.row("devices").click();
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.done.join("|"), "");
  // Logged in.
  shown.login.logged_in = true;
  await shown.context.checkLogin();
  assert.equal(shown.body.dataset.open, "music");
  assert.equal(shown.done.join("|"), "replace /settings|push #music");
});

test("a look at Nova while a section is open neither closes it, scrolls, nor moves the focus", async () => {
  const shown = page({ hash: "#music" });
  await arrived(shown);
  const scrolled = shown.scrolls.length;
  const kept = focused;
  shown.answers.state = { ...STATE, memories: ["likes tea"], status: { ...STATE.status, problems: [] }, restart_needed: true };
  await shown.context.load();
  assert.equal(shown.body.dataset.open, "music");
  assert.equal(shown.shown(), "music");
  assert.equal(shown.scrolls.length, scrolled);
  assert.equal(focused === kept, true);
  // The look did draw: its sections are drawn while hidden, as they are while shown.
  assert.equal(shown.node("memories").textContent, "likes tea");
  assert.equal(shown.node("restart").hidden, false);
});

test("with nothing open the Status row is the one showing, and the open section's row once one is", async () => {
  const shown = page();
  await arrived(shown);
  assert.equal(shown.row("status").classes.has("showing"), true);
  assert.equal(shown.row("music").classes.has("showing"), false);
  await shown.row("music").click();
  assert.equal(shown.row("status").classes.has("showing"), false);
  assert.equal(shown.row("music").classes.has("showing"), true);
});

test("Status's problems aren't drawn by opening it before Nova has been heard from", async () => {
  const shown = page();
  await shown.context.checkLogin();
  await shown.row("status").click();
  assert.equal(shown.body.dataset.open, "status");
  // No "No problems lately." as if there were none, when nothing is known.
  assert.equal(shown.node("problems").children.length, 0);
  await shown.context.load();
  assert.equal(shown.node("problems").children.length, 1);
});

test("Status's problems are measured again as Status comes into view, and not while it stays hidden", async () => {
  const shown = page();
  await arrived(shown);
  // Emptied, to tell whether they're drawn again.
  const drawn = () => shown.node("problems").children.length;
  shown.node("problems").replaceChildren();
  await shown.row("music").click();
  await shown.row("clock").click();
  assert.equal(drawn(), 0);
  await shown.node("back").click();
  shown.arrive();
  assert.equal(drawn(), 0, "the list alone on a phone leaves Status hidden");
  await shown.row("status").click();
  assert.equal(drawn(), 1);
});

test("on a wide screen Status is in view with the list, so it is measured as the list comes back", async () => {
  const shown = page({ wide: true });
  await arrived(shown);
  await shown.row("music").click();
  shown.node("problems").replaceChildren();
  await shown.node("back").click();
  shown.arrive();
  assert.equal(shown.node("problems").children.length, 1);
});

test("the page has exactly one section for each of the list's, and no other", () => {
  const list = vm.createContext({});
  vm.runInContext(LIST, list);
  const wanted = vm.runInContext("NovaList.SECTIONS.map((section) => section.id).join(',')", list);
  assert.equal(wanted, SECTION_IDS.join(","));
  const found = [...HTML.matchAll(/data-section="([a-z]+)"/g)].map((one) => one[1]);
  assert.equal(found.length, SECTION_IDS.length);
  assert.equal(found.slice().sort().join(","), SECTION_IDS.slice().sort().join(","));
  for (const id of SECTION_IDS) {
    assert.equal(HTML.split(`id="section-${id}"`).length - 1, 1, id);
    // Each is labelled by a heading of its own, which can take the focus.
    assert.equal(HTML.includes(`<h2 class="heading" id="${id}-title" tabindex="-1">`), true, id);
    assert.equal(HTML.includes(`aria-labelledby="${id}-title"`), true, id);
  }
});

test("every id the script asks the page for by name is in the page, exactly once", () => {
  const asked = new Set([...SCRIPT.matchAll(/getElementById\("([^"]+)"\)/g)].map((one) => one[1]));
  assert.equal(asked.size > 50, true);
  const wrong = [...asked].filter((id) => HTML.split(`id="${id}"`).length - 1 !== 1);
  assert.equal(wrong.join(","), "");
});

test("the approvals section is hidden through npm", async () => {
  const shown = page({ route: "npm" });
  await arrived(shown);
  await shown.context.loadApprovals();
  assert.equal(shown.node("approvals-body").hidden, true);
  assert.equal(shown.node("approvals-npm").hidden, false);
  assert.equal(HTML.includes(`<p class="panel note" id="approvals-npm" hidden>Only at the page's Tailscale address.</p>`), true);
  assert.equal(shown.node("line-approvals").textContent, "Only at the page's Tailscale address");
  // Nor is Nova asked, which would refuse it.
  assert.equal(shown.asked.includes("/api/approvals"), false);
  // At the Tailscale address it's shown, with the passkeys, who can approve and what was approved.
  const tailnet = page();
  await arrived(tailnet);
  await tailnet.context.loadApprovals();
  assert.equal(tailnet.node("approvals-body").hidden, false);
  assert.equal(tailnet.node("approvals-npm").hidden, true);
  assert.equal(tailnet.node("line-approvals").textContent, "1 passkey, the PC");
  assert.equal(tailnet.node("passkeys").textContent.includes("Phone"), true);
  assert.equal(tailnet.node("approvers").textContent.includes("Can approve"), true);
  assert.equal(tailnet.node("recent-approvals").textContent.includes("Remove 'Dentist'"), true);
});

// A change Nova answers only once he approves, `after` milliseconds of the page's time on - or
// never, if the page gives up first, as fetch does when its signal aborts.
const waitsForOk = (after, message) => (path, options, answer) => new Promise((resolve, reject) => {
  options.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  setTimeout(() => resolve(answer({ success: true, message })), after);
});

// Lets the page's promises settle, with no time passing.
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a change which waits for his OK is given as long as Nova waits, the control saying where to give it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const off = { ...APPROVALS, satellites: [{ ...APPROVALS.satellites[0], can_approve: false }] };
  const run = async (state, after) => {
    const shown = page({ state, approvals: off, post: waitsForOk(after, "pc can approve now.") });
    await arrived(shown);
    await shown.context.loadApprovals();
    const box = descendants(shown.node("approvers")).find((one) => one.getAttribute("role") === "switch");
    const words = descendants(shown.node("approvers")).find((one) => one.text === "Can approve" || one.text.startsWith("Check"));
    box.checked = true;
    const done = Promise.all(box.listeners.change.map((listener) => listener()));
    await settle();
    // Waiting: the switch is off and its words say where to approve.
    assert.equal(words.text, "Check your phone.");
    assert.equal(box.disabled, true);
    return { shown, box, words, done };
  };
  // With Nova's wait not known, it's given the longest Nova waits: well past a change's usual 60 seconds.
  const unknown = await run(STATE, 90000);
  t.mock.timers.tick(61000);
  await settle();
  assert.equal(unknown.words.text, "Check your phone.");
  t.mock.timers.tick(30000);
  await unknown.done;
  assert.equal(unknown.shown.node("toast").textContent, "pc can approve now.");
  assert.equal(unknown.words.text, "Can approve");
  assert.equal(unknown.box.disabled, false);
  // With it known, 2 minutes and 15 seconds - and no longer.
  const wait = { key: "approvals.wait_minutes", label: "Minutes to wait for an answer", kind: "number", value: 2,
                 used: "Used straight away", form: "number", min: 1, max: 10, whole: true };
  const known = await run({ ...STATE, settings: [wait] }, 200000);
  t.mock.timers.tick(134000);
  await settle();
  assert.equal(known.words.text, "Check your phone.");
  t.mock.timers.tick(2000);
  await known.done;
  assert.equal(known.shown.node("toast").textContent, "Nova didn't answer in time, so that may or may not have worked.");
  assert.equal(known.box.disabled, false);
});

test("a satellite which can approve is named as it's said, with its machine name under it", async () => {
  const shown = page();
  await arrived(shown);
  await shown.context.loadApprovals();
  const texts = descendants(shown.node("approvers")).map((one) => one.text).filter(Boolean);
  assert.equal(texts.includes("The PC"), true);
  assert.equal(texts.includes("pc · Connected"), true);
});
