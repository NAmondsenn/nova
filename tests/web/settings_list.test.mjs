// The settings page's list - which section a setting belongs to, what each row's line
// says, what needs attention and what a search finds - see voice_assistant/web/settings_list.js.
//
// The script never touches the page, so it is run in an empty context and NovaList read
// off it. What it gives is copied out through JSON before it is compared, as arrays made
// in another context aren't equal to this one's, and only small strings and numbers are
// compared, never whole objects.

// Set before anything makes a Date: a zone half an hour off UTC, so a line which said the
// time in UTC, or without its minutes right, would show.
process.env.TZ = "Asia/Kolkata";

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SCRIPT = readFileSync(new URL("../../voice_assistant/web/settings_list.js", import.meta.url), "utf8");

const context = vm.createContext({});
vm.runInContext(SCRIPT, context, { filename: "settings_list.js" });
const List = context.NovaList;

const plain = (value) => JSON.parse(JSON.stringify(value));

// A moment well away from any change of clock, in epoch seconds: 19:38 in Kolkata, 14:08 in UTC.
const NOW = 1795010880;

// A view where everything is well, with whatever a test changes laid over it.
const view = (changes = {}) => ({
  loaded: true,
  tiles: [{ name: "Claude", good: true, words: "Claude can't be reached" }],
  problems: [],
  restart: false,
  settingsProblem: "",
  music: { title: "Song", artist: "Artist", playing: true },
  clock: { timers: 0, reminders: 1, alarms: 1 },
  lights: { on: 2, total: 5 },
  batteries: [{ name: "Tablet", percent: 14, low: true }],
  displays: { total: 1 },
  journeys: { requests: 21, cap: 3000 },
  football: { teams: 2 },
  google: "in",
  spotify: "off",
  routines: 2,
  satellites: [{ name: "bedroom", connected: true, isDefault: true }],
  voice: 60,
  webSearch: true,
  conversation: { on: true, talkOver: "voice" },
  quiet: { from: "23:00", until: "07:00" },
  memories: 12,
  keys: { set: 6, total: 11 },
  approvals: { passkeys: 1, approvers: ["the PC"] },
  ...changes,
});

// What a row's line says, and whether it is marked as a warning.
const line = (id, changes) => plain(List.lines(view(changes), NOW)[id]);
const said = (id, changes) => line(id, changes).text;

test("the groups and sections are in the contract's order", () => {
  assert.equal(List.GROUPS.map((group) => `${group.id}:${group.name}`).join(" "), "now:Now home:Home daily:Daily nova:Nova");
  assert.equal(
    List.SECTIONS.map((section) => section.id).join(" "),
    "status music clock devices batteries displays journeys football calendars routines satellites approvals sound claude conversation notifications memory keys else",
  );
  const section = (id) => List.SECTIONS.find((one) => one.id === id);
  assert.equal(section("status").name, "Status");
  assert.equal(section("clock").name, "Timers and alarms");
  assert.equal(section("journeys").icon, "i-car");
  assert.equal(section("journeys").group, "daily");
  assert.equal(section("satellites").group, "nova");
  assert.equal(section("else").name, "Everything else");
  assert.equal(section("else").icon, "i-settings");
  // Conversation is in Claude's group, straight after it.
  assert.equal(section("conversation").name, "Conversation");
  assert.equal(section("conversation").group, section("claude").group);
});

