// The settings page's rows made alive: what each row says, what needs attention, and search - see
// voice_assistant/web/settings.js, settings.html and settings_list.js.
//
// The page's scripts are run in a context of their own, settings_list.js then settings.js, over a
// small stand-in for the page: nodes which keep their children, classes and attributes and which
// the page's own selectors find, the sections the HTML names, and a fetch which answers as Nova
// would. Only small strings, numbers and booleans are compared, never the stand-in's nodes, as a
// failing comparison of those would print the whole tree.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const web = (name) => readFileSync(new URL(`../../voice_assistant/web/${name}`, import.meta.url), "utf8");
const LIST = web("settings_list.js");
const SCRIPT = web("settings.js");
const HTML = web("settings.html");

const BLOCKS = [...HTML.matchAll(/<div class="settings-of" data-settings="([a-z]+)"/g)].map((one) => one[1]);
const SECTION_IDS = ["status", "music", "clock", "devices", "batteries", "displays", "journeys", "football",
                     "calendars", "routines", "satellites", "sound", "claude", "notifications", "memory", "keys",
                     "else"];

// What has the focus, and the top-level nodes of the page now being tested - which is what a node
// is "connected" to.
let active = null;
let roots = new Set();

// One simple selector, as far as the page's own use: a tag, classes, attributes, and :active.
const SIMPLE = /^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)(:active)?$/;

// An element, as far as the page's script uses one.
class Node {
  constructor(tag) {
    this.tag = tag;
    this.id = "";
    this.text = "";
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.pressed = false;
    this.dataset = {};
    this.style = { setProperty() {} };
    this.listeners = {};
    this.attributes = new Map();
    this.classes = new Set();
    this.held = "";
    this.scrolled = null;
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        if (force === undefined ? !this.classes.has(name) : force) this.classes.add(name);
        else this.classes.delete(name);
      },
    };
  }

  get className() {
    return [...this.classes].join(" ");
  }

  set className(value) {
    this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get value() {
    return this.held;
  }

  set value(value) {
    this.held = String(value);
  }

  get parentElement() {
    return this.parent;
  }

  get isConnected() {
    let top = this;
    while (top.parent) top = top.parent;
    return roots.has(top);
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.text = String(value);
    this.release();
  }

  // Lets go of every child.
  release() {
    for (const child of this.children) child.parent = null;
    this.children = [];
  }

  append(...nodes) {
    this.children.push(...this.adopt(nodes));
  }

  replaceChildren(...nodes) {
    this.text = "";
    this.release();
    this.children = this.adopt(nodes);
  }

  adopt(nodes) {
    return nodes.map((node) => {
      const made = typeof node === "string" ? Object.assign(new Node("#text"), { text: node }) : node;
      if (made.parent && made.parent !== this) made.parent.children = made.parent.children.filter((one) => one !== made);
      made.parent = this;
      return made;
    });
  }

  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((one) => one !== this);
    this.parent = null;
  }

  cloneNode() {
    const copy = new Node(this.tag);
    copy.text = this.text;
    copy.children = this.children.map((child) => {
      const made = child.cloneNode();
      made.parent = copy;
      return made;
    });
    return copy;
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

  closest(selector) {
    const name = selector.slice(1);
    for (let node = this; node; node = node.parent) if (node.classes.has(name)) return node;
    return null;
  }

  matches(selector) {
    return selector.split(",").some((part) => {
      const found = SIMPLE.exec(part.trim());
      if (!found) throw new Error(`The stand-in can't match ${part}`);
      const [, tag, classes, attributes, pressed] = found;
      if (tag && this.tag !== tag) return false;
      if (classes.split(".").filter(Boolean).some((name) => !this.classes.has(name))) return false;
      for (const one of attributes.match(/\[[^\]]+\]/g) || []) {
        const [, name, value] = /\[([\w-]+)(?:="([^"]*)")?\]/.exec(one);
        const held = value === undefined ? Boolean(this[name] || this.attributes.has(name)) : this.getAttribute(name) === value;
        if (!held) return false;
      }
      return !pressed || this.pressed;
    });
  }

  querySelectorAll(selector) {
    return descendants(this).filter((one) => one.matches(selector));
  }

  querySelector(selector) {
    // The page's play button has its icon's <use> under it, which the stand-in doesn't draw.
    return this.querySelectorAll(selector)[0] || (selector === "use" ? new Node("use") : null);
  }

  contains(other) {
    return other === this || descendants(this).includes(other);
  }

  focus() {
    active = this;
  }

  scrollIntoView(how) {
    this.scrolled = how.block;
  }
}

