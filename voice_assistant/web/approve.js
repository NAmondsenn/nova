// The phone's approval page - see voice_assistant/approvals.py and settings_page.py.
//
// His phone's notification opens it at /approve/<request id>. It asks Nova for that request
// and shows what it is for, with Approve and Refuse. Approve asks the phone for his passkey,
// which needs his fingerprint, face or PIN, against the request's own challenge, and sends
// Nova what the phone signed; Nova checks it. Refuse needs no passkey. A request already
// answered, or run out, is said as that and nothing else. It sets one global, NovaApprove,
// for the tests, with base64url.js's helpers, which it uses.
//
// The notification's address ends ?go=1, and then the page starts Approve by itself, once,
// as it opens: a tap on the notification goes straight to his fingerprint. A browser which
// won't ask for a passkey without a tap of his own leaves the buttons, saying so.

"use strict";

(() => {
  // What Nova says for a request it doesn't know, or an address which is no request's.
  const NO_SUCH = "There's no such request.";
  const SAID = { answered: "Already answered.", expired: "This has run out.", unknown: NO_SUCH };
  // How long the phone's passkey prompt may wait for him, in milliseconds.
  const PROMPT_WAIT = 120000;

  // From base64url.js, loaded first.
  const { b64urlToBytes, bytesToB64url } = globalThis.NovaBase64url;

  // Whether the page's address asks it to start Approve by itself: ?go=1, as the notification's has.
  const goFrom = (search) => /(?:^\?|&)go=1(?:&|$)/.test(String(search == null ? "" : search));

  // The request's id in the page's address, as Nova serves the page: /approve/ and 32
  // lower-case hex digits, and nothing more - else null.
  const idFromPath = (path) => {
    const found = /^\/approve\/([0-9a-f]{32})$/.exec(String(path == null ? "" : path));
    return found ? found[1] : null;
  };

  // Sends Nova an answer, with the header which says it comes from the page itself.
  const post = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Nova": "page" },
      body: JSON.stringify(body),
    });
    return response.json();
  };

  // The page, once it's in the browser: what's shown and what the buttons do.
  const start = async () => {
    const description = document.getElementById("description");
    const buttons = document.getElementById("buttons");
    const approve = document.getElementById("approve");
    const refuse = document.getElementById("refuse");
    const said = document.getElementById("said");
    // The settings page's sky, drawn again as the phone turns.
    if (globalThis.NovaSky) {
      const sky = document.getElementById("sky");
      NovaSky.draw(sky);
      window.addEventListener("resize", () => NovaSky.draw(sky));
    }

    // Shows only what's said - for a request which can no longer be answered here.
    const only = (text) => {
      description.hidden = true;
      buttons.hidden = true;
      said.textContent = text;
      said.hidden = false;
    };
    // Says something under the request, keeping it in view, with the buttons or without them.
    const say = (text, keepButtons) => {
      buttons.hidden = !keepButtons;
      approve.disabled = refuse.disabled = false;
      said.textContent = text;
      said.hidden = !text;
    };
    // What Nova said of an answer. Once it's settled - here, or elsewhere first - the buttons go.
    const answered = (result) => {
      const message = (result && result.message) || "That didn't work.";
      if (message === SAID.answered || message === SAID.expired || message === NO_SUCH) only(message);
      else say(message, !(result && result.success));
    };

    description.hidden = true;
    buttons.hidden = true;
    said.hidden = true;
    const id = idFromPath(location.pathname);
    if (!id) {
      only(NO_SUCH);
      return;
    }
    let request;
    try {
      const response = await fetch(`/api/approvals/request?id=${id}`);
      request = await response.json();
    } catch (error) {
      only("Couldn't reach Nova. Open the notification again in a moment.");
      return;
    }
    if (!request || request.state !== "open") {
      only(SAID[request && request.state] || NO_SUCH);
      return;
    }
    // Open, but missing what it needs to be shown or answered: said plainly, rather than an empty
    // request with buttons which can't work.
    if (typeof request.description !== "string" || !request.description
        || typeof request.challenge !== "string" || !request.challenge) {
      only("Nova didn't send this request properly. Open the notification again.");
      return;
    }
    // As text: a description names his own events and albums, which could hold anything.
    description.textContent = request.description;
    description.hidden = false;
    buttons.hidden = false;

    // Asks the phone for his passkey and sends Nova what it signed. By itself (`automatic`, as
    // the page opens) a browser may refuse without a tap of his, so a failure then asks for one.
    const approveNow = async (automatic) => {
      approve.disabled = refuse.disabled = true;
      said.hidden = true;
      let credential;
      try {
        const publicKey = {
          challenge: b64urlToBytes(request.challenge),
          allowCredentials: (request.credentials || []).map((each) => ({ type: "public-key", id: b64urlToBytes(each) })),
          userVerification: "required",
          timeout: PROMPT_WAIT,
        };
        // The passkeys' relying party, whenever Nova gives it - left out, the browser would take
        // the page's own host, which is the same unless the page was reached another way.
        if (typeof request.rp_id === "string" && request.rp_id) publicKey.rpId = request.rp_id;
        credential = await navigator.credentials.get({ publicKey });
        if (!credential) throw new Error("no passkey");
      } catch (error) {
        // Cancelled, or timed out, or refused without a tap: the buttons stay, to try again or to refuse.
        say(automatic ? "Tap Approve to continue." : "Not approved yet. Try again, or Refuse.", true);
        return;
      }
      try {
        answered(await post("/api/approvals/answer", {
          id,
          credential_id: bytesToB64url(credential.rawId),
          client_data: bytesToB64url(credential.response.clientDataJSON),
          authenticator_data: bytesToB64url(credential.response.authenticatorData),
          signature: bytesToB64url(credential.response.signature),
        }));
      } catch (error) {
        say("Couldn't reach Nova, so it isn't approved yet. Try again.", true);
      }
    };
    approve.addEventListener("click", () => approveNow(false));

    refuse.addEventListener("click", async () => {
      approve.disabled = refuse.disabled = true;
      try {
        answered(await post("/api/approvals/refuse", { id }));
      } catch (error) {
        say("Couldn't reach Nova, so it isn't refused yet. Try again.", true);
      }
    });

    // Opened from the notification: Approve, once, with no second tap. Refuse stays a button.
    if (goFrom(location.search)) await approveNow(true);
  };

  // Only in a browser's page - the tests also run this with no page at all.
  const ready = typeof document === "undefined" || !document ? Promise.resolve() : start();
  globalThis.NovaApprove = { b64urlToBytes, bytesToB64url, idFromPath, goFrom, ready };
})();