test("every key start in the table belongs to its section and sub-heading", () => {
  const expected = [
    ["music.volume", "music", "Music"],
    ["navidrome.url", "music", "Music"],
    ["soulsync.url", "music", "Music"],
    ["spotify.device", "music", "Music"],
    ["clean_check.enabled", "music", "Music"],
    ["new_music.enabled", "music", "Music"],
    ["wrapped.day", "music", "Wrapped"],
    ["display.brightness", "displays", null],
    ["journeys.cap", "journeys", null],
    ["football.teams", "football", null],
    ["voice.speed", "sound", "Voice"],
    ["sounds.chime", "sound", "Sounds"],
    ["thresholds.wake", "sound", "Listening"],
    ["web_search.enabled", "claude", null],
    ["conversation.history_length", "claude", null],
    ["conversation.history_max_age", "claude", null],
    ["conversation.enabled", "conversation", null],
    ["conversation.pause_seconds", "conversation", null],
    ["conversation.longest_turn", "conversation", null],
    ["conversation.idle_minutes", "conversation", null],
    ["conversation.talk_over", "conversation", null],
    ["conversation.dip_level", "conversation", null],
    ["conversation.bigger_model", "conversation", null],
    ["conversation.switch_itself", "conversation", null],
    ["conversation.history_exchanges", "conversation", null],
    ["usage.limit", "claude", null],
    ["ntfy.topic", "notifications", null],
    ["home_assistant.url", "else", "Home"],
    ["mqtt.host", "else", "Home"],
    ["logging.keep", "else", "Privacy"],
  ];
  for (const [key, section, subheading] of expected) {
    assert.equal(List.sectionOf(key), section, key);
    assert.equal(List.subheadingOf(key), subheading, key);
  }
});

test("every sub-heading a key can be under is in the order, and the order has no other", () => {
  const keys = List.SECTIONS.flatMap((section) => section.starts.map((start) => `${start}x`)).concat("made.up");
  const given = new Set(keys.map((key) => List.subheadingOf(key)).filter(Boolean));
  assert.equal([...given].sort().join(","), [...List.SUBHEADING_ORDER].sort().join(","));
  assert.equal(plain(List.SUBHEADING_ORDER).join(","), "Music,Wrapped,Voice,Sounds,Listening,Home,Privacy,Other");
});

test("a key nothing claims is in Everything else under Other", () => {
  assert.equal(List.sectionOf("made.up"), "else");
  assert.equal(List.subheadingOf("made.up"), "Other");
  // A start only counts whole: it ends at the dot.
  assert.equal(List.sectionOf("musical.thing"), "else");
});

test("the status line is the first tile which isn't good", () => {
  const tiles = [
    { name: "Nova", good: true, words: "Nova is down" },
    { name: "Claude", good: false, words: "Claude can't be reached" },
    { name: "Music", good: false, words: "Music is off" },
  ];
  assert.equal(said("status", { tiles }), "Claude can't be reached");
});

test("the status line names the newest problem of the last hour, in local time", () => {
  const problems = [{ at: NOW - 60, level: "warning" }, { at: NOW - 600, level: "problem" }, { at: NOW - 900, level: "problem" }];
  assert.equal(said("status", { problems }), `A problem at 19:28`);
  assert.equal(said("status", { problems }), "A problem at 19:28");
  assert.equal(said("status", { problems: [{ at: NOW - 3600, level: "problem" }] }), `A problem at 18:38`);
});

test("the status line is All well with no problem in the hour, or only warnings", () => {
  assert.equal(said("status"), "All well");
  assert.equal(said("status", { problems: [{ at: NOW - 3601, level: "problem" }] }), "All well");
  assert.equal(said("status", { problems: [{ at: NOW - 10, level: "warning" }] }), "All well");
});

test("a tile which isn't good comes before a problem", () => {
  const tiles = [{ name: "Claude", good: false, words: "Claude can't be reached" }];
  assert.equal(said("status", { tiles, problems: [{ at: NOW - 10, level: "problem" }] }), "Claude can't be reached");
});

test("the status line isn't All well when its tiles or problems couldn't be read", () => {
  assert.equal(said("status", { tiles: null }), "");
  assert.equal(said("status", { problems: null }), "");
  // Nothing wrong in the tiles which were read still shows before the rest is known.
  const tiles = [{ name: "Claude", good: false, words: "Claude can't be reached" }];
  assert.equal(said("status", { tiles, problems: null }), "Claude can't be reached");
  // Tiles read as none, with problems read as none, is well.
  assert.equal(said("status", { tiles: [] }), "All well");
});