// Every node under one, in order.
function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

// What /api/state says, with nothing in it.
const STATE = {
  wake_word: "Hey Nova", muted: false, satellites: [], satellites_unreadable: false, satellite: null,
  status: { reply_ms: null, replies: 0, started: 1700000000, speech_to_text: "ready", ntfy: null, problems: [] },
  volumes: {}, clock: { timers: [], alarms: [] }, memories: [], routines: [],
  displays: { port: 0, list: [] }, keys: [], settings: [], settings_problem: "", restart_needed: false, page: "one",
  journeys: null,
};

// A setting as /api/state gives one, with made-up values.
const timeOf = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "text", value,
                                  form: "time" });
const number = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "number", value,
                                  form: "number", min: 0, max: 100, whole: true });
const yesNo = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "yes/no", value });

// What the other loads say, with nothing the matter.
const MUSIC = { available: true, now: null, playlists: [], spotify: { signed_in: true } };
const HOME = { available: false };
const BATTERIES = { available: true, also_send_when_said: false, devices: [] };
const FOOTBALL = { available: false, message: "Home Assistant isn't connected." };
const CALENDARS = { available: false, message: "Google isn't set up." };

// A song playing, as /api/music says it.
const playing = (track, artist = "Artist") => ({ ...MUSIC, now: { track, artist, album: "", playing: true, cover: null,
                                                                   duration: 0, position: 0 } });

// A battery as /api/batteries gives one.
const battery = (name, level, more = {}) => ({ id: name.toLowerCase(), name, level, charging: false, stale: null,
                                                kind: "mobile", enabled: true, marks: [15, 5], default_marks: [15, 5],
                                                send_to_phone: false, changed: false, ...more });

