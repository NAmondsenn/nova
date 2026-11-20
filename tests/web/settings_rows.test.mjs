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
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
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

  blur() {
    if (active === this) active = null;
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

// The labels Nova gives the settings these tests use, as settings_page.EDITABLE has them: a search
// has to find a setting by what a person reads, and by its key, not by a label made from it.
const LABELS = {
  "ntfy.quiet_from": "Quiet hours start at",
  "ntfy.quiet_until": "Quiet hours end at",
  "journeys.buffer_minutes": "Minutes to spare when working out when to leave",
  "web_search.enabled": "Search the web when asked to",
  "music.player": "What plays the music",
  "wrapped.enabled": "Make a Wrapped of your listening",
};
const labelOf = (key) => LABELS[key] || assert.fail(`No label for ${key}: copy it from EDITABLE`);

// A setting as /api/state gives one, with made-up values.
const timeOf = (key, value) => ({ key, label: labelOf(key), used: "Used straight away", kind: "text", value,
                                  form: "time" });
const number = (key, value) => ({ key, label: labelOf(key), used: "Used straight away", kind: "number", value,
                                  form: "number", min: 0, max: 100, whole: true });
const yesNo = (key, value) => ({ key, label: labelOf(key), used: "Used straight away", kind: "yes/no", value });

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
  const listeners = {};
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
    addEventListener(kind, listener) {
      (listeners[kind] ||= []).push(listener);
    },
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
  const window = { scrollY: 0, matchMedia: () => ({ matches: false, addEventListener() {} }), scrollTo() {},
                   addEventListener() {}, removeEventListener() {} };
  const answer = (body) => (body === null
    ? { ok: false, status: 500, json: async () => ({}), text: async () => "broken" }
    : { ok: true, status: 200, json: async () => body, text: async () => "" });
  const paths = { "/api/state": "state", "/api/music": "music", "/api/home": "home", "/api/batteries": "batteries",
                  "/api/football": "football", "/api/calendars": "calendars",
                  "/api/home-choices": "choices" };
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
    // A key pressed in the search box.
    press(key) {
      for (const listener of node("find").listeners.keydown || []) listener({ key });
    },
    // An event the browser sends the document.
    fire(kind) {
      for (const listener of listeners[kind] || []) listener({});
    },
    // What has the focus now.
    focused: () => active,
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
  assert.equal(shown.attention().join("|"), "else: Nova can't read its saved settings");
});