test("a tile which isn't good but has no words is named", () => {
  assert.equal(said("status", { tiles: [{ name: "Claude", good: false }] }), "Claude isn't right");
  assert.equal(said("status", { tiles: [{ name: "Claude", good: false, words: "" }] }), "Claude isn't right");
  assert.equal(said("status", { tiles: [{ good: false }] }), "");
  const lines = List.lines(view({ tiles: [{ good: false }] }), NOW);
  assert.equal(typeof lines.status.text, "string");
});

test("the newest problem is the one named, wherever it is in the list", () => {
  const problems = [{ at: NOW - 900, level: "problem" }, { at: NOW - 300, level: "problem" }, { at: NOW - 600, level: "problem" }];
  assert.equal(said("status", { problems }), "A problem at 19:33");
  assert.deepEqual(attention({ problems }), ["status: A problem at 19:33"]);
});

test("a problem with no usable time is passed over", () => {
  const odd = [{ level: "problem" }, { at: "soon", level: "problem" }, { at: NaN, level: "problem" }, { at: null, level: "problem" }];
  assert.equal(said("status", { problems: odd }), "All well");
  assert.deepEqual(attention({ problems: odd }), []);
  assert.equal(said("status", { problems: [...odd, { at: NOW - 600, level: "problem" }] }), "A problem at 19:28");
  assert.deepEqual(attention({ problems: [...odd, { at: NOW - 600, level: "problem" }] }), ["status: A problem at 19:28"]);
});

test("the status line is empty until the state is loaded", () => {
  assert.equal(said("status", { loaded: false }), "");
});

test("the music line is the song and artist while playing", () => {
  assert.equal(said("music"), "Song, Artist");
  assert.equal(said("music", { music: { title: "Song", artist: "", playing: true } }), "Song");
  assert.equal(said("music", { music: { title: "Song", artist: null, playing: true } }), "Song");
  assert.equal(said("music", { music: { title: "Song", artist: "Artist", playing: false } }), "Nothing playing");
  assert.equal(said("music", { music: { title: "", artist: "Artist", playing: false } }), "Nothing playing");
  assert.equal(said("music", { music: null }), "");
});

test("the music line doesn't claim nothing is playing when something is, with no title", () => {
  assert.equal(said("music", { music: { title: "", artist: "Artist", playing: true } }), "Artist");
  assert.equal(said("music", { music: { title: null, artist: "Artist", playing: true } }), "Artist");
  assert.equal(said("music", { music: { title: "", artist: "", playing: true } }), "Playing");
  assert.equal(said("music", { music: { playing: true } }), "Playing");
});

test("the clock line counts what is set, with its singular and plural", () => {
  assert.equal(said("clock"), "1 reminder, 1 alarm");
  assert.equal(said("clock", { clock: { timers: 1, reminders: 0, alarms: 0 } }), "1 timer");
  assert.equal(said("clock", { clock: { timers: 2, reminders: 2, alarms: 2 } }), "2 timers, 2 reminders, 2 alarms");
  assert.equal(said("clock", { clock: { timers: 0, reminders: 0, alarms: 3 } }), "3 alarms");
  assert.equal(said("clock", { clock: { timers: 0, reminders: 0, alarms: 0 } }), "Nothing set");
  assert.equal(said("clock", { clock: null }), "");
});

test("the devices line counts the lights which are on", () => {
  assert.equal(said("devices"), "2 lights on");
  assert.equal(said("devices", { lights: { on: 1, total: 5 } }), "1 light on");
  assert.equal(said("devices", { lights: { on: 0, total: 5 } }), "All off");
  assert.equal(said("devices", { lights: { on: 0, total: 0 } }), "No lights");
  assert.equal(said("devices", { lights: null }), "");
});

