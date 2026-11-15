// The Wrapped's story page - see voice_assistant/wrapped.py, which makes and keeps each one,
// and settings_page.py, which serves it.
//
// The Wrapped asked for in the address (/wrapped?key=2026-11), or the newest, is asked for
// once from /api/wrapped, and made into cards shown one at a time: a tap on the right half
// or a swipe left goes on, a tap on the left half or a swipe right goes back, and so do the
// arrow keys. A card whose part of the Wrapped is missing is left out. Everything from the
// Wrapped is put on the page as text, never as HTML, and styles only ever set through the
// CSSOM, as the page's Content-Security-Policy allows nothing inline.
//
// Through NPM, /api/wrapped needs the settings page's login, which this page hasn't a form
// for: refused, it says so and links to the settings page to log in there.

"use strict";

// Where the Wrapped and its pictures are asked for.
const WRAPPED = "/api/wrapped";
const PICTURE = "/api/wrapped/picture";

// How far a finger must move across, in pixels, for a swipe rather than a tap.
const SWIPE = 40;

// How long after a swipe the click it can end in is ignored, in milliseconds - lifted over
// one of the halves, it would otherwise move a second card.
const AFTER_SWIPE = 500;

// How long the headline numbers take to count up, in milliseconds.
const COUNT_UP = 1100;

// How many colours the cards take in turn - the tone-N classes in wrapped.css.
const TONES = 7;

// From how many minutes they're said in hours too: "12,400 minutes, about 207 hours".
const HOURS_FROM = 120;

// Said in the user's language, rather than as the browser's would be.
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
  "September", "October", "November", "December"];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// The cards, the one shown, and until when a click is a swipe's.
let cards = [];
let shown = 0;
let ignoreClicksUntil = 0;

// Helpers

function element(tag, text, className) {
  const made = document.createElement(tag);
  if (text !== undefined && text !== null) made.textContent = String(text);
  if (className) made.className = className;
  return made;
}

function isCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function formatted(number) {
  return Math.round(number).toLocaleString("en-GB");
}

function counted(number, what) {
  return `${formatted(number)} ${what}${Math.round(number) === 1 ? "" : "s"}`;
}

// Names, with the entries without one left out.
function named(entries) {
  return Array.isArray(entries)
    ? entries.filter((entry) => entry && typeof entry.name === "string" && entry.name)
    : [];
}

// A day the Wrapped has as "2026-11-13", as a Date at local midnight - or null.
function day(text) {
  const found = typeof text === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  return found ? new Date(Number(found[1]), Number(found[2]) - 1, Number(found[3])) : null;
}

// "1 November to 30 November 2026", the year once if it's the same.
function periodSaid(period) {
  const start = day(period && period.start);
  const end = day(period && period.end);
  if (!start || !end) return "";
  const first = `${start.getDate()} ${MONTHS[start.getMonth()]}`
    + (start.getFullYear() !== end.getFullYear() ? ` ${start.getFullYear()}` : "");
  return `${first} to ${end.getDate()} ${MONTHS[end.getMonth()]} ${end.getFullYear()}`;
}

// A Wrapped's key as it's said: "2026", or "November 2026".
function keySaid(key) {
  const month = key.length > 4 ? Number(key.slice(5)) : 0;
  return month >= 1 && month <= 12 ? `${MONTHS[month - 1]} ${key.slice(0, 4)}` : key;
}

// An hour of the day as a person says it: "midnight", "9am", "midday", "6pm".
function hourSaid(hour) {
  if (hour === 0) return "midnight";
  if (hour === 12) return "midday";
  return `${hour % 12}${hour < 12 ? "am" : "pm"}`;
}

function pictureAddress(key, number) {
  return `${PICTURE}?key=${encodeURIComponent(key)}&n=${encodeURIComponent(number)}`;
}

// An artist's picture, or the first letter of their name where there's none or it won't load.
function picture(key, entry, className) {
  const initial = element("div", (entry.name.trim()[0] || "?").toUpperCase(), `${className} initial`);
  initial.setAttribute("aria-hidden", "true");
  if (!Number.isInteger(entry.picture) || entry.picture < 0) return initial;
  const image = element("img", null, className);
  image.alt = "";
  image.decoding = "async";
  image.src = pictureAddress(key, entry.picture);
  image.addEventListener("error", () => image.replaceWith(initial), { once: true });
  return image;
}

// Making the cards

// A card, its parts rising in the order given. A part may be left out as null.
function card(parts, className = "") {
  const made = element("section", null, `card ${className}`.trim());
  let order = 0;
  for (const part of parts) {
    if (!part) continue;
    part.classList.add("rise");
    part.style.setProperty("--order", String(order++));
    made.append(part);
  }
  made.countUps = [...made.querySelectorAll("[data-count]")];
  return made;
}