test("the clock displays row says how many there are and nothing of connection, whatever the clock server is doing", async () => {
  const list = [{ id: "1", satellite: null }, { id: "2", satellite: "pc" }];
  const shown = page({ state: { ...STATE, displays: { port: 0, list } } });
  await shown.context.load();
  assert.equal(JSON.stringify(shown.view().displays), '{"total":2}');
  assert.equal(shown.line("displays"), "2 clock displays");
  // With its port on, or its satellites unreadable, no differently.
  shown.given.state = { ...STATE, satellites_unreadable: true, displays: { port: 8765, list: list.slice(0, 1) } };
  await shown.context.load();
  assert.equal(shown.line("displays"), "1 clock display");
  shown.given.state = { ...STATE, displays: { port: 0, list: [] } };
  await shown.context.load();
  assert.equal(shown.line("displays"), "None added");
  shown.given.state = null;
  await shown.context.load();
  assert.equal(shown.line("displays"), "");
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
  // The microphone's mute, a label and its switch in a row of a list, as the page has them.
  const microphone = own("satellites", "li", "");
  const mute = Object.assign(new Node("label"), { htmlFor: "mute" });
  mute.textContent = "Mute the microphone";
  const muteControl = Object.assign(new Node("input"), { id: "mute", type: "checkbox" });
  microphone.append(mute, muteControl);
  return {
    heading: own("status", "h3", "Recent problems"),
    microphone,
    mute,
    muteControl,
    loose: own("satellites", "label", "Loose label", { htmlFor: "" }),
    add: own("satellites", "button", "Add a satellite"),
    placeholder: own("satellites", "input", "", { type: "text", placeholder: "Its Pi's machine name, e.g. bedroom" }),
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

test("typing 'quiet hours' lists Notifications with both night settings as hits, which open it and mark the field", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  withWords(shown);
  shown.type("quiet hours");
  assert.equal(shown.rows().join(","), "notifications");
  assert.equal(shown.hits().map((one) => one.textContent).join("|"), "Quiet hours start at|Quiet hours end at");
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
  assert.equal(shown.node("find").value, "quiet hours");
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
  assert.equal(found("ntfy quiet from"), "notifications: Quiet hours start at");
  // A label's hit is its control, which a visually hidden label would leave nothing to mark by. What
  // is marked is the row the control is in, not the control, whose own look the mark would paint
  // over; the focus goes to the control itself. A label with no control is itself the target.
  shown.type("mute");
  await shown.click(shown.hits()[0]);
  assert.equal(shown.body.dataset.open, "satellites");
  assert.equal(words.microphone.classes.has("found"), true);
  assert.equal(words.microphone.scrolled, "center");
  assert.equal(words.muteControl.classes.has("found"), false);
  assert.equal(words.mute.classes.has("found"), false);
  assert.equal(shown.focused() === words.muteControl, true);
  shown.type("loose label");
  await shown.click(shown.hits()[0]);
  assert.equal(words.loose.classes.has("found"), true);
  assert.equal(words.microphone.classes.has("found"), false);
  assert.equal(shown.focused() === words.loose, true);
});

test("a button with no words of its own is found by its accessible name", async () => {
  const shown = page();
  await shown.loadAll();
  const next = Object.assign(new Node("button"), {});
  next.setAttribute("aria-label", "Next song");
  shown.node("section-music").append(next);
  shown.type("next song");
  assert.equal(`${shown.rows().join(",")}: ${shown.hits().map((one) => one.textContent).join("|")}`, "music: Next song");
});

test("a found setting is marked by its field, and a switch takes the focus", async () => {
  const shown = page({ state: { ...STATE, settings: [yesNo("web_search.enabled", true)] } });
  await shown.loadAll();
  shown.type("search the web");
  await shown.click(shown.hits()[0]);
  const input = shown.node("setting-web_search.enabled");
  assert.equal(input.type, "checkbox");
  assert.equal(input.closest(".field").classes.has("found"), true);
  assert.equal(input.classes.has("found"), false);
  assert.equal(shown.focused() === input, true);
});

test("a found field which is typed into is marked but not focused, so no phone's keyboard covers it", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  const words = withWords(shown);
  // A setting's time field: the focus stays on the section's heading, where opening it put it.
  shown.type("quiet hours end");
  await shown.click(shown.hits()[0]);
  const input = shown.node("setting-ntfy.quiet_until");
  assert.equal(input.type, "time");
  assert.equal(input.closest(".field").classes.has("found"), true);
  assert.equal(shown.focused() === shown.node("notifications-title"), true);
  // A text box found by its placeholder, the same.
  shown.type("machine name");
  await shown.click(shown.hits()[0]);
  assert.equal(words.placeholder.classes.has("found"), true);
  assert.equal(shown.focused() === shown.node("satellites-title"), true);
});

test("a found select or button takes the focus", async () => {
  const shown = page();
  await shown.loadAll();
  const words = withWords(shown);
  const label = Object.assign(new Node("label"), { htmlFor: "weather-entity" });
  label.textContent = "Weather from";
  const select = Object.assign(new Node("select"), { id: "weather-entity", type: "select-one" });
  shown.node("section-calendars").append(label, select);
  shown.type("weather from");
  await shown.click(shown.hits()[0]);
  assert.equal(shown.focused() === select, true);
  shown.type("add alarm");
  await shown.click(shown.hits()[0]);
  assert.equal(shown.focused() === words.alarm, true);
});

test("a found sub-heading in a section's settings marks itself, not the panel it is in", async () => {
  const shown = page({ state: { ...STATE, settings: [yesNo("wrapped.enabled", true), number("music.player", 1)] } });
  await shown.loadAll();
  shown.type("wrapped");
  const hit = shown.hits().find((one) => one.textContent === "Wrapped");
  await shown.click(hit);
  const heading = descendants(shown.block("music")).find((one) => one.tag === "h3" && one.textContent === "Wrapped");
  assert.equal(heading.classes.has("found"), true);
  const panel = descendants(shown.block("music")).find((one) => one.classes.has("panel") && one.classes.has("settings"));
  assert.equal(panel.classes.has("found"), false);
});

test("Enter in the search box lets go of it, so a phone's keyboard closes over the results", async () => {
  const shown = page();
  await shown.loadAll();
  shown.node("find").focus();
  shown.type("music");
  shown.press("a");
  assert.equal(shown.focused() === shown.node("find"), true);
  shown.press("Enter");
  assert.equal(shown.focused(), null);
  assert.equal(shown.rows().join(","), "music");
});

test("how many sections a search found is said to a screen reader, and nothing once the box is empty", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  const said = () => shown.node("find-said").textContent;
  shown.type("quiet");
  assert.equal(said(), "1 section found");
  shown.type("a");
  assert.equal(said(), `${shown.rows().length} sections found`);
  shown.type("zzz");
  assert.equal(said(), "Nothing on Nova's page matches that.");
  shown.type("");
  assert.equal(said(), "");
});

