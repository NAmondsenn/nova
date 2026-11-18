// A feature's settings in its own section, and the one save bar - see
// voice_assistant/web/settings.js, settings.html and settings_list.js.
//
// The page's scripts are run in a context of their own, settings_list.js then settings.js, over
// a small stand-in for the page: nodes which keep their children, attributes and classes, the
// blocks the HTML names for the settings, and a fetch which answers as Nova would. Only small
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

// The sections which have a block for their settings in the page.
const BLOCKS = [...HTML.matchAll(/<div class="settings-of" data-settings="([a-z]+)"/g)].map((one) => one[1]);

// What has the focus, as the stand-in page has it.
let active = null;

// An element, as far as the page's script uses one.
class Node {
  constructor(tag) {
    this.tag = tag;
    this.text = "";
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.dataset = {};
    this.style = { setProperty() {} };
    this.listeners = {};
    this.attributes = new Map();
    this.classes = new Set();
    this.held = "";
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

  // A value is always text, as the browser's is.
  get value() {
    return this.held;
  }

  set value(value) {
    this.held = String(value);
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }

  // Text is a node of its own here, as the page appends it as well as elements.
  append(...nodes) {
    this.children.push(...this.adopt(nodes));
  }

  replaceChildren(...nodes) {
    this.text = "";
    this.children = this.adopt(nodes);
  }

  adopt(nodes) {
    return nodes.map((node) => {
      const made = typeof node === "string" ? Object.assign(new Node("#text"), { text: node }) : node;
      made.parent = this;
      return made;
    });
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

  // The nearest of this node and the ones above it with the class named, as ".field".
  closest(selector) {
    const name = selector.slice(1);
    for (let node = this; node; node = node.parent) if (node.classes.has(name)) return node;
    return null;
  }

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }

  focus() {
    active = this;
  }
}

// Every node under one, in order.
function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

// What /api/state says, with no settings.
const STATE = {
  wake_word: "Hey Nova", muted: false, satellites: [], satellites_unreadable: false, satellite: null,
  status: { reply_ms: null, replies: 0, started: 1700000000, speech_to_text: "ready", ntfy: null, problems: [] },
  volumes: {}, clock: { timers: [], alarms: [] }, memories: [], routines: [],
  displays: { port: 0, list: [] }, keys: [], settings: [], settings_problem: "", restart_needed: false, page: "one",
};

// A setting as /api/state gives one, with made-up values.
const number = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "number", value,
                                  form: "number", min: 0, max: 100, whole: true });
const yesNo = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "yes/no", value });
const timeOf = (key, value) => ({ key, label: `Label of ${key}`, used: "Used straight away", kind: "text", value,
                                  form: "time" });
const SETTINGS = [number("football.kickoff_minutes", 15), number("voice.speed", 1),
                  yesNo("sounds.reminder_enabled", true), number("made.up", 7)];

// The page's scripts over a stand-in page, the save answered as `saved`.
function page({ state = { ...STATE, settings: SETTINGS }, saved = { success: true, message: "Saved." } } = {}) {
  const nodes = new Map();
  const blocks = new Map(BLOCKS.map((id) => [id, new Node("div")]));
  const listeners = {};
  const posted = [];
  const everything = () => [...blocks.values()].flatMap((block) => descendants(block));
  const document = {
    hidden: false,
    body: new Node("body"),
    get activeElement() {
      return active;
    },
    getElementById(id) {
      const found = everything().find((one) => one.id === id);
      if (found) return found;
      if (!nodes.has(id)) nodes.set(id, new Node("div"));
      return nodes.get(id);
    },
    createElement: (tag) => new Node(tag),
    createElementNS: (namespace, tag) => new Node(tag),
    // The only two things the settings are looked for by: a section's block, and what's
    // part of the settings form wherever it's drawn.
    querySelector(selector) {
      const block = /^\[data-settings="([a-z]+)"\]$/.exec(selector);
      return block ? blocks.get(block[1]) || null : null;
    },
    querySelectorAll(selector) {
      if (!selector.includes('[form="settings"]')) return [];
      return everything().filter((one) => ["input", "select"].includes(one.tag) && one.getAttribute("form") === "settings");
    },
    addEventListener(kind, listener) {
      (listeners[kind] ||= []).push(listener);
    },
  };
  const window = { scrollY: 0, matchMedia: () => ({ matches: false }), scrollTo() {}, addEventListener() {} };
  const answers = { state };
  const answer = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => "" });
  const context = vm.createContext({
    document,
    window,
    location: { hash: "", pathname: "/settings", search: "" },
    history: { state: null, pushState() {}, replaceState() {}, back() {} },
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
    async fetch(path, options) {
      if (path === "/api/login-state") return answer({ route: "direct", logged_in: true, password_set: true, page: "one" });
      if (path === "/api/state") return answer(answers.state);
      if (path === "/api/settings") {
        posted.push(options.body);
        return answer(saved);
      }
      return answer({});
    },
  });
  vm.runInContext(LIST, context);
  vm.runInContext(SCRIPT, context);
  active = null;
  context.setUpSettings();
  const node = (id) => document.getElementById(id);
  const shown = {
    context,
    node,
    answers,
    posted,
    block: (id) => blocks.get(id),
    // The headings in a block, as one string.
    headings: (id) => descendants(blocks.get(id)).filter((one) => one.tag === "h3").map((one) => one.textContent).join(","),
    // The input of a setting, wherever it is drawn.
    input: (key) => everything().find((one) => one.name === key),
    // The words of what's said above the block's settings, or null with none drawn.
    problemIn: (id) => {
      const first = blocks.get(id).children[0];
      return first && first.classes.has("problem") ? first : null;
    },
    // A change made in an input, as the browser tells the page of one.
    typed(key, value) {
      const input = this.input(key);
      if (input.type === "checkbox") input.checked = value;
      else input.value = value;
      for (const listener of listeners.input || []) listener({ target: input });
    },
    // The save bar's words.
    said: () => node("unsaved").textContent,
    async submit() {
      let prevented = false;
      for (const listener of node("settings").listeners.submit || []) await listener({ preventDefault: () => (prevented = true) });
      assert.equal(prevented, true);
    },
  };
  return shown;
}