// A headline number, which counts up to itself as its card is shown.
function bigNumber(number) {
  const made = element("p", formatted(number), "big");
  made.dataset.count = String(Math.round(number));
  return made;
}

function line(text, className = "line") {
  return element("p", text, className);
}

// A line with one part in the card's colour: "That's about ", "207 hours", ".".
function lineWith(before, strong, after = "") {
  const made = element("p", before, "line");
  made.append(element("strong", strong), after);
  return made;
}

// A ranked list: each entry's name, what's under it, and its picture if it has one.
function ranks(entries, startAt, under, key, withPictures) {
  const list = element("ol", null, "ranks");
  entries.forEach((entry, at) => {
    const item = element("li");
    item.append(element("span", startAt + at, "rank"));
    if (withPictures) item.append(picture(key, entry, "thumb"));
    const who = element("div", null, "who");
    who.append(element("b", entry.name), element("span", under(entry)));
    item.append(who);
    list.append(item);
  });
  return list;
}

function playsOf(entry) {
  return isCount(entry.plays) ? counted(entry.plays, "play") : "";
}

function byAndPlays(entry) {
  const by = typeof entry.artist === "string" && entry.artist ? entry.artist : "";
  return [by, playsOf(entry)].filter(Boolean).join(" · ");
}

function intro(wrapped) {
  const year = wrapped.kind === "year";
  const month = !year && wrapped.key.length > 4 ? MONTHS[Number(wrapped.key.slice(5)) - 1] : "";
  return card([
    line("Nova Wrapped", "kicker"),
    element("h1", year ? `Your ${wrapped.key} Wrapped` : `Your ${month || keySaid(wrapped.key)}`, "title"),
    periodSaid(wrapped.period) ? line(periodSaid(wrapped.period), "line quiet") : null,
    line("Tap or swipe to start", "hint"),
  ], "intro");
}

function minutes(wrapped) {
  if (!isCount(wrapped.minutes) || wrapped.minutes <= 0) return null;
  const across = [isCount(wrapped.plays) ? counted(wrapped.plays, "play") : "",
    isCount(wrapped.active_days) ? `on ${counted(wrapped.active_days, "day")}` : ""].filter(Boolean).join(" ");
  return card([
    line("You listened for", "kicker"),
    bigNumber(wrapped.minutes),
    line(Math.round(wrapped.minutes) === 1 ? "minute" : "minutes", "name"),
    wrapped.minutes >= HOURS_FROM ? lineWith("That's about ", counted(wrapped.minutes / 60, "hour"), ".") : null,
    across ? line(`Across ${across}.`, "line quiet") : null,
  ]);
}

function topArtist(wrapped) {
  const top = named(wrapped.top_artists)[0];
  if (!top) return null;
  const halo = element("div", null, "halo");
  halo.append(picture(wrapped.key, top, "portrait"));
  return card([
    halo,
    line("Your top artist", "kicker"),
    element("h2", top.name, "name"),
    isCount(top.plays) ? lineWith("Your number one, with ", counted(top.plays, "play"), ".") : null,
  ]);
}

function topFive(wrapped) {
  const top = named(wrapped.top_artists).slice(0, 5);
  if (top.length < 2) return null;
  return card([
    line("Your top artists", "kicker"),
    element("h2", "On repeat", "title"),
    ranks(top, 1, playsOf, wrapped.key, true),
  ]);
}

// The top song or album, big, with those after it small.
function topOf(entries, kicker) {
  const top = named(entries).slice(0, 5);
  if (!top.length) return null;
  const artist = typeof top[0].artist === "string" && top[0].artist ? top[0].artist : "";
  const after = top.slice(1);
  let rest = null;
  if (after.length) {
    rest = ranks(after, 2, byAndPlays, null, false);
    rest.classList.add("runners-up");
  }
  return card([
    line(kicker, "kicker"),
    element("h2", top[0].name, "name"),
    artist ? line(`by ${artist}`) : null,
    isCount(top[0].plays) ? lineWith("Played ", counted(top[0].plays, "time"), ".") : null,
    rest,
  ]);
}

function genre(wrapped) {
  const top = wrapped.top_genre;
  if (!top || typeof top.name !== "string" || !top.name || !isCount(top.percentage)) return null;
  const share = element("div", null, "share");
  share.setAttribute("aria-hidden", "true");
  share.style.setProperty("--share", String(Math.min(100, top.percentage)));
  return card([
    share,
    line("Your top genre", "kicker"),
    element("h2", top.name, "name"),
    lineWith("", `${Math.round(top.percentage)}%`, " of everything you played."),
  ]);
}

