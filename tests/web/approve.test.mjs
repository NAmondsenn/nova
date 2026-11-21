// The phone's approval page - see voice_assistant/web/approve.js and approve.html.
//
// The script is run in a context of its own over a small stand-in for the page, as the settings
// page's is in settings_sections.test.mjs: just enough of the DOM, the address, a fetch which
// answers as Nova would and a passkey prompt which answers as the phone would. Only small
// strings, numbers and booleans are compared, never the stand-in's nodes.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SCRIPT = readFileSync(new URL("../../voice_assistant/web/approve.js", import.meta.url), "utf8");
// Its base64url helpers, loaded before it as the page loads them.
const BASE64URL = readFileSync(new URL("../../voice_assistant/web/base64url.js", import.meta.url), "utf8");
const HTML = readFileSync(new URL("../../voice_assistant/web/approve.html", import.meta.url), "utf8");

const ID = "0123456789abcdef0123456789abcdef";

// An element, as far as the page's script uses one.
class Node {
  constructor(tag) {
    this.tag = tag;
    this.text = "";
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }

  addEventListener(kind, listener) {
    (this.listeners[kind] ||= []).push(listener);
  }

  // Each of the listeners for a click, waited for.
  click() {
    return Promise.all((this.listeners.click || []).map((listener) => listener()));
  }
}

const bytes = (...values) => Uint8Array.from(values).buffer;

// The script over a stand-in page at `path`, with Nova saying `request` of it and the phone's
// passkey prompt doing `prompt` (a function of the options it was given).
async function page({ path = `/approve/${ID}`, request = null, prompt = null } = {}) {
  const nodes = new Map();
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Node("div"));
      return nodes.get(id);
    },
  };
  const asked = [];
  const posted = [];
  const answers = { "/api/approvals/answer": { success: true, message: "Approved." },
                    "/api/approvals/refuse": { success: true, message: "Refused." } };
  const answer = (body) => ({ ok: true, status: 200, json: async () => body });
  const prompts = [];
  const context = vm.createContext({
    document,
    location: { pathname: path },
    console,
    atob,
    btoa,
    setTimeout,
    clearTimeout,
    navigator: {
      credentials: {
        async get(options) {
          prompts.push(options);
          return prompt(options);
        },
      },
    },
    async fetch(url, options = {}) {
      if (options.method === "POST") {
        posted.push({ url, headers: options.headers, body: JSON.parse(options.body) });
        return answer(answers[url]);
      }
      asked.push(url);
      return answer(request);
    },
  });
  vm.runInContext(BASE64URL, context, { filename: "base64url.js" });
  vm.runInContext(SCRIPT, context, { filename: "approve.js" });
  await context.NovaApprove.ready;
  const node = (id) => document.getElementById(id);
  return { context, node, asked, posted, prompts, answers,
           // What the page shows, in words: the description if shown, the buttons if shown, and what it said.
           shows: () => [node("description").hidden ? "" : node("description").textContent,
                         node("buttons").hidden ? "" : "buttons", node("said").hidden ? "" : node("said").textContent]
             .filter(Boolean).join(" | ") };
}

const OPEN = { state: "open", description: "Remove 'Dentist', Tue 25 Nov 10:00", challenge: "AAEC_w",
               credentials: ["AQID", "BAUG"], rp_id: "nova.example.ts.net" };

test("the id is read from the path", () => {
  const context = vm.createContext({ atob, btoa, document: undefined });
  vm.runInContext(BASE64URL, context);
  vm.runInContext(SCRIPT, context);
  const { idFromPath, b64urlToBytes } = context.NovaApprove;
  // base64url.js's own, handed on.
  assert.equal(b64urlToBytes, context.NovaBase64url.b64urlToBytes);
  assert.equal(idFromPath(`/approve/${ID}`), ID);
  for (const path of ["/approve/", `/approve/${ID}/`, `/approve/${ID}0`, `/approve/${ID.toUpperCase()}`,
                      `/approve/${ID.slice(1)}`, `/other/approve/${ID}`, "/", "", null, undefined]) {
    assert.equal(idFromPath(path), null, String(path));
  }
});

test("an open request shows its description and both buttons", async () => {
  const shown = await page({ request: { ...OPEN, description: "<b>Remove</b> 'Dentist'" } });
  assert.deepEqual(shown.asked, [`/api/approvals/request?id=${ID}`]);
  // As text, never as markup.
  assert.equal(shown.shows(), "<b>Remove</b> 'Dentist' | buttons");
  assert.equal(shown.node("approve").disabled, false);
  assert.equal(shown.node("refuse").disabled, false);
});

test("approving posts the assertion as base64url with the page's header", async () => {
  const shown = await page({
    request: OPEN,
    prompt: () => ({ id: "AQID", rawId: bytes(1, 2, 3), type: "public-key",
                     response: { clientDataJSON: bytes(123, 125), authenticatorData: bytes(5, 6, 7, 250),
                                 signature: bytes(48, 69, 2, 33, 254) } }),
  });
  await shown.node("approve").click();
  // The passkey prompt, for this request's challenge and his passkeys, needing his fingerprint or PIN.
  const asked = JSON.parse(JSON.stringify(shown.prompts[0].publicKey, (key, value) =>
    (value && value.byteLength !== undefined ? Array.from(new Uint8Array(value)) : value)));
  assert.deepEqual(asked, { challenge: [0, 1, 2, 255], rpId: "nova.example.ts.net",
                            allowCredentials: [{ type: "public-key", id: [1, 2, 3] }, { type: "public-key", id: [4, 5, 6] }],
                            userVerification: "required", timeout: 120000 });
  assert.equal(shown.posted.length, 1);
  const { url, headers, body } = shown.posted[0];
  assert.equal(url, "/api/approvals/answer");
  assert.equal(headers["X-Nova"], "page");
  assert.equal(headers["Content-Type"], "application/json");
  assert.deepEqual(body, { id: ID, credential_id: "AQID", client_data: "e30", authenticator_data: "BQYH-g",
                           signature: "MEUCIf4" });
  assert.equal(shown.shows(), "Remove 'Dentist', Tue 25 Nov 10:00 | Approved.");
});