// The page's scripts over a stand-in page. `answers` is what Nova says to each ask, or null for an
// ask which fails.
function page(answers = {}) {
  roots = new Set();
  const made = new Map();
  const blocks = new Map(BLOCKS.map((id) => [id, Object.assign(new Node("div"), { hidden: true })]));
  for (const [id, block] of blocks) block.setAttribute("data-settings", id);
  const everything = () => [...roots].flatMap((root) => [root, ...descendants(root)]);
  const timers = [];
  const document = {
    hidden: false,
    body: new Node("body"),
    get activeElement() {
      return active;
    },
    getElementById(id) {
      const found = everything().find((one) => one.id === id);
      if (found) return found;
      const node = Object.assign(new Node("div"), { id });
      made.set(id, node);
      roots.add(node);
      return node;
    },
    createElement: (tag) => new Node(tag),
    createElementNS: (namespace, tag) => new Node(tag),
    querySelector: (selector) => everything().find((one) => one.matches(selector)) || null,
    querySelectorAll: (selector) => everything().filter((one) => one.matches(selector)),
    addEventListener() {},
  };
  // Each section holds its settings' block, as the HTML has it, and the list its three parts.
  for (const id of SECTION_IDS) {
    const section = document.getElementById(`section-${id}`);
    if (blocks.has(id)) section.append(blocks.get(id));
  }
  document.getElementById("list").append(document.getElementById("attention"), document.getElementById("find"),
                                         document.getElementById("rows"));
  const given = { state: STATE, music: MUSIC, home: HOME, batteries: BATTERIES, football: FOOTBALL, calendars: CALENDARS,
                  ...answers };
  const window = { scrollY: 0, matchMedia: () => ({ matches: false }), scrollTo() {}, addEventListener() {} };
  const answer = (body) => (body === null
    ? { ok: false, status: 500, json: async () => ({}), text: async () => "broken" }
    : { ok: true, status: 200, json: async () => body, text: async () => "" });
  const paths = { "/api/state": "state", "/api/music": "music", "/api/home": "home", "/api/batteries": "batteries",
                  "/api/football": "football", "/api/calendars": "calendars" };
  const context = vm.createContext({
    document,
    window,
    location: { hash: "", pathname: "/settings", search: "" },
    history: { state: null, pushState() {}, replaceState() {}, back() {} },
    // Timers are kept, not run, so what waits two seconds can be let go of without waiting them.
    setTimeout: (run, milliseconds) => timers.push({ run, milliseconds, live: true }),
    clearTimeout: (id) => {
      if (timers[id - 1]) timers[id - 1].live = false;
    },
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
      if (path === "/api/login-state") return answer({ route: "direct", logged_in: true, password_set: true, page: "one" });
      return answer(path in paths ? given[paths[path]] : {});
    },
  });
  vm.runInContext(LIST, context);
  vm.runInContext(SCRIPT, context);
  active = null;
  context.setUpList();
  const node = (id) => document.getElementById(id);
  return {
    context,
    node,
    given,
    body: document.body,
    // Each of the loads, as the page asks for them.
    async loadAll() {
      await context.load();
      await context.loadMusic();
      await context.loadHome();
      await context.loadBatteries();
      await context.loadFootball();
      await context.loadCalendars();
    },
    block: (id) => blocks.get(id),
    // A row's line, as the row in the page has it.
    line: (id) => node(`line-${id}`).textContent,
    warns: (id) => node(`line-${id}`).classes.has("warn"),
    // The section rows in the page now, by their section's id, in order.
    rows: () => descendants(node("rows")).filter((one) => one.classes.has("row") && !one.classes.has("hit"))
      .map((one) => one.dataset.open),
    hits: () => descendants(node("rows")).filter((one) => one.classes.has("hit")),
    // The attention rows' words and their sections.
    attention: () => node("attention").children.map((one) => `${one.dataset.open}: ${one.textContent}`),
    async click(one) {
      await Promise.all((one.listeners.click || []).map((listener) => listener({})));
    },
    // Typing in the search box.
    type(text) {
      const box = node("find");
      box.value = text;
      for (const listener of box.listeners.input || []) listener({});
    },
    // Let go of every timer which waits the time given.
    wait(milliseconds) {
      for (const one of timers) {
        if (one.live && one.milliseconds === milliseconds) {
          one.live = false;
          one.run();
        }
      }
    },
    // The page's own variables and what it makes from them.
    view: () => JSON.parse(vm.runInContext("JSON.stringify(currentView())", context)),
    set(code, value) {
      context.value = value;
      vm.runInContext(`${code} = value`, context);
    },
    get: (code) => vm.runInContext(code, context),
  };
}

// The words of what a stamp in the brain's log would say, for some minutes ago: "2026-11-18 14:05".
const stamp = (minutesAgo) => {
  const when = new Date(Date.now() - minutesAgo * 60000);
  const two = (number) => String(number).padStart(2, "0");
  return { time: `${when.getFullYear()}-${two(when.getMonth() + 1)}-${two(when.getDate())} ${two(when.getHours())}:${two(when.getMinutes())}`,
           clock: `${two(when.getHours())}:${two(when.getMinutes())}` };
};

test("the rows say how each thing is, the low battery in the warning colour, and what needs attention is above", async () => {
  const shown = page({ state: { ...STATE, restart_needed: true }, music: playing("Song"),
                       batteries: { ...BATTERIES, devices: [battery("Phone", 80), battery("Tablet", 14)] } });
  assert.equal(shown.line("music"), "");
  await shown.loadAll();
  assert.equal(shown.line("music"), "Song, Artist");
  assert.equal(shown.warns("music"), false);
  assert.equal(shown.line("batteries"), "Tablet at 14%");
  assert.equal(shown.warns("batteries"), true);
  assert.equal(shown.line("status"), "All well");
  assert.equal(shown.line("memory"), "Nothing remembered");
  assert.equal(shown.line("else"), "");
  assert.equal(shown.attention().join("|"), "status: Nova needs a restart to use what's been saved|batteries: Tablet is at 14%");
  const [restart, low] = shown.node("attention").children;
  assert.equal(restart.tag, "button");
  assert.equal(restart.classes.has("attention"), true);
  assert.equal(restart.classes.has("row"), true);
  await shown.click(low);
  assert.equal(shown.body.dataset.open, "batteries");
  await shown.click(restart);
  assert.equal(shown.body.dataset.open, "status");
});