test("closing a section whose row isn't in the search's results puts the focus on the search box", async () => {
  const shown = page({ state: { ...STATE, settings: QUIET } });
  await shown.loadAll();
  shown.type("quiet");
  // Opened from somewhere other than the results: an attention row, say.
  shown.context.openSection("memory");
  assert.equal(shown.focused() === shown.node("memory-title"), true);
  await shown.click(shown.node("back"));
  assert.equal(shown.body.dataset.open, undefined);
  assert.equal(shown.focused() === shown.node("find"), true);
});

test("the list is drawn again as the page comes back into view, so a line with a time in it is judged afresh", async () => {
  const shown = page();
  await shown.loadAll();
  assert.equal(shown.line("memory"), "Nothing remembered");
  shown.set("stateSaid", { ...STATE, memories: ["a", "b", "c"] });
  shown.fire("visibilitychange");
  assert.equal(shown.line("memory"), "3 things remembered");
});

test("the choice of satellite to look after has its caption, shown and hidden with it", async () => {
  const two = [{ name: "bedroom", connected: true, default: true, travels: false },
               { name: "kitchen", connected: true, default: false, travels: false }];
  const shown = page({ state: { ...STATE, satellites: two, satellite: "bedroom" } });
  await shown.context.load();
  assert.equal(shown.node("satellite-choice").hidden, false);
  assert.equal(shown.node("satellite-choice-title").hidden, false);
  shown.given.state = { ...STATE, satellites: two.slice(0, 1), satellite: "bedroom" };
  await shown.context.load();
  assert.equal(shown.node("satellite-choice").hidden, true);
  assert.equal(shown.node("satellite-choice-title").hidden, true);
});

// A satellite's row as the page draws it, by its name - which the page puts first in the row's `what`, as text.
const satelliteRow = (shown, name) => shown.node("satellites").children
  .find((one) => one.querySelector(".what").children[0].text === name) || assert.fail(`No row for ${name}`);
const badgesOf = (row) => row.querySelectorAll(".badge").map((one) => one.textContent);
const detailsOf = (row) => row.querySelectorAll(".detail").map((one) => one.textContent);
// The box of the names a satellite is also called, and the words beside it.
const calledBox = (row) => row.querySelectorAll("input").find((one) => one.type === "text");