function when(wrapped) {
  const hour = wrapped.peak_hour;
  if (!Number.isInteger(hour) || hour < 0 || hour > 23 || typeof wrapped.time_of_day !== "string") return null;
  // "a night owl" as a headline: "Night owl".
  const sort = wrapped.time_of_day.replace(/^(an?|the) /, "");
  const dial = element("div", null, "dial");
  dial.setAttribute("aria-hidden", "true");
  const hand = element("div", null, "hand");
  hand.style.setProperty("--turn", `${hour * 15}deg`);
  dial.append(element("span", "midnight", "mark top"), element("span", "midday", "mark bottom"),
    hand, element("span", hourSaid(hour), "hour"));
  return card([
    dial,
    line("When you listen", "kicker"),
    element("h2", sort.charAt(0).toUpperCase() + sort.slice(1), "name"),
    lineWith("You listen most around ", hourSaid(hour), "."),
  ]);
}

function streakAndBusiest(wrapped) {
  const streak = isCount(wrapped.longest_streak) && wrapped.longest_streak > 0 ? wrapped.longest_streak : null;
  const busiest = wrapped.busiest_day || {};
  const date = day(busiest.date);
  if (streak === null && !date) return null;
  const parts = [];
  if (streak !== null) {
    parts.push(line("Your longest streak", "kicker"), bigNumber(streak),
      line(`${streak === 1 ? "day" : "days"} in a row`, "line"));
  }
  if (streak !== null && date) parts.push(element("div", null, "split"));
  if (date) {
    parts.push(line("Your busiest day", "kicker"),
      element("h2", `${DAYS[date.getDay()]} ${date.getDate()} ${MONTHS[date.getMonth()]}`, "name"),
      isCount(busiest.plays) ? lineWith("", counted(busiest.plays, "play"), " in one day.") : null);
  }
  return card(parts);
}

function months(wrapped) {
  const listed = Array.isArray(wrapped.months)
    ? wrapped.months.filter((month) => month && typeof month.label === "string" && month.label)
    : [];
  if (!listed.length) return null;
  const most = Math.max(1, ...listed.map((month) => (isCount(month.minutes) ? month.minutes : 0)));
  const list = element("ol", null, "months");
  listed.forEach((month, at) => {
    const item = element("li");
    const artist = typeof month.top_artist === "string" && month.top_artist ? month.top_artist : "";
    const minutesSaid = isCount(month.minutes) ? counted(month.minutes, "minute") : "";
    item.append(element("span", month.label, "label"),
      element("span", [artist, minutesSaid].filter(Boolean).join(" · ") || "-", "artist"));
    const meter = element("span", null, "meter");
    meter.style.width = `${Math.max(2, (isCount(month.minutes) ? month.minutes : 0) / most * 100)}%`;
    meter.style.animationDelay = `${300 + at * 60}ms`;
    item.append(meter);
    list.append(item);
  });
  return card([
    line("Month by month", "kicker"),
    element("h2", "Your year, as it went", "title"),
    list,
  ]);
}

function discoveries(wrapped) {
  const found = named(wrapped.discoveries).slice(0, 6);
  if (!found.length) return null;
  const list = element("ol", null, "found");
  for (const artist of found) {
    const item = element("li");
    item.append(picture(wrapped.key, artist, "thumb"), element("b", artist.name));
    if (isCount(artist.plays)) item.append(element("span", counted(artist.plays, "play")));
    list.append(item);
  }
  return card([
    line("New to you", "kicker"),
    element("h2", "Artists you found this year", "title"),
    list,
  ]);
}

function numbers(wrapped, keys) {
  const grid = element("div", null, "numbers");
  for (const [value, what] of [[wrapped.minutes, "minutes"], [wrapped.plays, "plays"], [wrapped.artists, "artists"],
    [wrapped.albums, "albums"], [wrapped.tracks, "songs"], [wrapped.active_days, "days listening"]]) {
    if (!isCount(value)) continue;
    const stat = element("div");
    stat.append(element("b", formatted(value)), element("span", what));
    grid.append(stat);
  }
  const actions = element("div", null, "actions");
  const again = element("button", "Watch again", "again");
  again.type = "button";
  again.addEventListener("click", () => show(0));
  actions.append(again);
  const others = keys.filter((key) => key !== wrapped.key);
  if (others.length) {
    const menu = element("details", null, "others");
    menu.append(element("summary", "Your other Wrappeds"));
    const list = element("ul");
    for (const key of others) {
      const item = element("li");
      const link = element("a", keySaid(key));
      link.href = `/wrapped?key=${encodeURIComponent(key)}`;
      item.append(link);
      list.append(item);
    }
    menu.append(list);
    actions.append(menu);
  }
  const what = wrapped.kind === "year" ? wrapped.key : keySaid(wrapped.key).split(" ")[0];
  return card([
    line("That's a wrap", "kicker"),
    element("h2", `Your ${what} in numbers`, "title"),
    grid.childElementCount ? grid : null,
    actions,
  ]);
}