test("a later state with nothing the matter takes the attention rows and the warning away", async () => {
  const shown = page({ state: { ...STATE, restart_needed: true },
                       batteries: { ...BATTERIES, devices: [battery("Tablet", 14)] } });
  await shown.loadAll();
  assert.equal(shown.node("attention").children.length, 2);
  shown.given.state = { ...STATE, restart_needed: false };
  shown.given.batteries = { ...BATTERIES, devices: [battery("Tablet", 80)] };
  await shown.context.load();
  await shown.context.loadBatteries();
  assert.equal(shown.node("attention").children.length, 0);
  assert.equal(shown.line("batteries"), "Tablet at 80%");
  assert.equal(shown.warns("batteries"), false);
});

test("an attention row which is still wanted is kept as it was, and keeps the focus", async () => {
  const shown = page({ state: { ...STATE, restart_needed: true },
                       batteries: { ...BATTERIES, devices: [battery("Tablet", 14)] } });
  await shown.loadAll();
  const [, low] = shown.node("attention").children;
  low.focus();
  // The restart is done, leaving the battery's row alone.
  shown.given.state = { ...STATE, restart_needed: false };
  await shown.context.load();
  assert.equal(shown.attention().join("|"), "batteries: Tablet is at 14%");
  assert.equal(shown.node("attention").children[0] === low, true);
  assert.equal(active === low, true);
});

test("a row being pressed is not redrawn, and is at the next load", async () => {
  const shown = page({ music: playing("Song") });
  await shown.loadAll();
  assert.equal(shown.line("music"), "Song, Artist");
  descendants(shown.node("rows")).find((one) => one.dataset.open === "status").pressed = true;
  shown.given.music = playing("Tune");
  shown.given.state = { ...STATE, restart_needed: true };
  await shown.context.loadMusic();
  await shown.context.load();
  assert.equal(shown.line("music"), "Song, Artist");
  assert.equal(shown.node("attention").children.length, 0);
  descendants(shown.node("rows")).find((one) => one.dataset.open === "status").pressed = false;
  await shown.context.loadMusic();
  assert.equal(shown.line("music"), "Tune, Artist");
  assert.equal(shown.attention().length, 1);
  // An attention row pressed holds the lines back too.
  shown.node("attention").children[0].pressed = true;
  shown.given.music = playing("Other");
  await shown.context.loadMusic();
  assert.equal(shown.line("music"), "Tune, Artist");
});

test("a load which fails leaves its row's line empty, and takes what hung on it from the attention", async () => {
  const shown = page({ music: { ...MUSIC, spotify: { signed_in: false } }, batteries: { ...BATTERIES, devices: [battery("Tablet", 14)] } });
  await shown.loadAll();
  assert.equal(shown.line("music"), "Nothing playing");
  assert.equal(shown.attention().join("|"), "batteries: Tablet is at 14%|music: Sign in to Spotify");
  shown.given.music = null;
  await shown.context.loadMusic();
  assert.equal(shown.line("music"), "");
  assert.equal(shown.attention().join("|"), "batteries: Tablet is at 14%");
  // Thrown, not only refused.
  shown.given.music = playing("Song");
  await shown.context.loadMusic();
  assert.equal(shown.line("music"), "Song, Artist");
  shown.context.fetch = async () => {
    throw new Error("down");
  };
  await shown.context.loadMusic();
  await shown.context.loadBatteries();
  assert.equal(shown.line("music"), "");
  assert.equal(shown.line("batteries"), "");
  assert.equal(shown.attention().length, 0);
});

test("a state which can't be read says nothing, not All well, and then the old state is not shown as now", async () => {
  const shown = page({ state: { ...STATE, memories: ["a", "b"], routines: [{ id: "r", name: "Bedtime", phrases: ["bedtime"], at: "", repeat: "daily", steps: [],
                                                                  satellite: null }] } });
  await shown.loadAll();
  assert.equal(shown.line("status"), "All well");
  assert.equal(shown.line("memory"), "2 things remembered");
  assert.equal(shown.line("routines"), "1 routine");
  shown.given.state = null;
  await shown.context.load();
  for (const id of ["status", "memory", "routines", "clock", "satellites", "keys", "displays"]) {
    assert.equal(shown.line(id), "", id);
  }
  assert.equal(shown.view().loaded, false);
  // The other loads' lines stay, as theirs worked.
  assert.equal(shown.line("music"), "Nothing playing");
});