test("the batteries line is the lowest, a warning when it is low", () => {
  const batteries = [{ name: "Phone", percent: 80, low: false }, { name: "Tablet", percent: 14, low: true }, { name: "Watch", percent: 50, low: false }];
  assert.deepEqual(line("batteries", { batteries }), { text: "Tablet at 14%", warn: true });
  const fine = [{ name: "Phone", percent: 80, low: false }, { name: "Watch", percent: 50, low: false }];
  assert.deepEqual(line("batteries", { batteries: fine }), { text: "Watch at 50%", warn: false });
  assert.equal(said("batteries", { batteries: [] }), "None to show");
  assert.equal(said("batteries", { batteries: null }), "");
});

test("a battery with no usable percent is passed over", () => {
  const odd = [{ name: "Phone", low: true }, { name: "Watch", percent: "full", low: true }, { name: "Pen", percent: null, low: true }];
  assert.equal(said("batteries", { batteries: odd }), "None to show");
  assert.deepEqual(attention({ batteries: odd }), []);
  const mixed = [...odd, { name: "Tablet", percent: 14, low: true }];
  assert.deepEqual(line("batteries", { batteries: mixed }), { text: "Tablet at 14%", warn: true });
  assert.deepEqual(attention({ batteries: mixed }), ["batteries: Tablet is at 14%"]);
});

test("the displays line says how many there are, and nothing of whether they are connected", () => {
  assert.equal(said("displays"), "1 clock display");
  assert.equal(said("displays", { displays: { total: 2 } }), "2 clock displays");
  assert.equal(said("displays", { displays: { total: 0 } }), "None added");
  assert.equal(said("displays", { displays: null }), "");
});

test("the journeys line puts commas in the cap whatever the machine's locale", () => {
  assert.equal(said("journeys"), "21 of 3,000 asks used this month");
  assert.equal(said("journeys", { journeys: { requests: 0, cap: 999 } }), "0 of 999 asks used this month");
  assert.equal(said("journeys", { journeys: { requests: 1234, cap: 1000000 } }), "1,234 of 1,000,000 asks used this month");
  assert.equal(said("journeys", { journeys: null }), "");
});

test("the football line counts the teams followed", () => {
  assert.equal(said("football", { football: { teams: 0 } }), "No teams followed");
  assert.equal(said("football", { football: { teams: 1 } }), "1 team followed");
  assert.equal(said("football"), "2 teams followed");
  assert.equal(said("football", { football: null }), "");
});

test("the calendars line says whether Google is signed in to", () => {
  assert.deepEqual(line("calendars", { google: "in" }), { text: "Signed in to Google", warn: false });
  assert.deepEqual(line("calendars", { google: "out" }), { text: "Sign in to Google", warn: true });
  assert.deepEqual(line("calendars", { google: "off" }), { text: "Google isn't set up", warn: false });
  assert.equal(said("calendars", { google: null }), "");
});

test("the routines line counts them", () => {
  assert.equal(said("routines", { routines: 0 }), "No routines");
  assert.equal(said("routines", { routines: 1 }), "1 routine");
  assert.equal(said("routines"), "2 routines");
  assert.equal(said("routines", { routines: null }), "");
});

test("the satellites line counts those connected", () => {
  const two = [{ name: "bedroom", connected: true, isDefault: true }, { name: "lounge", connected: true, isDefault: false }];
  assert.equal(said("satellites", { satellites: two }), "2 of 2 connected");
  assert.equal(said("satellites", { satellites: [{ ...two[0], connected: false }, two[1]] }), "1 of 2 connected");
  assert.equal(said("satellites", { satellites: [] }), "None added");
  assert.equal(said("satellites", { satellites: null }), "");
});

test("the sound line says the voice's level", () => {
  assert.equal(said("sound"), "Voice at 60%");
  assert.equal(said("sound", { voice: 0 }), "Voice at 0%");
  assert.equal(said("sound", { voice: null }), "");
});