test("refusing posts the id alone, with no passkey asked for", async () => {
  const shown = await page({ request: OPEN });
  await shown.node("refuse").click();
  assert.equal(shown.prompts.length, 0);
  assert.deepEqual(shown.posted.map((one) => [one.url, one.headers["X-Nova"], JSON.stringify(one.body)]),
                   [["/api/approvals/refuse", "page", JSON.stringify({ id: ID })]]);
  assert.equal(shown.shows(), "Remove 'Dentist', Tue 25 Nov 10:00 | Refused.");
});

test("an answered request shows only Already answered.", async () => {
  const answered = await page({ request: { state: "answered", description: "Remove 'Dentist'", credentials: [], rp_id: null } });
  assert.equal(answered.shows(), "Already answered.");
  const expired = await page({ request: { state: "expired", description: "Remove 'Dentist'", credentials: [], rp_id: null } });
  assert.equal(expired.shows(), "This has run out.");
  const unknown = await page({ request: { state: "unknown", description: "", credentials: [], rp_id: null } });
  assert.equal(unknown.shows(), "There's no such request.");
  // Nor is Nova asked of an address which is no request's.
  const nowhere = await page({ path: "/approve/nonsense" });
  assert.deepEqual(nowhere.asked, []);
  assert.equal(nowhere.shows(), "There's no such request.");
  // Answered elsewhere while the page was open: Nova says so, and only that is shown.
  const late = await page({ request: OPEN, prompt: () => ({ id: "AQID", rawId: bytes(1, 2, 3),
    response: { clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3) } }) });
  late.answers["/api/approvals/answer"] = { success: false, message: "Already answered." };
  await late.node("approve").click();
  assert.equal(late.shows(), "Already answered.");
});

test("an open request missing its description or challenge says so plainly, with no buttons", async () => {
  for (const missing of [{ description: "" }, { description: undefined }, { challenge: undefined }, { challenge: 7 }]) {
    const shown = await page({ request: { ...OPEN, ...missing } });
    assert.equal(shown.shows(), "Nova didn't send this request properly. Open the notification again.", JSON.stringify(missing));
  }
});

test("the passkeys' relying party is left out only when Nova gives none", async () => {
  const signed = () => ({ id: "AQID", rawId: bytes(1, 2, 3),
                          response: { clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3) } });
  const without = await page({ request: { ...OPEN, rp_id: null }, prompt: signed });
  await without.node("approve").click();
  assert.equal("rpId" in without.prompts[0].publicKey, false);
  const given = await page({ request: OPEN, prompt: signed });
  await given.node("approve").click();
  assert.equal(given.prompts[0].publicKey.rpId, "nova.example.ts.net");
});

test("a cancelled passkey prompt keeps the buttons", async () => {
  const shown = await page({
    request: OPEN,
    prompt: () => {
      const error = new Error("The operation either timed out or was not allowed.");
      error.name = "NotAllowedError";
      throw error;
    },
  });
  await shown.node("approve").click();
  assert.equal(shown.posted.length, 0);
  assert.equal(shown.shows().startsWith("Remove 'Dentist', Tue 25 Nov 10:00 | buttons"), true, shown.shows());
  assert.equal(shown.node("approve").disabled, false);
  assert.equal(shown.node("refuse").disabled, false);
  // And a passkey which didn't check out leaves them too, to try again.
  shown.context.navigator.credentials.get = async () => ({ id: "AQID", rawId: bytes(1, 2, 3),
    response: { clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3) } });
  shown.answers["/api/approvals/answer"] = { success: false, message: "That didn't check out." };
  await shown.node("approve").click();
  assert.equal(shown.shows(), "Remove 'Dentist', Tue 25 Nov 10:00 | buttons | That didn't check out.");
});

test("the page has each id the script asks for, exactly once, and loads its look from the settings page", () => {
  const asked = new Set([...SCRIPT.matchAll(/getElementById\("([^"]+)"\)/g)].map((one) => one[1]));
  assert.equal(asked.size >= 5, true);
  const wrong = [...asked].filter((id) => HTML.split(`id="${id}"`).length - 1 !== 1);
  assert.equal(wrong.join(","), "");
  // At /approve/<id>, so its files are asked for from the top.
  assert.equal(HTML.includes('<link rel="stylesheet" href="/settings.css">'), true);
  assert.equal(HTML.includes('<script src="/approve.js" defer></script>'), true);
  assert.equal(HTML.indexOf('<script src="/base64url.js" defer></script>') < HTML.indexOf('<script src="/approve.js"'), true);
  assert.equal(HTML.includes('<script src="/base64url.js" defer></script>'), true);
});