test("the view's parts come from what each load said: the clock, the problems and the status tiles", async () => {
  const recent = stamp(5);
  const shown = page({ state: {
    ...STATE,
    clock: { timers: [{ id: 1, name: "10 minute timer", due_at: 1 }, { id: 2, name: "Leave for Work reminder", due_at: 2 },
                      { id: 3, name: "Tea reminder", due_at: 3 }], alarms: [{ id: 4, name: "7:00 AM alarm", due_at: 4 }] },
    status: { ...STATE.status, speech_to_text: "missing", ntfy: "not connected",
              problems: [{ time: recent.time, level: "error", message: "x" }, { time: "2026-01-01 00:00", level: "warning", message: "y" }] } } });
  await shown.context.load();
  const view = shown.view();
  assert.equal(JSON.stringify(view.clock), '{"timers":1,"reminders":2,"alarms":1}');
  assert.equal(shown.line("clock"), "1 timer, 2 reminders, 1 alarm");
  assert.equal(view.problems.map((one) => one.level).join(","), "problem,warning");
  assert.equal(view.problems[0].at, new Date(`${recent.time.replace(" ", "T")}:00`).getTime() / 1000);
  assert.equal(view.tiles.map((one) => `${one.name}:${one.good}`).join(","),
               "Running:true,Typical reply:true,Speech to text:false,Notifications:false");
  assert.equal(view.tiles[2].words, "Speech to text is missing");
  assert.equal(shown.line("status"), "Speech to text is missing");
  // With the tiles good, it is the problem in the last hour, at its time.
  shown.given.state = { ...STATE, status: { ...STATE.status, problems: [{ time: recent.time, level: "error", message: "x" }] } };
  await shown.context.load();
  assert.equal(shown.line("status"), `A problem at ${recent.clock}`);
  assert.equal(shown.attention().join("|"), `status: A problem at ${recent.clock}`);
  // A warning is not one.
  shown.given.state = { ...STATE, status: { ...STATE.status, problems: [{ time: recent.time, level: "warning", message: "y" }] } };
  await shown.context.load();
  assert.equal(shown.line("status"), "All well");
  assert.equal(shown.attention().length, 0);
});

test("a battery is low as Nova would warn of it: warnings on, not charging, at or under its highest mark", async () => {
  const shown = page({ batteries: { ...BATTERIES, devices: [
    battery("A", 15), battery("B", 16), battery("C", 10, { charging: true }), battery("D", 10, { enabled: false }),
    battery("E", 5, { charging: null, marks: [5] }), battery("F", 40, { marks: [] })] } });
  await shown.context.loadBatteries();
  assert.equal(shown.view().batteries.map((one) => `${one.name}:${one.low}`).join(","),
               "A:true,B:false,C:false,D:false,E:true,F:false");
});

test("the lights, football, the sign-ins and the setting-based parts are read, and left out when they can't be", async () => {
  const shown = page({ state: { ...STATE, settings: [timeOf("ntfy.quiet_from", "23:00"), timeOf("ntfy.quiet_until", "07:00"),
                                                      yesNo("web_search.enabled", false)] } });
  await shown.context.load();
  shown.set("home", { available: true, success: true, lights: [{ on: true }, { on: false }, { on: true }] });
  shown.set("worked.home", true);
  shown.set("football", { available: true, teams: [{}, {}] });
  shown.set("worked.football", true);
  shown.set("calendars", { available: true, signed_in: false });
  shown.set("worked.calendars", true);
  shown.set("music", { available: true, now: null, spotify: { signed_in: true } });
  shown.set("worked.music", true);
  shown.context.drawList();
  let view = shown.view();
  assert.equal(JSON.stringify(view.lights), '{"on":2,"total":3}');
  assert.equal(shown.line("devices"), "2 lights on");
  assert.equal(JSON.stringify(view.football), '{"teams":2}');
  assert.equal(`${view.google},${view.spotify}`, "out,in");
  assert.equal(shown.line("calendars"), "Sign in to Google");
  assert.equal(shown.warns("calendars"), true);
  assert.equal(JSON.stringify(view.quiet), '{"from":"23:00","until":"07:00"}');
  assert.equal(view.webSearch, false);
  assert.equal(shown.line("notifications"), "Quiet from 23:00 to 07:00");
  assert.equal(shown.line("claude"), "Web search off");
  // The sign-in answers: Google not set up, Spotify not offered, and no night.
  shown.set("calendars", { available: false, message: "" });
  shown.set("music", { available: false });
  shown.given.state = { ...STATE, settings: [timeOf("ntfy.quiet_from", "07:00"), timeOf("ntfy.quiet_until", "07:00")] };
  await shown.context.load();
  view = shown.view();
  assert.equal(`${view.google},${view.spotify},${view.music},${view.quiet},${view.webSearch}`, "off,off,null,false,null");
  // A failed load of each is null, though what it held is still there.
  for (const part of ["home", "football", "calendars", "music"]) shown.set(`worked.${part}`, false);
  view = shown.view();
  assert.equal(`${view.lights},${view.football},${view.google},${view.spotify}`, "null,null,null,null");
  shown.context.drawList();
  assert.equal(`${shown.line("devices")}|${shown.line("football")}|${shown.line("calendars")}`, "||");
});