// What the page does as it is shown, logged in: its first look at Nova.
async function arrived(shown) {
  await shown.context.load();
}

test("each setting is drawn in its feature's block, under the Contract's sub-heading where there is one", async () => {
  const shown = page();
  await arrived(shown);
  assert.equal(shown.block("football").children.length > 0, true);
  assert.equal(shown.input("football.kickoff_minutes").name, "football.kickoff_minutes");
  assert.equal(descendants(shown.block("football")).includes(shown.input("football.kickoff_minutes")), true);
  assert.equal(shown.headings("football"), "");
  assert.equal(descendants(shown.block("sound")).includes(shown.input("voice.speed")), true);
  assert.equal(descendants(shown.block("sound")).includes(shown.input("sounds.reminder_enabled")), true);
  assert.equal(shown.headings("sound"), "Voice,Sounds");
  assert.equal(descendants(shown.block("else")).includes(shown.input("made.up")), true);
  assert.equal(shown.headings("else"), "Other");
});

test("sub-headings are in the old groups' order, whatever order Nova lists the settings in", async () => {
  const keys = ["thresholds.voice_check", "sounds.chime_enabled", "voice.speed", "wrapped.enabled", "music.provider",
                "logging.audio", "mqtt.host", "made.up"];
  const shown = page({ state: { ...STATE, settings: keys.map((key) => number(key, 1)) } });
  await arrived(shown);
  assert.equal(shown.headings("music"), "Music,Wrapped");
  assert.equal(shown.headings("sound"), "Voice,Sounds,Listening");
  assert.equal(shown.headings("else"), "Home,Privacy,Other");
});

test("a section with no settings on this Nova has no block drawn and no sub-heading", async () => {
  const shown = page();
  await arrived(shown);
  for (const id of ["claude", "notifications", "music", "journeys", "displays"]) {
    assert.equal(shown.block(id).hidden, true, id);
    assert.equal(shown.block(id).children.length, 0, id);
  }
  assert.equal(shown.block("football").hidden, false);
  // And one drawn goes again when its settings are no longer there.
  shown.answers.state = { ...STATE, settings: [number("made.up", 7)] };
  await shown.context.load();
  assert.equal(shown.block("football").hidden, true);
  assert.equal(shown.block("football").children.length, 0);
  assert.equal(shown.block("sound").hidden, true);
});

test("every input is part of the settings form, wherever it is drawn", async () => {
  const shown = page();
  await arrived(shown);
  for (const setting of SETTINGS) {
    assert.equal(shown.input(setting.key).getAttribute("form"), "settings", setting.key);
  }
  // A choice's select too.
  const choice = { ...SETTINGS[0], key: "journeys.distance_unit", kind: "text", choices: ["miles", "km"], value: "km" };
  shown.answers.state = { ...STATE, settings: [choice] };
  await shown.context.load();
  assert.equal(shown.input("journeys.distance_unit").getAttribute("form"), "settings");
});

test("the bar is hidden while nothing is changed", async () => {
  const shown = page();
  await arrived(shown);
  assert.equal(shown.node("settings").hidden, true);
  assert.equal(shown.context.document.body.classes.has("save-bar-shown"), false);
});

