// Base64url, shared by the settings page and the phone's approval page - see
// voice_assistant/web/base64url.js. Run in an empty context, as the page's other scripts are.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SCRIPT = readFileSync(new URL("../../voice_assistant/web/base64url.js", import.meta.url), "utf8");
const context = vm.createContext({ atob, btoa });
vm.runInContext(SCRIPT, context, { filename: "base64url.js" });
const { b64urlToBytes, bytesToB64url } = context.NovaBase64url;

const bytes = (...values) => Uint8Array.from(values).buffer;

test("base64url round trips bytes", () => {
  // Every byte, so + and / (as - and _) and the padding left off are all there.
  const all = Uint8Array.from({ length: 256 }, (_, at) => at);
  const text = bytesToB64url(all.buffer);
  assert.equal(/^[A-Za-z0-9_-]+$/.test(text), true);
  assert.equal(Array.from(b64urlToBytes(text)).join(","), Array.from(all).join(","));
  assert.equal(bytesToB64url(bytes(0, 1, 2, 255)), "AAEC_w");
  assert.equal(Array.from(b64urlToBytes("AAEC_w")).join(","), "0,1,2,255");
  // A view of part of a buffer gives only its own bytes.
  assert.equal(bytesToB64url(new Uint8Array([9, 0, 1, 2, 255, 9]).subarray(1, 5)), "AAEC_w");
  assert.equal(bytesToB64url(bytes()), "");
});

test("both pages load it before their own script", () => {
  const web = (name) => readFileSync(new URL(`../../voice_assistant/web/${name}`, import.meta.url), "utf8");
  const settings = web("settings.html");
  assert.equal(settings.indexOf('<script src="base64url.js" defer></script>') > 0, true);
  assert.equal(settings.indexOf('<script src="base64url.js"') < settings.indexOf('<script src="settings.js"'), true);
  // And the settings page uses it rather than helpers of its own.
  const script = web("settings.js");
  assert.equal(/function (b64urlToBytes|bytesToB64url)/.test(script), false);
  assert.equal(script.includes("NovaBase64url.bytesToB64url("), true);
});