test("the Claude line says whether web search is on", () => {
  assert.equal(said("claude", { webSearch: true }), "Web search on");
  assert.equal(said("claude", { webSearch: false }), "Web search off");
  assert.equal(said("claude", { webSearch: null }), "");
});

test("the Conversation line says whether conversations are on and what cuts in", () => {
  assert.equal(said("conversation"), "Conversations on, your voice cuts in");
  assert.equal(said("conversation", { conversation: { on: true, talkOver: "wake_word_or_press" } }),
    "Conversations on, the wake word or a press cuts in");
  assert.equal(said("conversation", { conversation: { on: true, talkOver: null } }), "Conversations on");
  assert.equal(said("conversation", { conversation: { on: false, talkOver: "voice" } }), "Conversations off");
  assert.equal(said("conversation", { conversation: null }), "");
});

test("the notifications line gives the quiet hours", () => {
  assert.equal(said("notifications"), "Quiet from 23:00 to 07:00");
  assert.equal(said("notifications", { quiet: false }), "No quiet hours");
  assert.equal(said("notifications", { quiet: null }), "");
});

test("the memory line counts what is remembered", () => {
  assert.equal(said("memory", { memories: 0 }), "Nothing remembered");
  assert.equal(said("memory", { memories: 1 }), "1 thing remembered");
  assert.equal(said("memory"), "12 things remembered");
  assert.equal(said("memory", { memories: null }), "");
});

test("the keys line counts those set", () => {
  assert.equal(said("keys"), "6 of 11 set");
  assert.equal(said("keys", { keys: null }), "");
});

test("the approvals section is listed after satellites", () => {
  const ids = List.SECTIONS.map((section) => section.id);
  assert.equal(ids.indexOf("approvals"), ids.indexOf("satellites") + 1);
  const section = List.SECTIONS.find((one) => one.id === "approvals");
  assert.equal(`${section.name} ${section.group} ${section.icon}`, "Approvals nova i-key");
});

test("approvals line counts passkeys and names who can approve", () => {
  assert.deepEqual(line("approvals", { approvals: { passkeys: 0, approvers: [] } }), { text: "No passkey yet", warn: true });
  assert.deepEqual(line("approvals", { approvals: { passkeys: 1, approvers: [] } }), { text: "1 passkey", warn: false });
  assert.equal(said("approvals", { approvals: { passkeys: 2, approvers: [] } }), "2 passkeys");
  assert.equal(said("approvals"), "1 passkey, the PC");
  assert.equal(said("approvals", { approvals: { passkeys: 3, approvers: ["the PC", "the Mac"] } }), "3 passkeys, the PC, the Mac");
  // The PC alone is a way to approve, so nothing's the matter.
  assert.deepEqual(line("approvals", { approvals: { passkeys: 0, approvers: ["the PC"] } }), { text: "No passkey, the PC", warn: false });
  assert.deepEqual(line("approvals", { approvals: { npm: true } }), { text: "Only at the page's Tailscale address", warn: false });
  assert.equal(said("approvals", { approvals: null }), "");
});

test("approvals settings keys belong to the approvals section", () => {
  for (const key of ["approvals.trust_minutes", "approvals.wait_minutes", "approvals.album_count"]) {
    assert.equal(List.sectionOf(key), "approvals", key);
    assert.equal(List.subheadingOf(key), null, key);
  }
});

test("Everything else has no line, and every section has one", () => {
  assert.equal(said("else"), "");
  const lines = List.lines(view(), NOW);
  for (const section of List.SECTIONS) assert.equal(typeof lines[section.id].text, "string", section.id);
  // Nothing loaded at all gives every section an empty line, not a missing one.
  const empty = List.lines(null, NOW);
  for (const section of List.SECTIONS) assert.equal(empty[section.id].text, "", section.id);
});

// A view with nothing the matter, for the attention tests.
const calm = () => view({ batteries: [{ name: "Tablet", percent: 80, low: false }] });
const attention = (changes) => plain(List.attention({ ...calm(), ...changes }, NOW)).map((one) => `${one.section}: ${one.text}`);