// What /api/state says of some satellites which are each a different machine.
const MACHINES_SEEN = [
  { name: "home-assistant", connected: true, default: true, travels: false, machine: "pi", called: [], saves_notes: false },
  { name: "pc", connected: true, default: false, travels: false, machine: "pc", called: ["Desk", "computer"], saves_notes: true },
  { name: "mac", connected: false, default: false, travels: false, machine: "mac", called: [], saves_notes: true },
  { name: "lab", connected: true, default: false, travels: false, machine: "linux", called: [], saves_notes: false },
  { name: "old", connected: true, default: false, travels: false, machine: null, called: [], saves_notes: false },
];

// What is POSTed, as the page sends it, with each answered as given - everything else as the stand-in does.
function posting(shown, said = { success: true, message: "Done." }) {
  const posts = [];
  const asked = shown.context.fetch;
  shown.context.fetch = async (path, options) => {
    if (!options || options.method !== "POST") return asked(path, options);
    posts.push({ path, ...JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => said, text: async () => "" };
  };
  return posts;
}

test("a satellite's row says what machine it is, if it says, and whether it saves notes", async () => {
  const shown = page({ state: { ...STATE, satellites: MACHINES_SEEN, satellite: "home-assistant" } });
  await shown.context.load();
  assert.equal(badgesOf(satelliteRow(shown, "home-assistant")).join("|"), "Raspberry Pi|Default");
  assert.equal(badgesOf(satelliteRow(shown, "pc")).join("|"), "PC");
  assert.equal(badgesOf(satelliteRow(shown, "mac")).join("|"), "Mac");
  // A Linux computer which isn't a Pi, and one which doesn't say, are shown without.
  assert.equal(badgesOf(satelliteRow(shown, "lab")).join("|"), "");
  assert.equal(badgesOf(satelliteRow(shown, "old")).join("|"), "");
  // It saves notes where it says it does, in a line of its own, never where it doesn't - the Pi, say.
  const notes = (name) => detailsOf(satelliteRow(shown, name)).includes("Saves notes to its Downloads");
  assert.equal(`${notes("home-assistant")} ${notes("pc")} ${notes("mac")} ${notes("lab")} ${notes("old")}`,
               "false true true false false");
});

test("a satellite's row has a box for what else it's called, which holds them as written and sends the text on change", async () => {
  const shown = page({ state: { ...STATE, satellites: MACHINES_SEEN, satellite: null } });
  await shown.context.load();
  const box = calledBox(satelliteRow(shown, "pc"));
  assert.equal(box.value, "Desk, computer");
  assert.equal(calledBox(satelliteRow(shown, "home-assistant")).value, "");
  // Said in words by the label round it, which holds it, so it needs no id.
  const label = box.parent;
  assert.equal(`${label.tag} ${label.textContent.startsWith("Also called")} ${label.contains(box)}`, "label true true");
  // Nothing is sent as the page draws, nor as it's typed in.
  const posts = posting(shown);
  box.value = "Desk, , Study ";
  assert.equal(posts.length, 0);
  for (const listener of box.listeners.change || []) await listener({});
  assert.equal(posts.length, 1);
  assert.equal(`${posts[0].path}|${posts[0].name}|${posts[0].called}`, "/api/satellites/called|pc|Desk, , Study ");
  // The text as typed, so the brain's trimming and checking are the same wherever it's sent from.
  assert.equal(Object.keys(posts[0]).sort().join(), "called,name,path");
});

test("a change of names which was refused, or didn't save, is drawn as it is - not as typed", async () => {
  const shown = page({ state: { ...STATE, satellites: MACHINES_SEEN, satellite: null } });
  await shown.context.load();
  const posts = posting(shown, { success: false, message: "Desk is already what you call pc." });
  const box = calledBox(satelliteRow(shown, "mac"));
  box.value = "Desk";
  for (const listener of box.listeners.change || []) await listener({});
  assert.equal(posts.length, 1);
  // Drawn again from what the brain says, which has none: the refused name isn't left in the box.
  assert.equal(calledBox(satelliteRow(shown, "mac")).value, "");
  assert.equal(calledBox(satelliteRow(shown, "mac")) === box, false);
});

test("a row isn't drawn over while a satellite's names are typed, and is once the box is left", async () => {
  const shown = page({ state: { ...STATE, satellites: MACHINES_SEEN, satellite: null } });
  await shown.context.load();
  const box = calledBox(satelliteRow(shown, "pc"));
  box.focus();
  box.value = "Desk, Stu";
  // Something else about a satellite changed, as it does every time one starts speaking.
  shown.given.state = { ...STATE, satellites: MACHINES_SEEN.map((one) => ({ ...one, connected: !one.connected })), satellite: null };
  await shown.context.load();
  assert.equal(calledBox(satelliteRow(shown, "pc")) === box, true);
  assert.equal(box.value, "Desk, Stu");
  box.blur();
  await shown.context.load();
  assert.equal(calledBox(satelliteRow(shown, "pc")) === box, false);
  assert.equal(calledBox(satelliteRow(shown, "pc")).value, "Desk, computer");
});

test("a setting is found by the words of its key too, though its label alone is shown", async () => {
  const shown = page({ state: { ...STATE, settings: [number("journeys.buffer_minutes", 10), ...QUIET] } });
  await shown.loadAll();
  const found = (query) => {
    shown.type(query);
    return `${shown.rows().join(",")}: ${shown.hits().map((one) => one.textContent).join("|")}`;
  };
  assert.equal(found("buffer"), "journeys: Minutes to spare when working out when to leave");
  assert.equal(found("buffer minutes"), "journeys: Minutes to spare when working out when to leave");
  assert.equal(found("quiet from"), "notifications: Quiet hours start at");
  // The hit's words and its accessible name are the label, not the key's.
  shown.type("buffer");
  assert.equal(shown.hits()[0].getAttribute("aria-label"), "Minutes to spare when working out when to leave, in Journeys");
  // And one it has no words of is not found.
  assert.equal(found("buffer volume"), ": ");
});

test("a key's field for a new key is not found until the key is open, as CSS hides it till then", async () => {
  const shown = page();
  await shown.loadAll();
  const key = Object.assign(new Node("form"), {});
  key.className = "tile key";
  const label = Object.assign(new Node("label"), { htmlFor: "key-CLAUDE" });
  label.textContent = "Claude";
  const opener = Object.assign(new Node("button"), {});
  opener.textContent = "Replace";
  const entry = new Node("div");
  entry.className = "entry";
  const paste = Object.assign(new Node("input"), { placeholder: "Paste it here", id: "key-CLAUDE" });
  const save = Object.assign(new Node("button"), {});
  save.textContent = "Save the key";
  entry.append(paste, save);
  key.append(label, opener, entry);
  shown.node("section-keys").append(key);
  const found = (query) => {
    shown.type(query);
    return `${shown.rows().join(",")}: ${shown.hits().map((one) => one.textContent).join("|")}`;
  };
  assert.equal(found("paste"), ": ");
  assert.equal(found("save the key"), ": ");
  // What is shown of it is found.
  assert.equal(found("replace"), "keys: Replace");
  key.classList.add("open");
  assert.equal(found("paste"), "keys: Paste it here");
  assert.equal(found("save the key"), "keys: Save the key");
});

test("a setting says when it's used only if that isn't straight away", async () => {
  const later = { ...number("music.player", 1), used: "Used once Nova restarts" };
  const shown = page({ state: { ...STATE, settings: [later, ...QUIET] } });
  await shown.context.load();
  assert.equal(shown.node("setting-music.player-used").textContent, "Used once Nova restarts");
  assert.equal(shown.node("setting-music.player").getAttribute("aria-describedby"), "setting-music.player-used");
  const field = (key) => shown.node(`setting-${key}`).closest(".field");
  assert.equal(field("ntfy.quiet_from").textContent.includes("Used"), false);
  assert.equal(shown.node("setting-ntfy.quiet_from").getAttribute("aria-describedby"), null);
  // The second night setting is described by its note once the two are the same, and by nothing before.
  const until = shown.node("setting-ntfy.quiet_until");
  assert.equal(until.getAttribute("aria-describedby"), null);
  until.value = shown.node("setting-ntfy.quiet_from").value;
  shown.context.showNight();
  assert.equal(until.getAttribute("aria-describedby"), "setting-ntfy.quiet_until-night");
  until.value = "07:00";
  shown.context.showNight();
  assert.equal(until.getAttribute("aria-describedby"), null);
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

// The opening row: what the brain says of a satellite which has said it opens apps and pages.
const OPENING = [
  { name: "pc", connected: true, default: false, travels: false, machine: "pc", called: [], saves_notes: true,
    opens: true, opens_on: true, apps_found: 42, app_names: { gta: "Grand Theft Auto V Enhanced", fc: "EA SPORTS FC 25" },
    pc_device: null },
  { name: "mac", connected: true, default: false, travels: false, machine: "mac", called: [], saves_notes: true,
    opens: false, opens_on: true, apps_found: 0, app_names: {}, pc_device: null },
];
// The box of the app names, which is the one with the placeholder, and the opening switch, by its words.
const appNamesBox = (row) => row.querySelectorAll("input").find((one) => one.placeholder === "GTA = Grand Theft Auto V Enhanced");
const opensSwitch = (row) => row.querySelectorAll("label").find((one) => one.textContent.startsWith("Opens apps and pages"));

test("opens row shown only when satellite.opens", async () => {
  const shown = page({ state: { ...STATE, satellites: OPENING, satellite: null } });
  await shown.context.load();
  const row = satelliteRow(shown, "pc");
  const label = opensSwitch(row);
  assert.equal(label.querySelector("input").checked, true);
  assert.equal(label.querySelector("input").getAttribute("role"), "switch");
  assert.ok(detailsOf(row).includes("Found 42 apps and games."));
  const box = appNamesBox(row);
  assert.equal(box.value, "gta = Grand Theft Auto V Enhanced, fc = EA SPORTS FC 25");
  assert.equal(box.parent.textContent.startsWith("Apps also called"), true);
  // One which has never said it opens things shows none of it.
  const mac = satelliteRow(shown, "mac");
  assert.equal(`${opensSwitch(mac)} ${appNamesBox(mac)} ${detailsOf(mac).some((one) => one.startsWith("Found"))}`,
               "undefined undefined false");
});

test("the opening switch and the app names are sent as they are changed", async () => {
  const shown = page({ state: { ...STATE, satellites: OPENING, satellite: null } });
  await shown.context.load();
  const posts = posting(shown);
  const row = satelliteRow(shown, "pc");
  const box = opensSwitch(row).querySelector("input");
  box.checked = false;
  for (const listener of box.listeners.change || []) await listener({});
  const names = appNamesBox(satelliteRow(shown, "pc"));
  names.value = "GTA = Grand Theft Auto V Enhanced ";
  for (const listener of names.listeners.change || []) await listener({});
  assert.equal(JSON.stringify(posts), JSON.stringify([
    { path: "/api/satellites/opens", name: "pc", on: false },
    { path: "/api/satellites/app-names", name: "pc", text: "GTA = Grand Theft Auto V Enhanced " }]));
});

// The shortcut a push-to-talk satellite listens on: its box, by its words, and the line which says another program has it.
const SHORTCUTS = [
  { name: "pc", connected: true, default: false, travels: false, machine: "pc", called: [], saves_notes: true,
    push_to_talk: true, shortcut: "ctrl+alt+n", shortcut_taken: false },
  { name: "mac", connected: true, default: false, travels: false, machine: "mac", called: [], saves_notes: true,
    push_to_talk: true, shortcut: "", shortcut_taken: true },
  { name: "home-assistant", connected: true, default: true, travels: false, machine: "pi", called: [], saves_notes: false,
    push_to_talk: false, shortcut: "ctrl+alt+n", shortcut_taken: false },
];
const shortcutBox = (row) => row.querySelectorAll("input").find((one) => one.placeholder === "none");
const IN_USE = "In use by another program on that computer.";

test("a push-to-talk satellite has a box for its shortcut, and a wake-word one has none", async () => {
  const shown = page({ state: { ...STATE, satellites: SHORTCUTS, satellite: null } });
  await shown.context.load();
  const box = shortcutBox(satelliteRow(shown, "pc"));
  assert.equal(box.value, "ctrl+alt+n");
  // Said in words by the label which holds it, as the other names' box is.
  assert.equal(`${box.parent.tag} ${box.parent.textContent.startsWith("Shortcut to listen")}`, "label true");
  // None is an empty box, which the placeholder says.
  assert.equal(shortcutBox(satelliteRow(shown, "mac")).value, "");
  assert.equal(shortcutBox(satelliteRow(shown, "home-assistant")), undefined);
  // The names' box is still the first, and the shortcut's is another.
  assert.notEqual(calledBox(satelliteRow(shown, "pc")), box);
});

test("the line about a shortcut in use is drawn only when it is", async () => {
  const shown = page({ state: { ...STATE, satellites: SHORTCUTS, satellite: null } });
  await shown.context.load();
  const inUse = (name) => detailsOf(satelliteRow(shown, name)).includes(IN_USE);
  assert.equal(`${inUse("pc")} ${inUse("mac")} ${inUse("home-assistant")}`, "false true false");
});

test("a shortcut is sent as it is changed, and the rows drawn again", async () => {
  const shown = page({ state: { ...STATE, satellites: SHORTCUTS, satellite: null } });
  await shown.context.load();
  const posts = posting(shown);
  const box = shortcutBox(satelliteRow(shown, "pc"));
  box.value = "Ctrl + Shift + F5 ";
  assert.equal(posts.length, 0);
  for (const listener of box.listeners.change || []) await listener({});
  assert.equal(JSON.stringify(posts), JSON.stringify([
    { path: "/api/satellites/shortcut", name: "pc", text: "Ctrl + Shift + F5 " }]));
  // Enter ends the typing, as it does in the names' box.
  let left = 0;
  box.blur = () => { left += 1; };
  for (const listener of box.listeners.keydown || []) await listener({ key: "Enter" });
  assert.equal(left, 1);
});

// What /api/home-choices says, with the PCs a satellite can be chosen from.
const choices = (satellite_pcs) => ({ available: true, lists: [], sending: [], weather: null, pcs: null, satellite_pcs });

test("a PC choice is shown only when Home Assistant lists more than one PC", async () => {
  const two = [{ ...OPENING[0], pc_device: "laptop" }, OPENING[1]];
  const shown = page({ state: { ...STATE, satellites: two, satellite: null },
                       choices: choices([{ device: "desk", name: "Desk" }, { device: "laptop", name: "Laptop" }]) });
  const choose = (row) => {
    const label = row.querySelectorAll("label").find((one) => one.textContent.startsWith("HASS.Agent PC"));
    return label ? label.querySelector("select") : undefined;
  };
  await shown.context.load();
  // Not until Home Assistant's choices have come: the state alone has no PCs.
  assert.equal(choose(satelliteRow(shown, "pc")), undefined);
  await shown.context.loadHomeChoices();
  const select = choose(satelliteRow(shown, "pc"));
  assert.equal(select.value, "laptop");
  assert.equal(select.querySelectorAll("option").map((one) => one.textContent).join("|"), "Not chosen|Desk|Laptop");
  assert.equal(choose(satelliteRow(shown, "mac")), undefined);
  const posts = posting(shown);
  select.value = "desk";
  for (const listener of select.listeners.change || []) await listener({});
  assert.equal(JSON.stringify(posts), JSON.stringify([{ path: "/api/satellites/pc", name: "pc", device: "desk" }]));
  // With one PC, or none, there is nothing to choose from.
  for (const none of [[], [{ device: "desk", name: "Desk" }]]) {
    const one = page({ state: { ...STATE, satellites: OPENING, satellite: null }, choices: choices(none) });
    await one.context.load();
    await one.context.loadHomeChoices();
    assert.equal(choose(satelliteRow(one, "pc")), undefined);
  }
});
