// Base64url, as Nova sends and takes every binary part of a passkey: no padding, - and _ for
// + and /. Shared by the settings page, which adds passkeys, and the phone's approval page,
// which answers with one. It sets one global, NovaBase64url, and is loaded before either's script.

"use strict";

(() => {
  const b64urlToBytes = (text) => {
    const plain = String(text).replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(plain + "=".repeat((4 - (plain.length % 4)) % 4));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  };

  // An ArrayBuffer, or a view of part of one, as the browser gives a passkey's parts.
  const bytesToB64url = (buffer) => {
    const view = ArrayBuffer.isView(buffer)
      ? new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength) : new Uint8Array(buffer);
    let binary = "";
    for (const byte of view) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };

  globalThis.NovaBase64url = { b64urlToBytes, bytesToB64url };
})();