test("nothing needs attention when nothing is the matter", () => {
  assert.deepEqual(attention({}), []);
});

test("a restart needed is Status's", () => {
  assert.deepEqual(attention({ restart: true }), ["status: Nova needs a restart to use what's been saved"]);
});

test("a settings problem is Everything else's, in a sentence of the page's own and never the problem's words", () => {
  // What Nova says of the file is a parser's message or a path, which the settings blocks say.
  assert.deepEqual(attention({ settingsProblem: "[Errno 13] Permission denied: '/made/up/config.yaml'" }),
                   ["else: Nova can't read its saved settings"]);
});

test("a default satellite which isn't connected is Satellites'", () => {
  const satellites = [{ name: "lounge", connected: false, isDefault: false }, { name: "bedroom", connected: false, isDefault: true }];
  assert.deepEqual(attention({ satellites }), ["satellites: The bedroom satellite isn't connected"]);
  assert.deepEqual(attention({ satellites: [{ name: "bedroom", connected: true, isDefault: true }] }), []);
});

test("each low battery is Batteries'", () => {
  const batteries = [{ name: "Tablet", percent: 14, low: true }, { name: "Phone", percent: 60, low: false }, { name: "Watch", percent: 9, low: true }];
  assert.deepEqual(attention({ batteries }), ["batteries: Tablet is at 14%", "batteries: Watch is at 9%"]);
});

test("a Google or Spotify which needs signing in to is Calendars' or Music's", () => {
  assert.deepEqual(attention({ google: "out" }), ["calendars: Sign in to Google"]);
  assert.deepEqual(attention({ spotify: "out" }), ["music: Sign in to Spotify"]);
  assert.deepEqual(attention({ google: "off", spotify: "in" }), []);
});

test("a problem in the last hour is Status's, and only a problem", () => {
  const recent = [{ at: NOW - 120, level: "warning" }, { at: NOW - 300, level: "problem" }];
  assert.deepEqual(attention({ problems: recent }), [`status: A problem at 19:33`]);
  assert.deepEqual(attention({ problems: [{ at: NOW - 7200, level: "problem" }] }), []);
  assert.deepEqual(attention({ problems: [{ at: NOW - 60, level: "warning" }] }), []);
});

test("what needs attention comes in the contract's order", () => {
  const all = {
    restart: true,
    settingsProblem: "Bad setting",
    satellites: [{ name: "bedroom", connected: false, isDefault: true }],
    batteries: [{ name: "Tablet", percent: 14, low: true }],
    google: "out",
    spotify: "out",
    problems: [{ at: NOW - 300, level: "problem" }],
  };
  assert.deepEqual(attention(all), [
    "status: Nova needs a restart to use what's been saved",
    "else: Nova can't read its saved settings",
    "satellites: The bedroom satellite isn't connected",
    "batteries: Tablet is at 14%",
    "calendars: Sign in to Google",
    "music: Sign in to Spotify",
    `status: A problem at 19:33`,
  ]);
});

test("nothing loaded needs no attention", () => {
  assert.deepEqual(plain(List.attention(null, NOW)), []);
});

test("words lower-cases and splits on white space", () => {
  assert.deepEqual(plain(List.words("  Quiet   HOURS ")), ["quiet", "hours"]);
  assert.deepEqual(plain(List.words("   ")), []);
  assert.deepEqual(plain(List.words("")), []);
});

test("words keeps at most 8 words of at most 40 characters", () => {
  assert.equal(List.words("a b c d e f g h i j").length, 8);
  assert.equal(List.words("x".repeat(500)).length, 1);
  assert.equal(List.words("x".repeat(500))[0].length, 40);
});

