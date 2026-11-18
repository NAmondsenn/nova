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

  setAttribute() {}

  removeAttribute() {}

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
                     "calendars", "routines", "satellites", "sound", "claude", "notifications", "memory", "keys",
                     "else"];

// What /api/state says, with nothing in it.
const STATE = {
  wake_word: "Hey Nova", muted: false, satellites: [], satellites_unreadable: false, satellite: null,
  status: { reply_ms: null, replies: 0, started: 1700000000, speech_to_text: "ready", ntfy: null, problems: [] },
  volumes: {}, clock: { timers: [], alarms: [] }, memories: [], routines: [],
  displays: { port: 0, list: [] }, keys: [], settings: [], settings_problem: "", restart_needed: false, page: "one",
};

// The page's scripts over a stand-in page, at the address `hash` and logged in or, with
// `loggedIn` false, through NPM and not yet. `state` is what Nova says of itself.
function page({ hash = "", loggedIn = true, state = STATE } = {}) {
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
  const entries = [{ state: null, hash }];
  let at = 0;
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
    scrollTo(left, top) {
      scrolls.push(top);
      window.scrollY = top;
    },
    addEventListener(kind, listener) {
      (windowListeners[kind] ||= []).push(listener);
    },
  };
  const login = { route: loggedIn ? "direct" : "npm", logged_in: loggedIn, password_set: true, page: "one" };
  const answers = { state };
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
    async fetch(path) {
      if (path === "/api/login-state") return answer(login);
      if (path === "/api/state") return answer(answers.state);
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
    // The row of a section in the list.
    row: (id) => descendants(node("rows")).find((one) => one.dataset.open === id),
    // Whether the focus is on the node of that id, or on a row.
    focusedOn: (id) => focused === node(id),
    focusedOnRow(id) {
      return focused === this.row(id);
    },
    // The browser going back one entry, and telling the page.
    arrive() {
      at -= 1;
      location.hash = entries[at].hash;
      for (const listener of windowListeners.popstate || []) listener({});
    },
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

test("loading at #music opens it with nothing pushed, and closing then replaces the address", async () => {
  const shown = page({ hash: "#music" });
  assert.equal(shown.body.dataset.open, undefined, "nothing opens until the page is let in");
  await arrived(shown);
  assert.equal(shown.body.dataset.open, "music");
  assert.equal(shown.shown(), "music");
  assert.equal(shown.done.join("|"), "");
  assert.equal(shown.focusedOn("music-title"), true);
  await shown.node("back").click();
  // Back would have left the page, so the address is made the bare page instead.
  assert.equal(shown.done.join("|"), "replace /settings");
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.focusedOnRow("music"), true);
});

test("a section's entry kept across a reload still goes back to the list", async () => {
  const shown = page();
  await arrived(shown);
  await shown.row("music").click();
  // A reload keeps the history, and its entry's state: a second page over the same entry.
  const again = page({ hash: "#music" });
  again.context.history.pushState({ nova: true }, "", "#music");
  await arrived(again);
  await again.node("back").click();
  assert.equal(again.done.join("|"), "push #music|back");
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
  assert.equal(shown.done.join("|"), "");
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