// Showing them

// Counts a headline number up from nothing, unless motion's been turned down.
function countUp(target) {
  const to = Number(target.dataset.count);
  if (reducedMotion.matches || !(to > 0)) {
    target.textContent = formatted(to);
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const done = Math.min(1, (now - started) / COUNT_UP);
    // Quick at first, settling on the number.
    target.textContent = formatted(to * (1 - Math.pow(1 - done, 3)));
    if (done < 1 && target.closest(".card.current")) requestAnimationFrame(step);
    else target.textContent = formatted(to);
  };
  target.textContent = "0";
  requestAnimationFrame(step);
}

function show(number) {
  shown = Math.max(0, Math.min(cards.length - 1, number));
  cards.forEach((each, at) => {
    each.classList.toggle("current", at === shown);
    each.setAttribute("aria-hidden", at === shown ? "false" : "true");
    // So a card off screen can't be tabbed into.
    each.inert = at !== shown;
  });
  [...document.getElementById("bars").children].forEach((bar, at) => {
    bar.classList.toggle("done", at < shown);
    bar.classList.toggle("current", at === shown);
  });
  cards[shown].countUps.forEach(countUp);
}

function go(by) {
  show(shown + by);
}

function listen() {
  const story = document.getElementById("story");
  for (const [id, by] of [["back", -1], ["next", 1]]) {
    document.getElementById(id).addEventListener("click", () => {
      if (Date.now() >= ignoreClicksUntil) go(by);
    });
  }
  let down = null;
  story.addEventListener("pointerdown", (event) => {
    down = event.isPrimary ? { x: event.clientX, y: event.clientY } : null;
  });
  story.addEventListener("pointercancel", () => { down = null; });
  story.addEventListener("pointerup", (event) => {
    if (!down || !event.isPrimary) return;
    const across = event.clientX - down.x;
    const upAndDown = event.clientY - down.y;
    down = null;
    if (Math.abs(across) >= SWIPE && Math.abs(across) > Math.abs(upAndDown)) {
      ignoreClicksUntil = Date.now() + AFTER_SWIPE;
      go(across < 0 ? 1 : -1);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const by = { ArrowRight: 1, ArrowLeft: -1, PageDown: 1, PageUp: -1 }[event.key];
    if (by) {
      event.preventDefault();
      go(by);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      show(event.key === "Home" ? 0 : cards.length - 1);
    }
  });
}

function build(wrapped, keys) {
  cards = [intro(wrapped), minutes(wrapped), topArtist(wrapped), topFive(wrapped),
    topOf(wrapped.top_tracks, "Your top song"), topOf(wrapped.top_albums, "Your top album"), genre(wrapped),
    when(wrapped), streakAndBusiest(wrapped), months(wrapped), discoveries(wrapped), numbers(wrapped, keys)]
    .filter(Boolean);
  const holder = document.getElementById("cards");
  const bars = document.getElementById("bars");
  cards.forEach((each, at) => {
    each.classList.add(`tone-${at % TONES}`);
    holder.append(each);
    const bar = element("div", null, "bar");
    bar.append(element("span"));
    bars.append(bar);
  });
  document.title = `${keySaid(wrapped.key)} Wrapped`;
  document.getElementById("message").hidden = true;
  document.getElementById("story").hidden = false;
  listen();
  show(0);
}

// Says why there's no story, with a link onwards if there's somewhere to go.
function say(text, link, linkText) {
  document.getElementById("said").textContent = text;
  const onwards = document.getElementById("message-link");
  if (link) {
    onwards.href = link;
    onwards.textContent = linkText;
    onwards.hidden = false;
  }
}

async function start() {
  const key = new URLSearchParams(window.location.search).get("key");
  let response;
  try {
    response = await fetch(key ? `${WRAPPED}?key=${encodeURIComponent(key)}` : WRAPPED,
      { cache: "no-store", credentials: "same-origin" });
  } catch {
    say("Nova can't be reached just now. Try again in a moment.");
    return;
  }
  // 401 through NPM without a login, 403 on the tailnet for a login not let in.
  if (response.status === 401 || response.status === 403) {
    say("Log in to Nova's page first, then open your Wrapped again.", "/", "Log in");
    return;
  }
  if (response.status === 404) {
    say("There's no Wrapped by that name.", "/wrapped", "See your newest");
    return;
  }
  let found = null;
  try {
    found = response.ok ? await response.json() : null;
  } catch {
    found = null;
  }
  if (!found) {
    say("Nova couldn't open your Wrapped just now. Try again in a moment.");
  } else if (!found.wrapped || typeof found.wrapped.key !== "string") {
    say("Your first Wrapped is made on the 1st of next month.");
  } else {
    build(found.wrapped, Array.isArray(found.keys) ? found.keys : []);
  }
}

start();