test("a change in Football and one in Sound make the bar say 2 changes, and a new look redraws neither", async () => {
  const shown = page();
  await arrived(shown);
  shown.typed("football.kickoff_minutes", "20");
  assert.equal(shown.said(), "1 change not saved yet.");
  shown.typed("sounds.reminder_enabled", false);
  assert.equal(shown.said(), "2 changes not saved yet.");
  assert.equal(shown.node("settings").hidden, false);
  assert.equal(shown.context.document.body.classes.has("save-bar-shown"), true);
  assert.equal(shown.node("save-settings").disabled, false);
  assert.equal(shown.node("save-settings").hidden, false);
  assert.equal(shown.node("restart-now").hidden, true);
  const football = shown.input("football.kickoff_minutes");
  const sound = shown.input("sounds.reminder_enabled");
  // Nova says another value for each, and a new setting.
  shown.answers.state = { ...STATE, settings: [number("football.kickoff_minutes", 30), yesNo("sounds.reminder_enabled", true),
                                               number("made.up", 7)] };
  await shown.context.load();
  assert.equal(shown.input("football.kickoff_minutes") === football, true);
  assert.equal(shown.input("sounds.reminder_enabled") === sound, true);
  assert.equal(football.value, "20");
  assert.equal(sound.checked, false);
  assert.equal(shown.said(), "2 changes not saved yet.");
  // The one change back, and then the other: the bar goes.
  shown.typed("football.kickoff_minutes", "15");
  assert.equal(shown.said(), "1 change not saved yet.");
  shown.typed("sounds.reminder_enabled", true);
  assert.equal(shown.node("settings").hidden, true);
  assert.equal(shown.context.document.body.classes.has("save-bar-shown"), false);
});

test("the settings are drawn again once nothing is changed, but not while one has the focus", async () => {
  const shown = page();
  await arrived(shown);
  const was = shown.input("football.kickoff_minutes");
  shown.answers.state = { ...STATE, settings: [number("football.kickoff_minutes", 30)] };
  active = shown.input("voice.speed");
  await shown.context.load();
  assert.equal(shown.input("football.kickoff_minutes") === was, true, "kept while another section's has the focus");
  active = null;
  await shown.context.load();
  assert.equal(shown.input("football.kickoff_minutes") === was, false);
  assert.equal(shown.input("football.kickoff_minutes").value, "30");
});

test("saving saves every section's changes in one go, and the bar goes", async () => {
  const shown = page();
  await arrived(shown);
  shown.typed("football.kickoff_minutes", "20");
  shown.typed("sounds.reminder_enabled", false);
  await shown.submit();
  assert.equal(shown.posted.length, 1);
  assert.equal(shown.posted[0], JSON.stringify({ changes: { "football.kickoff_minutes": "20", "sounds.reminder_enabled": false } }));
  assert.equal(shown.node("settings").hidden, true);
  assert.equal(shown.said(), "Nothing's changed yet.");
});

test("a save which needs a restart keeps the bar, offering Restart now, until Nova has restarted", async () => {
  const shown = page({ saved: { success: true, message: "Saved. Some of it waits.", restart_needed: true } });
  await arrived(shown);
  shown.typed("football.kickoff_minutes", "20");
  await shown.submit();
  assert.equal(shown.node("settings").hidden, false);
  assert.equal(shown.said(), "Saved. Some of it waits.");
  assert.equal(shown.node("restart-now").hidden, false);
  assert.equal(shown.node("save-settings").hidden, true);
  assert.equal(shown.context.document.body.classes.has("save-bar-shown"), true);
  // Restarted: Nova no longer says it needs one.
  shown.answers.state = { ...STATE, settings: SETTINGS, restart_needed: false };
  await shown.context.load();
  assert.equal(shown.node("settings").hidden, true);
  assert.equal(shown.context.document.body.classes.has("save-bar-shown"), false);
});

test("a save which fails says so in the bar, which stays", async () => {
  const shown = page({ saved: { success: false, message: "Nova said no." } });
  await arrived(shown);
  shown.typed("football.kickoff_minutes", "20");
  await shown.submit();
  assert.equal(shown.said(), "Nova said no.");
  assert.equal(shown.node("settings").hidden, false);
  assert.equal(shown.node("unsaved").classes.has("bad"), true);
});