test("saved settings which can't be read leave the setting-based lines empty, as what is shown is Nova's own", async () => {
  const shown = page({ state: { ...STATE, settings_problem: "line 3 is wrong",
                                settings: [timeOf("ntfy.quiet_from", "23:00"), timeOf("ntfy.quiet_until", "07:00"), yesNo("web_search.enabled", true)] } });
  await shown.context.load();
  assert.equal(`${shown.line("notifications")}|${shown.line("claude")}`, "|");
  assert.equal(shown.attention().join("|"), "else: line 3 is wrong");
});

test("the clocks count as connected as their satellite is, the default's when they have none", async () => {
  const satellites = [{ name: "bedroom", connected: true, default: true }, { name: "pc", connected: false, default: false }];
  const displays = { port: 0, list: [{ id: "1", satellite: null }, { id: "2", satellite: "pc" }, { id: "3", satellite: "bedroom" }] };
  const shown = page({ state: { ...STATE, satellites, displays } });
  shown.set("stateSaid", { ...STATE, satellites, displays });
  assert.equal(JSON.stringify(shown.view().displays), '{"connected":2,"total":3}');
  // The satellites' file can't be read, so nothing of either is known.
  shown.set("stateSaid", { ...STATE, satellites: [], satellites_unreadable: true, displays });
  const view = shown.view();
  assert.equal(`${view.satellites},${view.displays}`, "null,null");
  // No clocks is none added, as it is.
  shown.set("stateSaid", { ...STATE, satellites, displays: { port: 0, list: [] } });
  assert.equal(JSON.stringify(shown.view().displays), '{"connected":0,"total":0}');
});

test("the default satellite being away needs attention, and the volume, journeys and keys are read", async () => {
  const shown = page({ state: { ...STATE, satellites: [{ name: "bedroom", connected: false, default: true, travels: false }],
                                keys: [{ set: true }, { set: false }, { set: true }], journeys: { requests: 21, cap: 3000 },
                                memories: ["a"] } });
  await shown.context.load();
  assert.equal(shown.attention().join("|"), "satellites: The bedroom satellite isn't connected");
  assert.equal(shown.line("satellites"), "0 of 1 connected");
  assert.equal(shown.line("keys"), "2 of 3 set");
  assert.equal(shown.line("journeys"), "21 of 3,000 asks used this month");
  shown.set("stateSaid", { ...STATE, volumes: { general: 60.4 } });
  assert.equal(shown.view().voice, 60);
  shown.set("stateSaid", { ...STATE, volumes: {} });
  assert.equal(shown.view().voice, null);
});

