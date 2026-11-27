// The rules of Nova's settings page's list - see voice_assistant/settings_page.py and
// voice_assistant/web/settings.js.
//
// The page is a short list of sections, each row with a line on how it is, opening one
// section at a time. This decides which section a setting belongs to, what each row's line
// says, what needs attention and what a search finds. It never touches the page: it is
// given plain data (the view settings.js builds from what Nova says, and the rows the page
// has) and gives plain data back, so it can be tested with no page at all. It sets one
// global, NovaList, and is loaded before settings.js.

"use strict";

(() => {
  // The groups the list is divided into, in order.
  const GROUPS = [
    { id: "now", name: "Now" },
    { id: "home", name: "Home" },
    { id: "daily", name: "Daily" },
    { id: "nova", name: "Nova" },
  ];

  // The sections, in the order they are listed. `starts` are the beginnings of the settings
  // keys a section holds; a key nothing here claims is in "else".
  const SECTIONS = [
    { id: "status", name: "Status", group: "now", icon: "i-pulse", starts: [] },
    { id: "music", name: "Music", group: "now", icon: "i-music", starts: ["music.", "navidrome.", "soulsync.", "spotify.", "clean_check.", "new_music.", "wrapped."] },
    { id: "clock", name: "Timers and alarms", group: "now", icon: "i-alarm", starts: [] },
    { id: "devices", name: "Devices", group: "home", icon: "i-bulb", starts: [] },
    { id: "batteries", name: "Batteries", group: "home", icon: "i-battery", starts: [] },
    { id: "displays", name: "Clock displays", group: "home", icon: "i-display", starts: ["display."] },
    { id: "journeys", name: "Journeys", group: "daily", icon: "i-car", starts: ["journeys."] },
    { id: "football", name: "Football", group: "daily", icon: "i-football", starts: ["football."] },
    { id: "calendars", name: "Calendars and lists", group: "daily", icon: "i-calendar", starts: [] },
    { id: "routines", name: "Routines", group: "daily", icon: "i-routine", starts: [] },
    { id: "satellites", name: "Satellites", group: "nova", icon: "i-microphone", starts: ["computer."] },
    { id: "approvals", name: "Approvals", group: "nova", icon: "i-key", starts: ["approvals."] },
    { id: "updates", name: "Updates", group: "nova", icon: "i-server", starts: ["updates."] },
    { id: "sound", name: "Sound and voice", group: "nova", icon: "i-sound", starts: ["voice.", "sounds.", "thresholds."] },
    // Claude's own two settings of a thread's length are named in full, as the rest of "conversation." is the
    // Conversation section's, which comes after.
    { id: "claude", name: "Claude", group: "nova", icon: "i-reply", starts: ["web_search.", "conversation.history_length", "conversation.history_max_age", "usage."] },
    { id: "conversation", name: "Conversation", group: "nova", icon: "i-voice", starts: ["conversation."] },
    { id: "notifications", name: "Notifications", group: "nova", icon: "i-bell", starts: ["ntfy."] },
    { id: "memory", name: "Memory", group: "nova", icon: "i-memory", starts: [] },
    { id: "keys", name: "Keys", group: "nova", icon: "i-key", starts: [] },
    { id: "else", name: "Everything else", group: "nova", icon: "i-settings", starts: ["home_assistant.", "mqtt.", "logging."] },
  ];

  // The heading a key goes under inside a section which holds more than one of the old
  // groups, by the start of the key. A start not here has no heading, except in Music,
  // whose other keys are under "Music".
  const SUBHEADINGS = {
    "wrapped.": "Wrapped",
    "voice.": "Voice",
    "sounds.": "Sounds",
    "thresholds.": "Listening",
    "home_assistant.": "Home",
    "mqtt.": "Home",
    "logging.": "Privacy",
  };

  // The sub-headings in the order a section shows them, which is the order the old groups were
  // in: the one place they are all named, with SUBHEADINGS and subheadingOf.
  const SUBHEADING_ORDER = ["Music", "Wrapped", "Voice", "Sounds", "Listening", "Home", "Privacy", "Other"];

  // Which section a settings key is in.
  const sectionOf = (key) => {
    const text = String(key);
    const section = SECTIONS.find((one) => one.starts.some((start) => text.startsWith(start)));
    return section ? section.id : "else";
  };

  // The heading a settings key goes under inside its section, or null for none.
  const subheadingOf = (key) => {
    const text = String(key);
    const start = Object.keys(SUBHEADINGS).find((one) => text.startsWith(one));
    if (start) return SUBHEADINGS[start];
    const section = sectionOf(text);
    if (section === "music") return "Music";
    return section === "else" ? "Other" : null;
  };

  // A whole number with thousands' commas. Done here rather than with toLocaleString, as
  // that gives different marks on a machine set to another language.
  const withCommas = (number) => String(Math.round(Number(number))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");

  // "1 timer", "2 timers".
  const count = (number, one, many) => `${number} ${number === 1 ? one : many}`;

  // A moment, in epoch seconds, as the time of day where the page is: HH:MM in 24 hours.
  const clockAt = (seconds) => {
    const date = new Date(seconds * 1000);
    return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  };

  // How long a problem is still news for, in seconds.
  const RECENT = 3600;

  // A number which can be shown: not missing, not text, not NaN.
  const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

  // The newest problem (not a warning) in the last hour, or null. Looked for by its time,
  // not its place in the list, so the order the page keeps them in doesn't matter. One with
  // no time to say is passed over, rather than shown as "NaN:NaN".
  const recentProblem = (view, now) => {
    let newest = null;
    for (const problem of view.problems || []) {
      if (problem.level !== "problem" || !isNumber(problem.at) || now - problem.at > RECENT) continue;
      if (!newest || problem.at > newest.at) newest = problem;
    }
    return newest;
  };

  // The machines whose update failed or waits on him, and what the list says of one.
  const needsYou = (machines) => machines.filter((machine) => machine.state === "failed" || machine.state === "needs_you");
  // `spoken` is how the page says the machine mid-sentence, "the PC", so it is capitalised to start one.
  const updateNeed = (machine) => {
    const spoken = String(machine.spoken || machine.name || "");
    return `${spoken.charAt(0).toUpperCase()}${spoken.slice(1)} needs you for its update`;
  };
  const DIDNT_START = "Nova's update didn't start";

  // Each row's line: what it says and whether it is marked as a warning. A part of the view
  // which is null (not loaded, or couldn't be read) gives an empty line.
  const LINES = {
    status: (view, now) => {
      if (!view.loaded) return { text: "" };
      const bad = (view.tiles || []).find((tile) => !tile.good);
      // A tile which has gone wrong with nothing to say is named, so the line is never blank.
      if (bad) return { text: bad.words || (bad.name ? `${bad.name} isn't right` : "") };
      // Not knowing the tiles or the problems isn't the same as nothing being wrong.
      if (view.tiles == null || view.problems == null) return { text: "" };
      const problem = recentProblem(view, now);
      return { text: problem ? `A problem at ${clockAt(problem.at)}` : "All well" };
    },
    music: (view) => {
      const music = view.music;
      if (!music) return { text: "" };
      if (!music.playing) return { text: "Nothing playing" };
      if (!music.title) return { text: music.artist || "Playing" };
      return { text: music.artist ? `${music.title}, ${music.artist}` : music.title };
    },
    clock: (view) => {
      const clock = view.clock;
      if (!clock) return { text: "" };
      const parts = [];
      if (clock.timers) parts.push(count(clock.timers, "timer", "timers"));
      if (clock.reminders) parts.push(count(clock.reminders, "reminder", "reminders"));
      if (clock.alarms) parts.push(count(clock.alarms, "alarm", "alarms"));
      return { text: parts.length ? parts.join(", ") : "Nothing set" };
    },
    devices: (view) => {
      const lights = view.lights;
      if (!lights) return { text: "" };
      if (!lights.total) return { text: "No lights" };
      return { text: lights.on ? `${count(lights.on, "light", "lights")} on` : "All off" };
    },
    batteries: (view) => {
      if (!view.batteries) return { text: "" };
      const batteries = view.batteries.filter((battery) => isNumber(battery.percent));
      if (!batteries.length) return { text: "None to show" };
      const lowest = batteries.reduce((least, battery) => (battery.percent < least.percent ? battery : least));
      return { text: `${lowest.name} at ${lowest.percent}%`, warn: Boolean(lowest.low) };
    },
    displays: (view) => {
      const displays = view.displays;
      if (!displays) return { text: "" };
      return { text: displays.total ? count(displays.total, "clock display", "clock displays") : "None added" };
    },
    journeys: (view) => {
      const journeys = view.journeys;
      if (!journeys) return { text: "" };
      return { text: `${withCommas(journeys.requests)} of ${withCommas(journeys.cap)} asks used this month` };
    },
    football: (view) => {
      const football = view.football;
      if (!football) return { text: "" };
      return { text: football.teams ? `${count(football.teams, "team", "teams")} followed` : "No teams followed" };
    },
    calendars: (view) => {
      if (view.google === "in") return { text: "Signed in to Google" };
      if (view.google === "out") return { text: "Sign in to Google", warn: true };
      return { text: view.google === "off" ? "Google isn't set up" : "" };
    },
    routines: (view) => {
      if (view.routines == null) return { text: "" };
      return { text: view.routines ? count(view.routines, "routine", "routines") : "No routines" };
    },
    satellites: (view) => {
      const satellites = view.satellites;
      if (!satellites) return { text: "" };
      if (!satellites.length) return { text: "None added" };
      return { text: `${satellites.filter((satellite) => satellite.connected).length} of ${satellites.length} connected` };
    },
    // His passkeys and the satellites which can approve, as "1 passkey, the PC" - a warning
    // while there's no way to approve at all, as every big action is then refused.
    approvals: (view) => {
      const approvals = view.approvals;
      if (!approvals) return { text: "" };
      if (approvals.npm) return { text: "Only at the page's Tailscale address" };
      const approvers = approvals.approvers || [];
      if (!approvals.passkeys && !approvers.length) return { text: "No passkey yet", warn: true };
      const passkeys = approvals.passkeys ? count(approvals.passkeys, "passkey", "passkeys") : "No passkey";
      return { text: [passkeys, ...approvers].join(", ") };
    },
    // How far each machine is from the newest code: what needs him first, else who is behind or
    // being updated.
    updates: (view) => {
      const machines = view.updates && view.updates.machines;
      if (!machines) return { text: "" };
      const needing = needsYou(machines);
      if (needing.length) return { text: updateNeed(needing[0]), warn: true };
      if (machines.some((machine) => machine.state === "busy")) return { text: DIDNT_START, warn: true };
      const behind = machines.filter((machine) => machine.state === "behind").length;
      const updating = machines.filter((machine) => machine.state === "updating").length;
      const old = machines.filter((machine) => machine.state === "old").length;
      const parts = [];
      if (behind) parts.push(`${behind} behind`);
      if (updating) parts.push(`${updating} updating`);
      if (old) parts.push(`${old} to update by hand`);
      return { text: parts.length ? parts.join(", ") : "All up to date" };
    },
    sound: (view) => ({ text: view.voice == null ? "" : `Voice at ${view.voice}%` }),
    claude: (view) => {
      if (view.webSearch == null) return { text: "" };
      return { text: view.webSearch ? "Web search on" : "Web search off" };
    },
    // Whether conversations can be started and, if they can, what cuts in on a reply.
    conversation: (view) => {
      const conversation = view.conversation;
      if (!conversation) return { text: "" };
      if (!conversation.on) return { text: "Conversations off" };
      if (conversation.talkOver === "voice_at_computers") return { text: "Conversations on, your voice cuts in at a computer with headphones" };
      if (conversation.talkOver === "voice") return { text: "Conversations on, your voice cuts in anywhere" };
      if (conversation.talkOver === "wake_word_or_press") return { text: "Conversations on, the wake word or a press cuts in" };
      return { text: "Conversations on" };
    },
    notifications: (view) => {
      const quiet = view.quiet;
      if (quiet == null) return { text: "" };
      return { text: quiet ? `Quiet from ${quiet.from} to ${quiet.until}` : "No quiet hours" };
    },
    memory: (view) => {
      if (view.memories == null) return { text: "" };
      return { text: view.memories ? `${count(view.memories, "thing", "things")} remembered` : "Nothing remembered" };
    },
    keys: (view) => {
      const keys = view.keys;
      return { text: keys ? `${keys.set} of ${keys.total} set` : "" };
    },
    // Everything else is a place to look in, not a state to show.
    else: () => ({ text: "" }),
  };

  // Every section's line, as { text, warn } with `warn` always there. `now` is in epoch
  // seconds. With no view at all every line is empty.
  const lines = (view, now) => {
    const result = {};
    for (const section of SECTIONS) {
      const line = view ? LINES[section.id](view, now) : { text: "" };
      result[section.id] = { text: line.text, warn: Boolean(line.warn) };
    }
    return result;
  };

  // What needs attention, as { section, text }, in the order of what matters most.
  const attention = (view, now) => {
    const found = [];
    if (!view) return found;
    if (view.restart) found.push({ section: "status", text: "Nova needs a restart to use what's been saved" });
    // In words of the page's own: what Nova says of the file is a parser's message or a path to it,
    // which the top of each section's settings says in full.
    if (view.settingsProblem) found.push({ section: "else", text: "Nova can't read its saved settings" });
    const away = (view.satellites || []).find((satellite) => satellite.isDefault && !satellite.connected);
    if (away) found.push({ section: "satellites", text: `The ${away.name} satellite isn't connected` });
    for (const battery of view.batteries || []) {
      if (battery.low && isNumber(battery.percent)) found.push({ section: "batteries", text: `${battery.name} is at ${battery.percent}%` });
    }
    if (view.google === "out") found.push({ section: "calendars", text: "Sign in to Google" });
    if (view.spotify === "out") found.push({ section: "music", text: "Sign in to Spotify" });
    for (const machine of needsYou((view.updates && view.updates.machines) || [])) {
      found.push({ section: "updates", text: updateNeed(machine) });
    }
    if (((view.updates && view.updates.machines) || []).some((machine) => machine.state === "busy")) {
      found.push({ section: "updates", text: DIDNT_START });
    }
    const problem = recentProblem(view, now);
    if (problem) found.push({ section: "status", text: `A problem at ${clockAt(problem.at)}` });
    return found;
  };

  // How much of a search is looked at: more words or longer ones add nothing a person
  // meant, and would only make the page do more work for it.
  const MOST_WORDS = 8;
  const LONGEST_WORD = 40;

  // The most hits listed under a section.
  const MOST_HITS = 6;

  // A search's words: lower case, split on white space, and kept to a sensible size.
  const words = (query) => String(query == null ? "" : query)
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, MOST_WORDS)
    .map((word) => word.slice(0, LONGEST_WORD));

  // The sections which a search finds, in the list's order. `entries` are the page's rows,
  // as { section, kind: "name" | "line" | "setting" | "control", text, target, label? }. A section
  // is found when every word is in the text of some entry of it, and lists as hits its
  // settings and controls which hold every word. Words are looked for as plain text, never
  // as a pattern.
  const search = (query, entries) => {
    const wanted = words(query);
    if (!wanted.length) return [];
    const lower = entries.map((entry) => ({ entry, text: String(entry.text).toLowerCase() }));
    const found = [];
    for (const section of SECTIONS) {
      const own = lower.filter((one) => one.entry.section === section.id);
      if (!wanted.every((word) => own.some((one) => one.text.includes(word)))) continue;
      // A hit shows its entry's `label` where it has one, as a setting's text also holds the words
      // of its key, which are there to be found and not to be read.
      const hits = own
        .filter((one) => (one.entry.kind === "setting" || one.entry.kind === "control") && wanted.every((word) => one.text.includes(word)))
        .slice(0, MOST_HITS)
        .map((one) => ({ label: one.entry.label == null ? one.entry.text : one.entry.label, target: one.entry.target }));
      found.push({ section: section.id, hits });
    }
    return found;
  };

  // The section a location's hash names, as in "#music", or null for any other. A hash
  // always starts with its "#", so a bare id isn't one.
  const fromHash = (hash) => {
    const text = String(hash == null ? "" : hash);
    const section = text.startsWith("#") && SECTIONS.find((one) => one.id === text.slice(1));
    return section ? section.id : null;
  };

  globalThis.NovaList = { GROUPS, SECTIONS, SUBHEADING_ORDER, sectionOf, subheadingOf, lines, attention, words, search, fromHash };
})();