test("the quiet hours are in Notifications, with their ids, and say so when the two are the same", async () => {
  const quiet = [timeOf("ntfy.quiet_from", "23:00"), timeOf("ntfy.quiet_until", "07:00")];
  const shown = page({ state: { ...STATE, settings: quiet } });
  await arrived(shown);
  assert.equal(shown.node("setting-ntfy.quiet_from").name, "ntfy.quiet_from");
  assert.equal(descendants(shown.block("notifications")).includes(shown.node("setting-ntfy.quiet_until")), true);
  assert.equal(shown.node("setting-ntfy.quiet_until-night").hidden, true);
  shown.typed("ntfy.quiet_until", "23:00");
  assert.equal(shown.node("setting-ntfy.quiet_until-night").hidden, false);
  assert.equal(shown.said(), "1 change not saved yet.");
});

test("that Nova's settings can't be read is said at the top of every block drawn, and kept", async () => {
  const shown = page({ state: { ...STATE, settings: SETTINGS, settings_problem: "line 3 is wrong" } });
  await arrived(shown);
  for (const id of ["football", "sound", "else"]) {
    const problem = shown.problemIn(id);
    assert.equal(problem !== null, true, id);
    assert.equal(problem.tag, "p", id);
    assert.equal(problem.hidden, false, id);
    assert.equal(problem.classes.has("panel"), true, id);
    assert.equal(problem.textContent.includes("line 3 is wrong"), true, id);
    assert.equal(problem.textContent.startsWith("Couldn't read your saved settings"), true, id);
  }
  // The same words in each, none where there are no settings, and the problem kept for the list.
  assert.equal(shown.problemIn("football").textContent, shown.problemIn("else").textContent);
  assert.equal(shown.block("claude").children.length, 0);
  assert.equal(vm.runInContext("settingsProblem", shown.context), "line 3 is wrong");
  // Put right: gone from each, and from what's kept.
  shown.answers.state = { ...STATE, settings: SETTINGS, settings_problem: "" };
  await shown.context.load();
  assert.equal(shown.problemIn("football").hidden, true);
  assert.equal(shown.problemIn("else").textContent, "");
  assert.equal(vm.runInContext("settingsProblem", shown.context), "");
});

test("a problem is said in a block drawn after it was, and while a field is being changed in another", async () => {
  const shown = page({ state: { ...STATE, settings: [number("made.up", 7)], settings_problem: "broken" } });
  await arrived(shown);
  assert.equal(shown.problemIn("else").hidden, false);
  // Football's settings come to be, with the problem still there.
  shown.answers.state = { ...STATE, settings: [number("made.up", 7), number("football.kickoff_minutes", 15)],
                          settings_problem: "broken" };
  await shown.context.load();
  assert.equal(shown.problemIn("football").hidden, false);
  assert.equal(shown.problemIn("football").textContent.includes("broken"), true);
  // A field changed holds back the redraw but not the problem's going.
  shown.typed("made.up", "8");
  shown.answers.state = { ...STATE, settings: [number("made.up", 7), number("football.kickoff_minutes", 15)],
                          settings_problem: "" };
  await shown.context.load();
  assert.equal(shown.problemIn("football").hidden, true);
  assert.equal(shown.problemIn("else").hidden, true);
});

test("the page has a block as the last of each section which holds settings, and the bar outside them all", () => {
  const list = vm.createContext({});
  vm.runInContext(LIST, list);
  const wanted = vm.runInContext(
    "NovaList.SECTIONS.filter((section) => section.starts.length).map((section) => section.id).join(',')", list);
  assert.equal(BLOCKS.slice().sort().join(","), wanted.split(",").sort().join(","));
  for (const id of BLOCKS) {
    assert.equal(HTML.split(`data-settings="${id}"`).length - 1, 1, id);
    // Hidden until something is drawn in it, and the last thing in its section.
    assert.equal(HTML.includes(`<div class="settings-of" data-settings="${id}" hidden></div>\n        </section>`), true, id);
    const section = HTML.slice(HTML.indexOf(`id="section-${id}"`), HTML.indexOf("</section>", HTML.indexOf(`id="section-${id}"`)));
    assert.equal(section.includes(`data-settings="${id}"`), true, id);
  }
  // The old form's pieces are gone from the sections.
  assert.equal(HTML.includes("setting-groups"), false);
  assert.equal(HTML.includes('id="settings-problem"'), false);
  // The form is the save bar: after the last section, outside them all, and hidden to begin with.
  const form = HTML.indexOf('<form class="save-form" id="settings"');
  assert.equal(form > HTML.lastIndexOf("</section>"), true);
  assert.equal(HTML.slice(form).startsWith('<form class="save-form" id="settings" novalidate hidden>'), true);
  for (const id of ["unsaved", "save-settings", "restart-now"]) {
    assert.equal(HTML.indexOf(`id="${id}"`) > form, true, id);
  }
});