test("Journeys says its count of asks above its settings, and where its places come from under them", async () => {
  const shown = page({ state: { ...STATE, journeys: { requests: 21, cap: 3000 },
                                settings: [number("journeys.buffer_minutes", 10)] } });
  await shown.context.load();
  assert.equal(shown.node("journeys-used").textContent, "21 of 3,000 asks of Google used this month");
  assert.equal(shown.node("journeys-used").hidden, false);
  const parts = shown.block("journeys").children;
  assert.equal(parts[parts.length - 1].textContent, "Places are your zones in Home Assistant.");
  assert.equal(parts[parts.length - 1].classes.has("hint"), true);
  // Nothing to count, nothing said - and not an old count either.
  shown.given.state = { ...STATE, journeys: null, settings: [number("journeys.buffer_minutes", 10)] };
  await shown.context.load();
  assert.equal(shown.node("journeys-used").hidden, true);
  shown.given.state = { ...STATE, journeys: { requests: 3, cap: 10 } };
  await shown.context.load();
  assert.equal(shown.node("journeys-used").textContent, "3 of 10 asks of Google used this month");
  shown.given.state = null;
  await shown.context.load();
  assert.equal(shown.node("journeys-used").hidden, true);
});

// The section nodes get a few of their own words, as the page's HTML has: headings, labels,
// buttons and placeholders, one in each place, and some a search must pass over.
function withWords(shown) {
  const own = (id, tag, text, more = {}) => {
    const made = Object.assign(new Node(tag), more);
    made.textContent = text;
    shown.node(`section-${id}`).append(made);
    return made;
  };
  return {
    heading: own("status", "h3", "Recent problems"),
    mute: own("satellites", "label", "Mute the microphone", { htmlFor: "mute" }),
    add: own("satellites", "button", "Add a satellite"),
    placeholder: own("satellites", "input", "", { placeholder: "Its Pi's machine name, e.g. bedroom" }),
    alarm: own("clock", "button", "Add alarm"),
    empty: own("clock", "button", ""),
    hiddenOne: Object.assign(own("music", "button", "Playing hidden"), { hidden: true }),
    insideHidden: (() => {
      const wrapper = Object.assign(own("music", "div", ""), { hidden: true });
      const inner = Object.assign(new Node("button"), { text: "Inside hidden" });
      wrapper.append(inner);
      return inner;
    })(),
    again: own("satellites", "button", "Add a satellite"),
  };
}

const QUIET = [timeOf("ntfy.quiet_from", "23:00"), timeOf("ntfy.quiet_until", "07:00")];

test("typing 'quiet' lists Notifications with the quiet hours as hits, which open it and mark the field", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  withWords(shown);
  shown.type("quiet");
  assert.equal(shown.rows().join(","), "notifications");
  assert.equal(shown.hits().map((one) => one.textContent).join("|"), "Label of ntfy.quiet_from|Label of ntfy.quiet_until");
  assert.equal(shown.hits()[0].tag, "button");
  // The row in the results has the line, as the list's own does.
  assert.equal(shown.line("notifications"), "Quiet from 23:00 to 07:00");
  await shown.click(shown.hits()[1]);
  assert.equal(shown.body.dataset.open, "notifications");
  const field = shown.node("setting-ntfy.quiet_until").closest(".field");
  assert.equal(field.classes.has("found"), true);
  assert.equal(field.scrolled, "center");
  assert.equal(shown.node("setting-ntfy.quiet_from").closest(".field").classes.has("found"), false);
  // For two seconds, and then not.
  shown.wait(2000);
  assert.equal(field.classes.has("found"), false);
  // The box's words are left in place, so going back is to the same results.
  assert.equal(shown.node("find").value, "quiet");
  assert.equal(shown.rows().join(","), "notifications");
});

test("emptying the box puts every row back, with the lines which changed while the results were shown", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET }, music: playing("Song") });
  await shown.loadAll();
  const was = shown.node("rows").children[0];
  shown.type("quiet");
  assert.equal(shown.rows().length, 1);
  shown.given.music = playing("Tune");
  await shown.context.loadMusic();
  shown.type("   ");
  assert.equal(shown.rows().join(","), SECTION_IDS.join(","));
  assert.equal(shown.node("rows").children[0] === was, true);
  assert.equal(shown.line("music"), "Tune, Artist");
  assert.equal(shown.line("notifications"), "Quiet from 23:00 to 07:00");
  // A row's focus goes back to the one in the page now.
  assert.equal(shown.get("rowButtons.get('music')") === descendants(shown.node("rows")).find((one) => one.dataset.open === "music"), true);
});