// What the page makes of its rows, for the search tests.
const entries = [
  { section: "status", kind: "name", text: "Status", target: "status" },
  { section: "notifications", kind: "name", text: "Notifications", target: "notifications" },
  { section: "notifications", kind: "line", text: "Quiet from 23:00 to 07:00", target: "notifications" },
  { section: "notifications", kind: "setting", text: "Quiet hours start", target: "ntfy.quiet_start" },
  { section: "notifications", kind: "setting", text: "Quiet hours end", target: "ntfy.quiet_end" },
  { section: "sound", kind: "name", text: "Sound and voice", target: "sound" },
  { section: "sound", kind: "line", text: "Voice at 60%", target: "sound" },
  { section: "sound", kind: "setting", text: "How loud Nova speaks", target: "voice.volume" },
  { section: "sound", kind: "control", text: "Say something", target: "say-button" },
];

const found = (query, from = entries) => plain(List.search(query, from)).map((one) => `${one.section}: ${one.hits.map((hit) => hit.target).join(",")}`);

test("a search finds a section by its line, with no hits", () => {
  assert.deepEqual(found("23:00"), ["notifications: "]);
});

test("a search finds a section by a setting's label, listing the setting", () => {
  assert.deepEqual(found("LOUD"), ["sound: voice.volume"]);
  const hit = plain(List.search("loud", entries))[0].hits[0];
  assert.equal(hit.label, "How loud Nova speaks");
  assert.equal(hit.target, "voice.volume");
});

test("a hit shows its entry's label where it has one, and is found by the text, which holds more", () => {
  const withKey = [{ section: "sound", kind: "setting", text: "How loud Nova speaks voice volume", label: "How loud Nova speaks",
                     target: "voice.volume" },
                   { section: "sound", kind: "control", text: "Say something", target: "say-button" }];
  const hit = plain(List.search("voice volume", withKey))[0].hits[0];
  assert.equal(hit.label, "How loud Nova speaks");
  assert.equal(plain(List.search("something", withKey))[0].hits[0].label, "Say something");
});

test("a search finds a section by a control", () => {
  assert.deepEqual(found("something"), ["sound: say-button"]);
});

test("a search finds a section by its name, with no hits", () => {
  assert.deepEqual(found("status"), ["status: "]);
});

test("every word has to be somewhere in the section, not all in one entry", () => {
  // "quiet" is in the line and the settings, "notifications" in the name only.
  assert.deepEqual(found("quiet notifications"), ["notifications: "]);
  assert.deepEqual(found("quiet start"), ["notifications: ntfy.quiet_start"]);
  assert.deepEqual(found("quiet loud"), []);
});

test("a search lists the sections in the contract's order, and hits in the order given", () => {
  assert.deepEqual(found("quiet voice"), []);
  const shuffled = [...entries].reverse();
  assert.deepEqual(found("hours", shuffled), ["notifications: ntfy.quiet_end,ntfy.quiet_start"]);
  assert.deepEqual(found("s", shuffled).map((one) => one.split(":")[0]), ["status", "sound", "notifications"]);
});

test("a search lists at most 6 hits", () => {
  const many = Array.from({ length: 9 }, (_, number) => ({ section: "sound", kind: "setting", text: `Level ${number}`, target: `t${number}` }));
  assert.equal(plain(List.search("level", many))[0].hits.length, 6);
});

test("a search for no words finds nothing", () => {
  assert.deepEqual(found(""), []);
  assert.deepEqual(found("    "), []);
});

test("a search is plain text, never a pattern, and a long one is no trouble", () => {
  assert.deepEqual(found(".*("), []);
  assert.deepEqual(found("quiet.*"), []);
  assert.deepEqual(found("q".repeat(500)), []);
  assert.deepEqual(found("x ".repeat(500)), []);
});

test("a section is found from its hash only when the hash is one", () => {
  assert.equal(List.fromHash("#music"), "music");
  assert.equal(List.fromHash("#else"), "else");
  assert.equal(List.fromHash("#nonsense"), null);
  assert.equal(List.fromHash(""), null);
  assert.equal(List.fromHash("music"), null);
  assert.equal(List.fromHash(undefined), null);
});
