// The clock display's microphone button - see voice_assistant/web/display.js.
//
// The page's scripts are run in a context of their own, display-gestures.js then display.js,
// over a small stand-in for the page: just enough of the DOM for the marks and a touch, a
// clock which never runs on by itself, and a fetch which answers as Nova would. Only small
// strings and booleans are compared, never the stand-in's nodes.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const web = (name) => readFileSync(new URL(`../../voice_assistant/web/${name}`, import.meta.url), "utf8");
const GESTURES = web("display-gestures.js");
const SCRIPT = web("display.js");

const KEY = "k".repeat(43);

// An element, as far as the page's script uses one.
class Node {
  constructor(id) {
    this.id = id;
    this.textContent = "";
    this.hidden = false;
    this.className = "";
    this.style = {};
    this.attributes = new Map();
    const classes = new Set();
    this.classes = classes;
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

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  replaceChildren() {}

  append() {}

  // Each stand-in node is a leaf, so it holds only itself.
  contains(other) {
    return other === this;
  }
}

// What Nova's state says, with whatever a test changes laid over it.
const nova = (changes = {}) => ({
  version: 1, now: 1795010880, utc_offset: 0, connected: true, nova: "idle", heard: "", muted: false,
  weather: null, events: [], music: null, volume: null, background: null, brightness: 80, home_after: 60,
  ...changes,
});

// Lets every promise waiting to run, run.
const settle = () => new Promise((resolve) => setImmediate(resolve));

// The page over a stand-in, Nova's first state being `first`. `answer(body)` is what Nova
// says to a tap - {status, data}, or an Error for Nova out of reach - and is called as the
// tap is sent.
async function clock({ first = nova(), answer = (body) => ({ status: 200, data: { muted: body.muted } }) } = {}) {
  const nodes = new Map();
  const listeners = {};
  const sent = [];
  let now = 0;
  const document = {
    hidden: false,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Node(id));
      return nodes.get(id);
    },
    createElement: () => new Node(""),
    querySelectorAll: () => [],
    addEventListener(kind, listener) {
      (listeners[kind] ||= []).push(listener);
    },
  };
  const fetch = (path, options) => {
    if (path === "/display/api/state") return Promise.resolve({ ok: true, status: 200, json: async () => first });
    // A long poll Nova holds for as long as the test runs.
    if (path.startsWith("/display/api/state?")) return new Promise(() => {});
    const body = JSON.parse(options.body);
    sent.push({ path, method: options.method, key: options.headers.Authorization, muted: body.muted });
    const said = answer(body);
    if (said instanceof Error) return Promise.reject(said);
    return Promise.resolve({ ok: said.status === 200, status: said.status, json: async () => said.data });
  };
  const context = vm.createContext({
    document,
    addEventListener() {},
    location: { hash: "", pathname: "/display", search: "" },
    history: { replaceState() {} },
    localStorage: { getItem: () => KEY, setItem() {} },
    performance: { now: () => now },
    fetch,
    AbortController,
    setTimeout: () => 0,
    clearTimeout() {},
    console,
  });
  // The page's window is its global, which display-gestures.js leaves NovaGestures on.
  context.window = context;
  vm.runInContext(GESTURES, context, { filename: "display-gestures.js" });
  vm.runInContext(SCRIPT, context, { filename: "display.js" });
  await settle();
  const fire = (kind, target) => {
    const finger = { identifier: 1, clientX: 760, clientY: 465 };
    const event = { target, touches: kind === "touchend" ? [] : [finger], changedTouches: [finger], preventDefault() {} };
    for (const listener of listeners[kind] || []) listener(event);
  };
  const mic = document.getElementById("mic");
  return {
    mic,
    sent,
    // Whether the button shows the microphone muted, and is shown at all.
    shown: () => ({ hidden: mic.hidden, muted: mic.classes.has("muted"), pressed: mic.attributes.get("aria-pressed") }),
    // Which page is open.
    page: () => ["home", "coming-up", "now-playing"].find((id) => !document.getElementById(id).hidden),
    // A quick tap on a node, the microphone or anything else.
    tap(target = mic) {
      fire("touchstart", target);
      now += 80;
      fire("touchend", target);
      now += 1000;
    },
  };
}

test("the microphone shows whether Nova's muted", async () => {
  assert.deepEqual((await clock()).shown(), { hidden: false, muted: false, pressed: "false" });
  assert.deepEqual((await clock({ first: nova({ muted: true }) })).shown(), { hidden: false, muted: true, pressed: "true" });
});

test("tapping the microphone mutes it with the clock's key, shown before Nova answers", async () => {
  const page = await clock();
  page.tap();
  // Drawn as the finger lifts, the answer not yet read.
  assert.deepEqual(page.shown(), { hidden: false, muted: true, pressed: "true" });
  assert.equal(page.mic.classes.has("pressed"), true);
  assert.deepEqual(page.sent, [{ path: "/display/api/mute", method: "POST", key: `Bearer ${KEY}`, muted: true }]);
  await settle();
  assert.deepEqual(page.shown(), { hidden: false, muted: true, pressed: "true" });
  // A tap on the microphone is its own, not a page turn.
  assert.equal(page.page(), "home");
});

test("tapping it while muted unmutes it", async () => {
  const page = await clock({ first: nova({ muted: true }) });
  page.tap();
  assert.equal(page.shown().muted, false);
  await settle();
  assert.deepEqual(page.sent.map((asked) => asked.muted), [false]);
  assert.equal(page.shown().muted, false);
});

test("two taps say what each wants, and the last one's answer counts", async () => {
  const page = await clock();
  page.tap();
  page.tap();
  assert.deepEqual(page.sent.map((asked) => asked.muted), [true, false]);
  await settle();
  assert.equal(page.shown().muted, false);
});

test("a tap Nova turns down, or can't hear, is put back as Nova has it", async () => {
  const refused = await clock({ answer: () => ({ status: 409, data: null }) });
  refused.tap();
  assert.equal(refused.shown().muted, true);
  await settle();
  assert.equal(refused.shown().muted, false);
  const away = await clock({ first: nova({ muted: true }), answer: () => new Error("gone") });
  away.tap();
  assert.equal(away.shown().muted, false);
  await settle();
  assert.equal(away.shown().muted, true);
});

test("a tap anywhere else still turns the page, and mutes nothing", async () => {
  const page = await clock();
  page.tap(new Node("time"));
  assert.equal(page.page(), "coming-up");
  assert.deepEqual(page.sent, []);
});

test("the microphone isn't there while Nova's blob is", async () => {
  const page = await clock({ first: nova({ nova: "listening" }) });
  assert.equal(page.shown().hidden, true);
});