test("a search with no match says so, and one of pattern characters throws nothing and finds nothing", async () => {
  const shown = page();
  await shown.loadAll();
  shown.type("zzz");
  assert.equal(shown.rows().length, 0);
  const none = shown.node("rows").children[0];
  assert.equal(none.tag, "p");
  assert.equal(none.textContent, "Nothing on Nova's page matches that.");
  assert.equal(none.classes.has("note"), true);
  shown.type(".*(");
  assert.equal(shown.node("rows").children[0].textContent, "Nothing on Nova's page matches that.");
  shown.type("[a-");
  assert.equal(shown.node("rows").children[0].textContent, "Nothing on Nova's page matches that.");
  shown.type("");
  assert.equal(shown.rows().length, 17);
});

test("a search reads each section's own words from the page: headings, labels, buttons and placeholders", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  const words = withWords(shown);
  const found = (query) => {
    shown.type(query);
    return `${shown.rows().join(",")}: ${shown.hits().map((one) => one.textContent).join("|")}`;
  };
  assert.equal(found("recent"), "status: Recent problems");
  assert.equal(found("mute"), "satellites: Mute the microphone");
  assert.equal(found("machine name"), "satellites: Its Pi's machine name, e.g. bedroom");
  // The same words in two places are listed once.
  assert.equal(found("add a sat"), "satellites: Add a satellite");
  // A hidden button, one inside something hidden and an empty one aren't there.
  assert.equal(found("hidden"), ": ");
  assert.equal(found("alarm"), "clock: Add alarm");
  // A section found by its name has no hits, and every word has to be in the section.
  assert.equal(found("timers"), "clock: ");
  assert.equal(found("mute alarm"), ": ");
  // A setting's label is listed with its setting, once.
  assert.equal(found("ntfy.quiet_from"), "notifications: Label of ntfy.quiet_from");
  // Pressing a control's hit marks the element itself.
  shown.type("mute");
  await shown.click(shown.hits()[0]);
  assert.equal(shown.body.dataset.open, "satellites");
  assert.equal(words.mute.classes.has("found"), true);
  assert.equal(words.mute.scrolled, "center");
});

test("a label which holds its field is read as its own words, not the field's choices too", async () => {
  const shown = page();
  await shown.loadAll();
  const label = Object.assign(new Node("label"), { htmlFor: "" });
  const first = new Node("span");
  first.textContent = "Repeats";
  const choices = new Node("span");
  choices.classes.add("select");
  const select = new Node("select");
  select.textContent = "NeverEvery day";
  choices.append(select);
  label.append(first, choices);
  shown.node("section-clock").append(label);
  shown.type("repeats");
  assert.equal(shown.hits().map((one) => one.textContent).join("|"), "Repeats");
});

test("the results are not worked out again by a load, whose new lines still reach them", async () => {
  const shown = page({ music: playing("Song") });
  await shown.loadAll();
  shown.type("song");
  const group = shown.node("rows").children[0];
  assert.equal(shown.rows().join(","), "music");
  assert.equal(shown.line("music"), "Song, Artist");
  shown.given.music = playing("Tune");
  shown.given.state = { ...STATE, memories: ["song of mine"] };
  await shown.context.loadMusic();
  await shown.context.load();
  // Still the one result, though the words no longer match its line.
  assert.equal(shown.node("rows").children[0] === group, true);
  assert.equal(shown.rows().join(","), "music");
  assert.equal(shown.line("music"), "Tune, Artist");
  // A new look at the box finds what is there now.
  shown.type("song ");
  assert.equal(shown.rows().join(","), "");
});

test("a hit whose element has been drawn again since only opens its section", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  shown.type("quiet");
  const field = shown.node("setting-ntfy.quiet_from").closest(".field");
  field.parent.replaceChildren();
  await shown.click(shown.hits()[0]);
  assert.equal(shown.body.dataset.open, "notifications");
  assert.equal(field.classes.has("found"), false);
});

test("the list's rows are the sections in their groups, and a tap on one opens it", async () => {
  const shown = page({ state: { ...STATE, restart_needed: true } });
  await shown.loadAll();
  assert.equal(shown.rows().join(","), SECTION_IDS.join(","));
  const names = descendants(shown.node("rows")).filter((one) => one.tag === "h3").map((one) => one.textContent);
  assert.equal(names.join(","), "Now,Home,Daily,Nova");
  const row = descendants(shown.node("rows")).find((one) => one.dataset.open === "memory");
  await shown.click(row);
  assert.equal(shown.body.dataset.open, "memory");
});
