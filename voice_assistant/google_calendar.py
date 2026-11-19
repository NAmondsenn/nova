"""
Google Calendar Module.

Reads the user's calendars from Google, and says what's on them, with Nova's own
sign-in to Google. See docs/decisions/0008.

The calendar comes straight from Google, not through Home Assistant - the user's
answer 2 - so Nova signs in to Google itself (decision 10). It asks only for what it
uses: the list of calendars, and reading and adding events. The sign-in is kept on
the brain, readable by Nova's user only, never on a satellite. It's done on the
settings page, under Calendars and lists - see settings_page.py - in place of 0008's
command on the brain, so there's only the one way in to keep safe (0009's decision 8).

It's plain HTTPS with requests - an authorisation code with PKCE, from a "Desktop
app" client of the user's own - rather than a Google library, which would be one more
dependency for a few requests: the token, the list of calendars, reading events, and
writing one - adding, changing or removing it. Every write goes through _send_once, which
sends it once, never again on an unclear answer, and tells nobody - no guest is emailed.
Only an event on one of the user's own calendars is changed, never an invite from
someone else.

Every calendar in Google's own list of the user's is read, so one added later turns up
by itself, unless it's chosen out of what's on, on the settings page. With Google out of
reach there's no calendar - no copy is kept. Each but the user's own is an activity, by
its name - "when's gym tomorrow?" works as soon as there's a Gym calendar - unless the page
says otherwise for it. A calendar named by an email address - the user's own, as Google
names it, or a person's own shared with them - is never said by it, or told to Claude.
"""

import base64
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests
import urllib3

from voice_assistant import network, paths, storage
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import spoken_clock_time

logger = logging.getLogger(__name__)

# urllib3's debug lines have each address asked for - an events address has a calendar's
# id in it, and the user's own is their email address - so they're kept to warnings,
# whatever Nova's own level.
logging.getLogger("urllib3").setLevel(logging.WARNING)

# The least that list the calendars, read every calendar's events, and add to, change and
# remove the events on the user's own - events.owned reaches no other calendar's.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
]

# What each scope lets Nova do, as said when the user's sign-in left it out.
SCOPES_SAID = {
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly": "see the list of your calendars",
    "https://www.googleapis.com/auth/calendar.events.readonly": "read your calendars' events",
    "https://www.googleapis.com/auth/calendar.events.owned": "add events to your own calendars",
}

# The OAuth errors from Google's token address that only signing in again can put right:
# a sign-in revoked or run out, a client deleted, or with a secret made anew, or a client
# of the wrong type - which would otherwise be heard as Google being out of reach, for good.
SIGN_IN_ERRORS = ("invalid_grant", "invalid_client", "unauthorized_client")

AUTHORISE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
# Nothing listens here, so the page Google sends the browser to doesn't load, and the
# user pastes its address back - as with Spotify. The brain has no browser of its own.
REDIRECT = "http://127.0.0.1:8085/"

# How long a request to Google may take, in seconds.
REQUEST_TIMEOUT = 10

# An access token is refreshed this many seconds before Google says it runs out, so
# one that's nearly done isn't sent with a request which then arrives too late.
REFRESH_EARLY = 60

# How long a sign-in started on the page waits for its address, in seconds - long enough
# to sign in to Google, but not left to be taken days later.
SIGN_IN_LASTS = 10 * 60

# An OAuth error code as Google gives one, such as redirect_uri_mismatch - only that is
# said of a refusal, never the words Google answers with.
ERROR_CODE = re.compile(r"[a-z_]{1,40}")


def logged_error(error) -> str:
    """
    An OAuth error as it's logged, for both sign-ins: only a plain code such as
    access_denied, since a pasted address's error= can be anything - a new line in it
    would forge a log line.

    Args:
        error: The error, from the pasted address or the answer to it.

    Returns:
        The code, or "an unreadable error".
    """
    return error if isinstance(error, str) and ERROR_CODE.fullmatch(error) else "an unreadable error"

CALENDARS = "https://www.googleapis.com/calendar/v3/calendars"
CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# The most events Google gives on each page - its own most.
EVENTS_PAGE = 250

# The most pages read of one list - 20 of 250 events is 5000 for a single calendar, far more
# than the fortnight asked for has. Past that Google is giving the same next page over, and
# reading on would never end.
MOST_PAGES = 20

# What Google answers for a calendar it won't give the events of - deleted since its list
# was read, or no longer shared with the user. That one is left out, not the whole read.
CALENDAR_GONE = (403, 404)
# Except a 403 for one of these, which is Google slowing Nova down - a moment out of reach,
# not a calendar gone, or "nothing on" would be said for what wasn't read.
RATE_LIMITED = ("rateLimitExceeded", "userRateLimitExceeded")

# What Google answers for an event that's gone - deleted on the phone meanwhile, say: a 404
# for one it has forgotten, a 410 for one deleted. Said as that, and nothing more is sent.
EVENT_GONE = (404, 410)

# How far past a cut the times of a repeating event deleted on their own are looked for - two years.
# A series with no end ("every Tuesday") would otherwise be listed on and on, past MOST_PAGES, and a
# change from this one on refused. One deleted further ahead than that comes back when its series is put
# back, or is in a new series.
CANCELLED_LOOKED_FOR = timedelta(days=730)

# The most days named when some deleted on their own came back - "Tuesday, Wednesday, Thursday and 2 more".
MOST_DAYS_SAID = 3

# What's copied of an event removed to put it back, when Google won't set it back to
# confirmed: what the user sees of it. Not its id, its guests or the series it was in - a
# copy is an event of its own, and nobody is asked to it again.
COPIED = ("summary", "description", "location", "start", "end", "reminders", "colorId", "transparency",
          "visibility")

# An email address, which Google names the user's own calendar by, and a person's own shared
# with them. A bare "@" isn't one - "Gym @ 6" is a name, said as it is. Kept wide, as a missed
# address would be spoken and told to Claude: anything before the "@", then a domain whose
# last part, after a dot, starts with a letter ("me@example.com", "me@mail.co.uk", but not
# "Gym@6.30").
ADDRESS = re.compile(r"[^\s@]+@[^\s@]+\.[^\W\d_][^\s@]+")

# The ids of Google's own calendars - a country's holidays, the contacts' birthdays, week
# numbers - which are things on the calendar, not activities, unless switched on.
GOOGLES_OWN = "@group.v.calendar.google.com"

# The few English words for the same things as an activity's name - so "am I working on
# Saturday?" is the Work calendar, and "when's my next lecture?" the Uni one. They're
# words, not calendars: one called Uni gets uni's.
SAME_AS = {"work": ("working", "shift", "shifts"), "uni": ("university", "lecture", "lectures", "class", "classes")}

CANT_REACH_GOOGLE = "I can't reach Google's calendar right now."
NO_SUCH_CALENDAR = "I haven't got a calendar called that."
DAY_GONE = "That day's already gone - which day did you mean?"
HAS_GUESTS = "That has guests. Change it in Google Calendar."
RULES_UNREADABLE = "Google's answer for that repeating event didn't make sense, so I've left it as it was."
NO_ANSWER = "Google didn't answer in time"
LOST = "I lost Google partway through"
TROUBLE = "Google had trouble"
NEEDS_SIGN_IN = "I need signing in to Google again. That's done on the settings page."

# Most events said for one day before the rest are only counted.
MOST_EVENTS_SAID = 5

# From this many days, what's on is given as an overview - which days there's uni
# or work, and the rest by name - rather than every time of every day. A week said
# in full, as "uni from 11:30 AM until 1 PM and from 2:30 PM until 4:30 PM" a day
# at a time, took 52 seconds; as an overview, about 18. A fortnight, read out
# whole, once took three and a half minutes. "What about Friday?" still gets that
# day in full.
OVERVIEW_FROM_DAYS = 3

# Most things other than the activities named in an overview.
MOST_OTHERS_IN_OVERVIEW = 5

# How far ahead to look for one thing on the calendar - "when's my MOT?" - and the
# most matches said.
SEARCH_DAYS = 90
MOST_FOUND = 3

# How long an event added with no end lasts.
DEFAULT_LENGTH = timedelta(hours=1)

# The only ways a time to add is read - "20261029" or "2026-10-29T15" would be read by
# fromisoformat as times nobody meant.
WHEN_GIVEN = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?")

# The time an event to change is at, as the model gives it - "15:00" for "the 3 o'clock one".
AT_GIVEN = re.compile(r"(\d{1,2}):(\d{2})")

# What the user's own calendar is called, whatever Google names it - "my calendar", "my own".
OWN_CALENDAR = {"", "primary", "personal", "own", "mine", "main"}

# How a calendar named by an email address is said, and told to Claude, in its place - the
# user's own, which Google names by their address, and any other, usually a person's own
# shared with the user, named by theirs.
YOUR_CALENDAR = "your calendar"
A_SHARED_CALENDAR = "a shared calendar"

# What calendars.json keeps, and each starts as: the calendars chosen as activities, or
# not, by id; those chosen out of what's on, each id to False; and the one new events go
# to, or None for the primary.
CALENDAR_CHOICES = {"activities": {}, "whats_on": {}, "new_events": None}


class SignInNeeded(Exception):
    """There's no sign-in to Google, or Google no longer accepts it."""


class ClientRefused(SignInNeeded):
    """Google doesn't know Nova's OAuth client, or not with that secret."""


class GoogleSignInError(ValueError):
    """A sign-in to Google which didn't work, said as the page says it."""


def _challenge(verifier: str) -> str:
    """The S256 code challenge for a PKCE verifier, as RFC 7636 has it."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GoogleSignIn:
    """
    Nova's sign-in to Google: signs in once, keeps the refresh token, and gives an
    access token for each request.

    A refresh token is all that's saved. The access token is kept in memory only, so
    a copy of the data folder never holds one that works on its own.
    """

    def __init__(self, client_id: str, client_secret: str, path: str = paths.GOOGLE_TOKEN_FILE,
                 session: Optional[requests.Session] = None):
        """
        Args:
            client_id: The OAuth client's id, GOOGLE_CLIENT_ID.
            client_secret: The OAuth client's secret, GOOGLE_CLIENT_SECRET.
            path: Where the sign-in is kept.
            session: What requests go through - a fake, in the tests.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.path = path
        self._session = session or requests.Session()
        # The sign-in under way, kept in memory only and used once.
        self._state: Optional[str] = None
        self._verifier: Optional[str] = None
        self._started_at = 0.0
        # Each request to the page has a thread of its own, so two finishes at once, or a
        # start between a finish's read and its clear, could otherwise share a state. Not
        # the access token's lock, which is held while Google's asked for one.
        self._signing = threading.Lock()
        self._access: Optional[str] = None
        self._access_until = 0.0
        # Several requests at once refresh the access token only once between them.
        self._lock = threading.Lock()

    def start(self) -> str:
        """
        Starts a sign-in.

        Returns:
            The address to open in a browser, to sign in to Google there.
        """
        state, verifier = secrets.token_urlsafe(), secrets.token_urlsafe(64)
        with self._signing:
            self._state, self._verifier, self._started_at = state, verifier, time.monotonic()
        fields = {
            "client_id": self.client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Offline gives a refresh token, and consent makes sure it's given even
            # when this account has signed Nova in before.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORISE}?{urlencode(fields)}"

    def finish(self, pasted: str):
        """
        Finishes the sign-in started, from the address Google sent the browser to.

        The state and verifier are used once, whether it works or not, so an address
        can't be tried again.

        Args:
            pasted: The address, pasted from the browser's address bar.

        Raises:
            GoogleSignInError: If the address isn't from the sign-in started, or none
                was, or it ran out, or was cancelled on Google's page, or it hasn't got
                Google's code in it - nothing is asked or saved - or if Google refused
                the code for the client's set-up, was slowing Nova down, wasn't given
                everything Nova asks for, or gave no whole sign-in to keep - nothing is
                saved - or the sign-in couldn't be saved.
            ValueError: If the address couldn't be read at all, such as "http://[x".
            SignInNeeded: If Google turned the code down, or ClientRefused, if it
                doesn't know the client.
            requests.RequestException: If Google couldn't be reached, or had trouble.
        """
        with self._signing:
            state, verifier, started_at = self._state, self._verifier, self._started_at
            self._state = self._verifier = None
        fields = {name: values[0] for name, values in parse_qs(urlsplit(str(pasted).strip()).query).items()}
        # Compared in constant time, so how much of it matched can't be timed - as bytes,
        # since compare_digest won't take a string with "é" in it, and a pasted address
        # could have anything.
        if state is None or not secrets.compare_digest(fields.get("state", "").encode("utf-8"),
                                                       state.encode("ascii")):
            raise GoogleSignInError("That address isn't from this sign-in - start again.")
        if time.monotonic() - started_at > SIGN_IN_LASTS:
            raise GoogleSignInError("That sign-in ran out - start again.")
        if "error" in fields:
            # Cancel, on Google's page - said as SpotifySignIn says it, not as an address
            # copied short.
            logger.info(f"Google turned the sign-in down: {logged_error(fields['error'])}")
            raise GoogleSignInError("Google didn't accept that sign-in - start again.")
        if not fields.get("code"):
            raise GoogleSignInError("That address hasn't got Google's code in it.")

        try:
            answer = self._ask_for_token({"grant_type": "authorization_code", "code": fields["code"],
                                          "code_verifier": verifier, "redirect_uri": REDIRECT})
        except requests.HTTPError as e:
            response = e.response
            if response is None or not 400 <= response.status_code < 500:
                raise
            # Google slowing Nova down is a moment out of reach, not the client being wrong.
            if response.status_code == 429 or _rate_limited(response):
                logger.warning(f"Google slowed the sign-in down: HTTP {response.status_code}")
                raise GoogleSignInError("Google is slowing Nova down - start the sign-in again in a minute.") \
                    from None
            # A client set up wrongly in Google Cloud - a redirect it doesn't allow, the
            # API switched off - which trying again, or Google being out of reach, isn't.
            code = self._error(response)
            said = code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else f"HTTP {response.status_code}"
            logger.warning(f"Google refused the sign-in: {said}")
            raise GoogleSignInError(f"Google refused the sign-in ({said}) - "
                                    f"check the client in Google Cloud.") from None
        # Checked whole before anything's kept: a refresh token that isn't text would be
        # saved but never read back, and without an access token the sign-in would be
        # saved and then said not to have worked.
        tokens = [answer.get("refresh_token"), answer.get("access_token")]
        if not all(isinstance(token, str) and token for token in tokens) or \
                not isinstance(answer.get("scope", ""), str) or str(answer.get("token_type")).lower() != "bearer":
            raise GoogleSignInError("Google didn't give Nova a sign-in to keep - try again.")
        # Google lets each box on its page be unticked. A sign-in without adding would only
        # have failed later, by voice.
        granted = answer.get("scope", " ".join(SCOPES)).split()
        missing = [SCOPES_SAID[scope] for scope in SCOPES if scope not in granted]
        if missing:
            raise GoogleSignInError(f"Google didn't let Nova {' or '.join(missing)} - sign in again, "
                                    f"with every box ticked.")
        try:
            storage.save_json(self.path, {"refresh_token": answer["refresh_token"],
                                          "scope": answer.get("scope", " ".join(SCOPES))})
        except OSError as e:
            # Its words name the file, and why - never the token.
            logger.error(f"Couldn't save the sign-in to Google: {e}")
            raise GoogleSignInError("Couldn't save the sign-in - check Nova's data folder.") from None
        with self._lock:
            self._keep(answer)
        logger.info("Signed in to Google")

    def signed_in(self) -> bool:
        """
        Returns:
            Whether there's a saved sign-in.
        """
        return self._refresh_token() is not None

    def access_token(self) -> str:
        """
        An access token for a request to Google, refreshed when it's nearly run out.

        Returns:
            The access token.

        Raises:
            SignInNeeded: If there's no saved sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached.
        """
        with self._lock:
            if self._access and time.monotonic() < self._access_until - REFRESH_EARLY:
                return self._access
            refresh_token = self._refresh_token()
            if refresh_token is None:
                raise SignInNeeded("Nova isn't signed in to Google.")
            self._keep(self._ask_for_token({"grant_type": "refresh_token", "refresh_token": refresh_token}))
            return self._access

    def drop_access_token(self):
        """
        Forgets the access token kept, so the next request gets one afresh - for one
        Google has turned down before it ran out, as it does once the sign-in is removed
        in the Google account.
        """
        with self._lock:
            self._access = None

    def _ask_for_token(self, fields: Dict[str, str]) -> Dict:
        """
        Asks Google's token address for a token.

        Args:
            fields: What's asked, besides the client's id and secret.

        Returns:
            Google's answer.

        Raises:
            SignInNeeded: If Google says the grant is no good - revoked, or run out - or
                won't let the client have one, or ClientRefused, if it doesn't know the client.
            requests.RequestException: If Google couldn't be reached, or refused.
        """
        response = self._session.post(TOKEN, data={"client_id": self.client_id,
                                                   "client_secret": self.client_secret, **fields},
                                      timeout=REQUEST_TIMEOUT)
        error = self._error(response) if response.status_code in (400, 401) else None
        if error == "invalid_client":
            raise ClientRefused("Google doesn't know Nova's OAuth client.")
        if error in SIGN_IN_ERRORS:
            raise SignInNeeded("Google no longer accepts Nova's sign-in.")
        response.raise_for_status()
        try:
            answer = response.json()
        except ValueError as e:
            raise requests.RequestException("Google's answer wasn't JSON") from e
        if not isinstance(answer, dict):
            raise requests.RequestException("Google's answer wasn't a token")
        return answer

    @staticmethod
    def _error(response) -> Optional[str]:
        """The OAuth error Google answered with, such as invalid_grant."""
        try:
            body = response.json()
        except ValueError:
            return None
        return body.get("error") if isinstance(body, dict) else None

    def _keep(self, answer: Dict):
        """
        Keeps an access token from Google's answer in memory, until it runs out.

        Raises:
            requests.RequestException: If the answer hasn't got one.
        """
        if not answer.get("access_token"):
            raise requests.RequestException("Google's answer had no access token")
        self._access = answer["access_token"]
        try:
            lasts = float(answer.get("expires_in", 0))
        except (TypeError, ValueError):
            lasts = 0.0
        self._access_until = time.monotonic() + lasts

    def _refresh_token(self) -> Optional[str]:
        """The saved refresh token, or None if there isn't one."""
        try:
            saved = storage.load_json(self.path)
        except (OSError, ValueError) as e:
            logger.warning(f"Couldn't read the Google sign-in, so it needs doing again: {type(e).__name__}")
            return None
        if not isinstance(saved, dict) or not isinstance(saved.get("refresh_token"), str):
            return None
        return saved["refresh_token"] or None


class GoogleCalendar:
    """The user's calendars, read from Google itself with Nova's own sign-in."""

    def __init__(self, sign_in: GoogleSignIn, choices: ChoiceStore, session: Optional[requests.Session] = None):
        """
        Args:
            sign_in: Nova's sign-in to Google, which gives each request its access token.
            choices: calendars.json, as CALENDAR_CHOICES - which calendars are activities
                by id, for one that isn't by itself, or is but shouldn't be; which are out
                of what's on; and where new events go. Each is chosen on the settings page
                and used from the next request.
            session: What requests go through - a fake, in the tests.
        """
        self.sign_in = sign_in
        self.choices = choices
        self._session = session or requests.Session()
        # Google's list of the user's calendars, and when it was read, by time.monotonic().
        self._known: List[Dict] = []
        self._known_at: Optional[float] = None
        # Goes up at each sign-in, so a read of the list begun before one - with the account
        # before's access token - can't keep its list once the new account's is read.
        self._generation = 0
        self._reading = threading.Lock()
        # Called, on a thread of its own, after each write Google says it did. The brain has it
        # ask Home Assistant to read its calendars again: Home Assistant reads Google only every
        # 15 minutes, so without it "cancel the dentist" would leave its dashboards showing the
        # dentist for up to a quarter of an hour.
        self.after_change: Optional[Callable[[], None]] = None

    @classmethod
    def from_env(cls, config) -> Optional["GoogleCalendar"]:
        """
        The calendar, once Google's keys are set - GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
        in .env, or under Keys on the settings page.

        Args:
            config: Configs loaded from config.yaml - none of which the calendar needs.

        Returns:
            A GoogleCalendar, or None without both keys.
        """
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            logger.info("The calendar is off until GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set - see the "
                        "README's Google Calendar")
            return None
        # The paths are looked up now, not when this module was imported, as paths.DATA_DIR can be moved first.
        return cls(GoogleSignIn(client_id, client_secret, paths.GOOGLE_TOKEN_FILE),
                   ChoiceStore(os.path.join(paths.DATA_DIR, "calendars.json"), CALENDAR_CHOICES))

    def _get(self, url: str, params: Dict) -> Dict:
        """
        Asks Google's calendar for something, with Nova's access token.

        Args:
            url: The address.
            params: What's asked, in its query.

        Returns:
            Google's answer.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError: If its answer wasn't a JSON object.
        """
        # With no network at all, nothing's tried - neither the sign-in nor the calendar,
        # each of which would wait out its timeout to say so.
        if not network.online():
            raise requests.ConnectionError("No network connection")
        for _ in range(2):
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                         headers={"Authorization": f"Bearer {self.sign_in.access_token()}"})
            if response.status_code != 401:
                break
            # Turned down before it ran out - the sign-in removed in the Google account, say.
            # Got afresh, the sign-in says whether it still works; turned down again, it doesn't.
            self.sign_in.drop_access_token()
        if response.status_code == 401:
            raise SignInNeeded("Google turned Nova's access token down.")
        response.raise_for_status()
        answer = response.json()
        if not isinstance(answer, dict):
            raise ValueError("Google's answer wasn't an object")
        return answer

    def _pages(self, url: str, params: Dict) -> List[Dict]:
        """
        Every item Google lists, over as many pages as it gives them in.

        Raises:
            The same as _get, and ValueError if a page's items aren't a list of objects, or
            there are more than MOST_PAGES of them.
        """
        items, page = [], None
        for _ in range(MOST_PAGES):
            answer = self._get(url, {**params, "pageToken": page} if page else params)
            listed = answer.get("items", [])
            if not isinstance(listed, list) or not all(isinstance(item, dict) for item in listed):
                raise ValueError("Google's items aren't a list")
            items += listed
            page = answer.get("nextPageToken")
            if not page:
                return items
        raise ValueError(f"Google gave more than {MOST_PAGES} pages")

    def calendars(self, max_age: float = 300) -> List[Dict]:
        """
        Google's list of the user's calendars, read afresh once it's older than max_age -
        so one added in Google turns up by itself.

        Args:
            max_age: Seconds the list read last is good for.

        Returns:
            Each calendar as {"id", "name", "primary", "writable"} - writable for the user's
            own, as calendar.events.owned only adds to those.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError: If its answer wasn't a list of calendars.
        """
        # Given back only as it's kept, as the page checks a tap against what's kept: with a
        # sign-in while it was read, it's read again - unless the new account's read has
        # been kept meanwhile. Each time round needs another sign-in, which a person makes.
        while True:
            with self._reading:
                if self._known_at is not None and time.monotonic() - self._known_at < max_age:
                    return list(self._known)
                generation = self._generation
            listed = []
            for entry in self._pages(CALENDAR_LIST, {}):
                if not isinstance(entry.get("id"), str):
                    raise ValueError("A calendar in Google's list has no id")
                listed.append({"id": entry["id"], "name": entry.get("summaryOverride") or entry.get("summary") or "",
                               "primary": entry.get("primary") is True,
                               "writable": entry.get("accessRole") == "owner"})
            with self._reading:
                if generation == self._generation:
                    self._known, self._known_at = listed, time.monotonic()
                    return list(listed)

    def read_list_again(self):
        """
        Reads Google's list of the calendars afresh, straight away but not waited for -
        after a sign-in, which may be another account's. Until it's read, the activities
        heard are the last list's, and if it can't be, it's read when next wanted.
        """
        with self._reading:
            self._generation += 1
            self._known_at = None
        threading.Thread(target=self._read_list, daemon=True, name="calendars").start()

    def _read_list(self):
        try:
            self.calendars()
        except READ_FAILURES as e:
            # Only the kind of failure - a requests error names the address.
            logger.warning(f"Couldn't read the calendars from Google after the sign-in: {type(e).__name__}")

    def known(self) -> List[Dict]:
        """The calendars Google listed last, without asking it - none until it's been asked."""
        return list(self._known)

    def activity_calendars(self) -> Dict[str, Dict]:
        """
        The activities, from the calendars Google listed last, without asking it: every
        calendar but the user's own, Google's own (see GOOGLES_OWN) and one named by an
        email address, unless calendars.json says otherwise for it.

        Returns:
            Each activity's word - its calendar's name as it's said, in lower case - to its calendar.
        """
        chosen = self._chosen("activities")
        found: Dict[str, Dict] = {}
        for calendar in self.known():
            word = spoken_name(calendar).strip().lower()
            if word and self._is_activity(calendar, chosen):
                # Of two calendars with one name, the first in Google's list is the one heard.
                found.setdefault(word, calendar)
        return found

    def is_activity(self, calendar: Dict) -> Optional[bool]:
        """
        Whether a calendar is an activity, for the settings page's switch - as
        activity_calendars has it, so the switch is never on for one that's never heard.

        Args:
            calendar: A calendar, as calendars gives it.

        Returns:
            True or False, or None for one that can't be an activity just now: one with
            no name, or a name an activity listed before it has.
        """
        activities = self.activity_calendars()
        if any(each["id"] == calendar["id"] for each in activities.values()):
            return True
        word = spoken_name(calendar).strip().lower()
        heard = activities.get(word)
        place = {each["id"]: i for i, each in enumerate(self.known())}
        # Switched on, one listed after the activity of its name still wouldn't be the one heard.
        taken = heard is not None and place.get(heard["id"], -1) < place.get(calendar["id"], len(place))
        return None if not word or taken else False

    @staticmethod
    def _is_activity(calendar: Dict, chosen: Dict) -> bool:
        choice = chosen.get(calendar["id"])
        if isinstance(choice, bool):
            return choice
        # A person's calendar shared with the user is named by their email address, and
        # is no more an activity than the holidays are.
        return not calendar["primary"] and not calendar["id"].endswith(GOOGLES_OWN) and not named_by_address(calendar)

    def _chosen(self, key: str) -> Dict:
        """One of calendars.json's choices by calendar id - none, if the file's been edited into something else."""
        chosen = self.choices.get(key)
        return chosen if isinstance(chosen, dict) else {}

    def left_out(self) -> Set[str]:
        """The ids of the calendars chosen out of what's on."""
        return {calendar_id for calendar_id, on in self._chosen("whats_on").items() if on is False}

    def choose(self, calendar_id: str, whats_on: Optional[bool] = None, activity: Optional[bool] = None):
        """
        Chooses whether a calendar is in what's on, and whether it's an activity - each
        used from the next request. A choice is kept by the calendar's id, so one Google
        stops listing for a while is as it was chosen when it's back.

        Args:
            calendar_id: One of the calendars Google listed last - read it first with
                calendars, as nothing's known of it after a restart until then.
            whats_on: Whether it's read for what's on and looking something up, or None to leave it.
            activity: Whether it's an activity, or None to leave it.

        Raises:
            ValueError: If Google isn't listing it.
            OSError: If calendars.json couldn't be saved. Nothing is changed.
        """
        if not any(calendar["id"] == calendar_id for calendar in self.known()):
            raise ValueError("Google isn't showing that calendar just now.")

        def change(state: Dict):
            for key, value in (("whats_on", whats_on), ("activities", activity)):
                if value is None:
                    continue
                if not isinstance(state[key], dict):
                    state[key] = {}
                state[key][calendar_id] = value
            # Only one that's out is kept, so the file says what's been chosen.
            if whats_on:
                state["whats_on"].pop(calendar_id)

        self.choices.change(change)

    def set_new_events(self, calendar_id: str):
        """
        Chooses the calendar new events go to, from the next one added.

        Args:
            calendar_id: One of the user's own calendars Google listed last - read it first
                with calendars, as for choose.

        Raises:
            ValueError: If Google isn't listing it, or Nova can't add to it.
            OSError: If calendars.json couldn't be saved. Nothing is changed.
        """
        calendar = next((each for each in self.known() if each["id"] == calendar_id), None)
        if calendar is None:
            raise ValueError("Google isn't showing that calendar just now.")
        if not calendar["writable"]:
            raise ValueError("Nova can only add to your own calendars.")
        self.choices.change(lambda state: state.update(new_events=calendar_id))

    def activity_words(self) -> Dict[str, str]:
        """
        Every word an activity is heard by locally, to the activity: its name, its plural,
        and SAME_AS's words for it. A name of two or more words is heard by its first word
        too - "uni" for "Uni timetable" - unless another activity's name starts with it, or
        it's SAME_AS's for another, as "working" is Work's beside "Working group". Each is
        as the request is, once intents.py has tidied it - "Work (Tesco)" is "work tesco".
        One named by an email address isn't heard at all - "a shared calendar" would be
        heard by "a".

        Returns:
            e.g. {"work": "work", "works": "work", "shift": "work", ...}.
        """
        names: Dict[str, str] = {}
        for word, calendar in self.activity_calendars().items():
            if named_by_address(calendar):
                continue
            # Of two that tidy alike - "Work" and "Work!" - the first listed, as activity_calendars has it.
            names.setdefault(_as_said(word), word)
        names.pop("", None)
        firsts: Dict[str, List[str]] = {}
        for said in names:
            firsts.setdefault(said.split()[0], []).append(said)
        heard: List[Tuple[str, str]] = [(said, word) for said, word in names.items()]
        heard += [(said if said.endswith("s") else said + "s", word) for said, word in names.items()]
        heard += [(same, word) for said, word in list(heard) for same in SAME_AS.get(said, ())]
        first_words = [(first, names[saids[0]]) for first, saids in firsts.items()
                       if len(saids) == 1 and first != saids[0]]
        heard += first_words + [(same, word) for first, word in first_words for same in SAME_AS.get(first, ())]
        words: Dict[str, str] = {}
        # A name is heard as that activity before it's another's plural, SAME_AS or first word.
        for said, word in heard:
            words.setdefault(said, word)
        return words

    def events(self, first: date, days: int, calendar_ids: Optional[List[str]] = None,
               leave_out: Set[str] = frozenset(), whole: bool = False) -> List[Dict]:
        """
        Everything on the calendars over some days.

        Args:
            first: The first day.
            days: How many days, from 1.
            calendar_ids: The calendars to read, or None for every one in Google's list.
            leave_out: Calendars not to read of those - the ones chosen out of what's on.
            whole: Whether to give what changing an event needs as well.

        Returns:
            The events, earliest first, each a dict with "summary", "start", "end",
            "all_day", "calendar", its calendar's name as it's said (see spoken_name), and "calendar_id" - start a local
            datetime, or a date for an all-day event. With whole, each has its "id" too; "recurring_id",
            its series' id for one time a repeating event is on, else None; "invite", whether someone
            else organised it; "writable", whether Nova can change its calendar's events; and "event",
            Google's own.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError, KeyError, TypeError: If its answer wasn't the shape of events.

        Reading every calendar, one Google won't give the events of - deleted, or no longer
        shared - is left out, with a warning, and the rest are read.
        """
        calendars = self.calendars()
        names = {calendar["id"]: spoken_name(calendar) for calendar in calendars}
        writable = {calendar["id"]: calendar["writable"] for calendar in calendars}
        # Each end made from its own day, not the start plus so many hours, so a day the
        # clocks change on is still from midnight to midnight.
        window = {"timeMin": datetime.combine(first, day_time()).astimezone().isoformat(),
                  "timeMax": datetime.combine(first + timedelta(days=days), day_time()).astimezone().isoformat(),
                  # Each time a repeating event is on, as an event of its own - as Home Assistant gave them.
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": EVENTS_PAGE}
        found = []
        # Left out once Google's list is read, so the first read after a restart - with
        # nothing known before it - still leaves out the right ones.
        reading = [calendar_id for calendar_id in (list(names) if calendar_ids is None else calendar_ids)
                   if calendar_id not in leave_out]
        for calendar_id in reading:
            try:
                events = self._pages(f"{CALENDARS}/{quote(calendar_id, safe='')}/events", window)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                # One asked for by id - an activity's own - is the whole read, so it's never
                # "you haven't got gym" for a Gym calendar that wasn't read.
                gone = status in CALENDAR_GONE and not _rate_limited(e.response)
                if gone:
                    # Its list is read afresh next time, so a calendar since deleted is gone from it.
                    self._known_at = None
                if not gone or calendar_ids is not None:
                    raise
                # Not its id, or the error, which has the address with the id in it.
                logger.warning(f"Left a calendar out, as Google wouldn't give its events: {status}")
                continue
            for event in events:
                if event.get("status") == "cancelled":
                    continue
                # A calendar not in Google's list has no name to give - and its id, which
                # for the primary is the user's email address, is never said in its place.
                read = {**_read_event(event), "calendar": names.get(calendar_id, ""), "calendar_id": calendar_id}
                if whole:
                    # Google marks the organiser "self" when it's the calendar this copy is on - the
                    # user's own event, on any of their calendars. Anyone else's is an invite.
                    organizer = event.get("organizer")
                    read.update(id=event.get("id"), recurring_id=event.get("recurringEventId"),
                                invite=isinstance(organizer, dict) and organizer.get("self") is not True,
                                writable=writable.get(calendar_id, False), event=event)
                found.append(read)
        return sorted(found, key=lambda event: _sort_key(event["start"]))

    def agenda(self, first: date, days: int = 1, today: Optional[date] = None) -> Dict:
        """
        What's on the calendar, as something to say.

        Args:
            first: The first day asked about.
            days: How many days, e.g. 2 for a weekend.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and the agenda as the 'message'.
        """
        try:
            events = self.events(first, days, leave_out=self.left_out())
        except READ_FAILURES as e:
            return _failed(e)
        events = _as_activities(events, self.activity_calendars(), first)
        return {"success": True, "message": describe_agenda(events, first, days, today or date.today())}

    def find(self, wanted: str, first: date, today: Optional[date] = None) -> Dict:
        """
        When one thing is on the calendar, for "when's my MOT?". Reading out whole
        days instead, Claude once read a fortnight of them looking for it.

        Args:
            wanted: A word or two from its name, e.g. "MOT".
            first: The first day to look from.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and when it is as the 'message'.
        """
        words = [word.strip(".,!?'\"") for word in (wanted or "").split()]
        patterns = [_called([word]) for word in words if word]
        if not patterns:
            return {"success": False, "message": "I didn't catch what to look for."}
        try:
            events = self.events(first, SEARCH_DAYS, leave_out=self.left_out())
        except READ_FAILURES as e:
            return _failed(e)
        found = [event for event in events if all(pattern.search(event["summary"]) for pattern in patterns)]
        return {"success": True, "message": describe_found(" ".join(words), found, today or date.today())}

    def activity(self, name: str, first: date, days: int = 1, today: Optional[date] = None,
                 count: Optional[int] = None, now: Optional[datetime] = None) -> Dict:
        """
        When the user has one of their activities - work, uni, the gym - from its calendar
        alone. Every event on it counts, whatever it's called.

        Args:
            name: The activity, or a word it's heard by, e.g. "work" or "shifts".
            first: The first day asked about.
            days: How many days.
            today: Today's date - only given by the tests.
            count: Only say the next this many times, for "when's my next shift?".
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and the times as the 'message'.
        """
        said = (name or "").strip()
        if not said:
            return {"success": False, "message": "I didn't catch which of your calendars you meant."}
        try:
            # Google's list is read, if it's old, before it's looked in.
            self.calendars()
            activities = self.activity_calendars()
            word = said.lower() if said.lower() in activities else self.activity_words().get(_as_said(said))
            if word is None:
                return {"success": False, "message": f"I haven't got a calendar called {said}."}
            events = self.events(first, days, calendar_ids=[activities[word]["id"]])
        except READ_FAILURES as e:
            return _failed(e)
        if count:
            return {"success": True, "message": describe_next(word, events, count, first,
                                                              now or datetime.now().astimezone())}
        return {"success": True, "message": describe_activity(word, events, first, days, today or date.today())}

    def add(self, summary: str, start: str, end: Optional[str] = None, all_day: Optional[bool] = None,
            calendar: Optional[str] = None, today: Optional[date] = None) -> Dict:
        """
        Adds an event to one of the user's own calendars, for "put the dentist on Thursday
        at 3". It's sent once, never again by itself: Google may have added it already.

        Args:
            summary: What the event is, e.g. "Dentist".
            start: When it starts, local - "YYYY-MM-DD HH:MM", or "YYYY-MM-DD" for all day.
            end: When it ends, the same way - an end day is included. None for
                DEFAULT_LENGTH, or for the one day.
            all_day: Whether it's all day, even with a time given.
            calendar: The calendar the user named, by its name or an activity's word -
                "work" is Work. None for the one new events go to.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and what was added, or why not, as the 'message' - and once
            it's added, an 'undo', which takes it back off.
        """
        summary = summary.strip() if isinstance(summary, str) else ""
        if not summary:
            return {"success": False, "message": "I didn't catch what to add."}
        today = today or date.today()
        times = _event_times(start, end, all_day)
        if isinstance(times, str):
            return {"success": False, "message": times}
        # Earlier today is fine - something just done, logged - but a day gone is most
        # likely the week or the year misheard.
        if _first_day(times[0]) < today:
            return {"success": False, "message": DAY_GONE}
        try:
            found = self._calendar_to_add_to(calendar)
            if isinstance(found, str):
                return {"success": False, "message": found}
            chosen, default = found
            event_id = self._insert(chosen["id"], {"summary": summary, "start": times[0], "end": times[1]})
        except _NotSure as e:
            logger.warning(f"Couldn't tell whether Google added an event: {e}")
            return {"success": False, "message": f"{e.said}, so I can't tell whether {summary} was added."}
        except READ_FAILURES as e:
            return _not_added(e)
        logger.info("Added an event to Google's calendar")
        to = f" to {spoken_name(chosen)}" if chosen["id"] != default["id"] and chosen["name"] else ""
        added = _event_url(chosen["id"], event_id)
        return {"success": True, "message": f"Added {summary}{to}, {_added_when(times[0], today)}.",
                "undo": _undo(summary, lambda: self._send_once("DELETE", added),
                              f"Took {summary} back off your calendar.", f"I can't tell whether {summary} was taken off.")}

    def remove(self, search: str, day: Optional[date] = None, at: Optional[str] = None, following: bool = False,
               calendar: Optional[str] = None, today: Optional[date] = None, now: Optional[datetime] = None) -> Dict:
        """
        Removes an event from one of the user's own calendars, for "cancel the dentist on
        Thursday" - straight away, and said back so a mistake is heard, and undone by "undo
        that". Only that day's of a repeating event, unless following: then from that one on,
        as Google Calendar's "this and following events" does, so the times before it - past
        shifts and lectures - stay. It's sent once, never again by itself.

        Args:
            search: A word or two from its name, e.g. "dentist".
            day: The day it's on, if the user said - else it's looked for from today.
            at: The time it starts, "HH:MM", if the user said.
            following: Whether to remove it from this one on, for "all of them" or "every week".
            calendar: The calendar it's on, if the user named one - "work" is Work.
            today: Today's date - only given by the tests.
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and what was removed, or why not, as the 'message' - and once
            it's removed, an 'undo', which puts it back.
        """
        today, now = _today_and_now(today, now)
        found = self._to_change(search, day, at, following, today, now, calendar)
        if isinstance(found, str):
            return {"success": False, "message": found}
        summary = found["summary"]
        try:
            if following and found["recurring_id"]:
                message, undo = self._remove_following(found, today)
            else:
                # A time a repeating event is on has an id of its own, so this removes that day only.
                url = _event_url(found["calendar_id"], found["id"])
                self._send_once("DELETE", url)
                when = _when_changed(found, today)
                message = f"Removed {summary}, {when}."
                undo = _undo(summary, lambda: self._confirm_or_copy(url, found["calendar_id"], _copy(found["event"])),
                             f"Put {summary} back, {when}.", f"I can't tell whether {summary} was put back.")
        except _NotSure as e:
            logger.warning(f"Couldn't tell whether Google removed an event: {e}")
            return {"success": False, "message": f"I can't tell whether {summary} was removed."}
        except _SeriesUnreadable:
            # Raised before anything is written, so it's still as it was.
            logger.error("Couldn't read a repeating event's rules, so it's left as it was")
            return {"success": False, "message": RULES_UNREADABLE}
        except READ_FAILURES as e:
            if _gone(e):
                logger.info("An event to remove was already gone from Google's calendar")
                return {"success": False, "message": f"{summary} was already gone."}
            return _not_changed(e)
        logger.info("Removed an event from Google's calendar")
        return {"success": True, "message": message, "undo": undo}

    def _remove_following(self, found: Dict, today: date) -> Tuple[str, Callable[[], Dict]]:
        """
        Removes a repeating event from one of its times on: the whole series from its first,
        else the series ended just before this one.

        Args:
            found: The time, as _to_change gives it.
            today: Today's date.

        Returns:
            What to say, and the undo - which puts the series back as it was, with the times
            of it deleted on their own deleted again.

        Raises:
            The same as _cancelled_from and _send_once, and _SeriesUnreadable if the series has no rules.
        """
        summary = found["summary"]
        calendar_id = found["calendar_id"]
        series_url = _event_url(calendar_id, found["recurring_id"])
        series = self._get(series_url, {})
        rules = series.get("recurrence")
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            raise _SeriesUnreadable("The series has no rules")
        # By when this one was meant to be, which the series' rules make it - not where it's
        # been moved to since.
        this = _google_when(found["event"].get("originalStartTime") or found["event"]["start"])
        on = f"from {spoken_day(_day_of(found), today)} on"
        # Read before anything is written: Google forgets the times deleted on their own once the series
        # ends before them, so the undo has to delete them again.
        cancelled = self._cancelled_from(series_url, this)
        if this == _google_when(series["start"]):
            self._send_once("DELETE", series_url)
            # A copy added in its place is a series of its own, so it leaves those times out itself.
            copy = {**_copy(series), "recurrence": rules + _exdates(cancelled, series["start"])}

            def put_back():
                # Only the series itself has those times to delete again.
                if self._confirm_or_copy(series_url, calendar_id, copy):
                    return self._delete_again(calendar_id, cancelled, today)
                return None
        else:
            self._send_once("PATCH", series_url, {"recurrence": _cut_rules(rules, this)})

            def put_back():
                self._send_once("PATCH", series_url, {"recurrence": rules})
                return self._delete_again(calendar_id, cancelled, today)
        undo = _undo(summary, put_back, f"Put {summary} back {on}.", f"I can't tell whether {summary} was put back.")
        return f"Removed {summary} {on}.", undo

    def _confirm_or_copy(self, url: str, calendar_id: str, copy: Dict):
        """
        Puts an event removed back: set back to confirmed, so its reminders, guests and
        repeats come back with it - else, if Google won't, a copy added in its place.

        Args:
            url: The event's address.
            calendar_id: Its calendar.
            copy: What's added if Google won't set it back - see COPIED.

        Returns:
            Whether the event itself is back - not a copy of it, which has an id of its own.

        Raises:
            The same as _send_once.
        """
        try:
            self._send_once("PATCH", url, {"status": "confirmed"})
        except requests.HTTPError as e:
            # Only a refusal - not Google slowing Nova down, which hasn't looked at it.
            if not _turned_down(e):
                raise
            logger.info(f"Google wouldn't set an event back to confirmed ({e.response.status_code}), so it's copied")
            self._insert(calendar_id, copy)
            return False
        return True

    def change(self, search: str, day: Optional[date] = None, at: Optional[str] = None, start: Optional[str] = None,
               end: Optional[str] = None, summary: Optional[str] = None, following: bool = False,
               calendar: Optional[str] = None, today: Optional[date] = None, now: Optional[datetime] = None) -> Dict:
        """
        Moves or renames an event on one of the user's own calendars, for "move the dentist to
        Friday at 4" or "rename it to dentist check-up" - straight away, and said back so a
        mistake is heard, and undone by "undo that". Only that day's of a repeating event, unless
        following: then from that one on, as Google Calendar's "this and following events" does,
        so the times before it stay - and then only its time of day can move, not its day, which
        a time alone is: it's on the day of the one found. It's sent once, never again by itself.

        Args:
            search: A word or two from its name, e.g. "dentist".
            day: The day it's on, if the user said - else it's looked for from today.
            at: The time it starts, "HH:MM", if the user said.
            start: When it's to start, local - "YYYY-MM-DD HH:MM", "YYYY-MM-DD" for the day
                alone, which keeps the time it's at, or keeps one all day all day, or "HH:MM"
                for the time alone, which is on the day it's on. None to leave it.
            end: When it's to end, the same way - an end day is included, and a time alone
                is on the start's day. None for the length it had, or DEFAULT_LENGTH for one
                all day given a time.
            summary: What it's to be called, or None to leave its name.
            following: Whether to change it from this one on, for "all of them" or "every week".
            calendar: The calendar it's on, if the user named one - "work" is Work.
            today: Today's date - only given by the tests.
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and what was changed, or why not, as the 'message' - and once
            it's changed, an 'undo', which puts it back as it was.
        """
        today, now = _today_and_now(today, now)
        new_name = summary.strip() if isinstance(summary, str) else ""
        start, end = (None if isinstance(each, str) and not each.strip() else each for each in (start, end))
        if start is None and not new_name:
            return {"success": False, "message": "I didn't catch what to change."}
        first = last = None
        if start is not None or end is not None:
            first, last = _time_said(start), _time_said(end) if end is not None else None
            if first is None or (end is not None and last is None):
                return {"success": False, "message": "I didn't catch when that is."}
            # As adding: earlier today is fine, but a day gone is most likely the week or the
            # year misheard. A time alone has no day yet - see below.
            if not isinstance(first, day_time) and (first.date() if isinstance(first, datetime) else first) < today:
                return {"success": False, "message": DAY_GONE}
        found = self._to_change(search, day, at, following, today, now, calendar)
        if isinstance(found, str):
            return {"success": False, "message": found}
        # A time alone - "move the dentist to 5" - is on the event's own day, which the user needn't
        # say and Claude can't know, then checked as a day said is. An end alone is on its start's.
        if isinstance(first, day_time):
            first = datetime.combine(_day_of(found), first).astimezone()
            if first.date() < today:
                return {"success": False, "message": DAY_GONE}
        if isinstance(last, day_time):
            last = datetime.combine(first.date() if isinstance(first, datetime) else first, last).astimezone()
        try:
            times = _moved_times(first, last, found) if first is not None else None
        except READ_FAILURES as e:
            return _not_changed(e)
        if isinstance(times, str):
            return {"success": False, "message": times}
        if following and found["recurring_id"]:
            # Another day for every time would need the series' rule changed too - "every Tuesday"
            # to Wednesdays - which is left to Google Calendar.
            if times and _first_day(times[0]) != _day_of(found):
                return {"success": False, "message": "I can only change the time for all of them. Move the days in "
                                                     "Google Calendar."}
            return self._change_following(found, times, new_name, today)
        summary = found["summary"]
        # A time a repeating event is on has an id of its own, so this changes that day only.
        url = _event_url(found["calendar_id"], found["id"])
        try:
            event = found["event"]
            changed, back = {}, {}
            if times:
                changed["start"], changed["end"] = _replacing(times, (event["start"], event["end"]))
                back["start"], back["end"] = _replacing((event["start"], event["end"]), times)
            if new_name:
                changed["summary"], back["summary"] = new_name, event.get("summary")
            self._send_once("PATCH", url, changed)
        except (_NotSure,) + READ_FAILURES as e:
            return _change_failed(e, summary)
        logger.info("Changed an event on Google's calendar")
        was = _when_changed(found, today)
        if times:
            to = _when_changed(_read_event({"start": times[0], "end": times[1]}), today)
        if times and new_name:
            message, undone = f"Moved {summary} to {to} and renamed it {new_name}.", \
                f"Put {summary} back as it was, {was}."
        elif times:
            message, undone = f"Moved {summary} to {to}.", f"Moved {summary} back to {was}."
        else:
            message, undone = f"Renamed {summary} to {new_name}, {was}.", f"Renamed it back to {summary}."
        # One deleted since is gone by the name it has now.
        return {"success": True, "message": message,
                "undo": _undo(new_name or summary, lambda: self._send_once("PATCH", url, back), undone,
                              f"I can't tell whether {summary} was put back.")}

    def _change_following(self, found: Dict, times: Optional[Tuple[Dict, Dict]], new_name: str, today: date) -> Dict:
        """
        Changes a repeating event from one of its times on: the series itself from its first,
        else the series ended just before this one and a new one started from it, with the change -
        as Google Calendar's "this and following events" does, so the times before it stay. A
        series with guests isn't split, as the new one wouldn't have them: that's said, and left.

        Args:
            found: The time, as _to_change gives it.
            times: Its new start and end, on its own day, as Google takes them - or None to leave them.
            new_name: What it's to be called, or "" to leave its name.
            today: Today's date.

        Returns:
            Dict with 'success' and what was changed, or why not, as the 'message' - and once
            it's changed, an 'undo', which puts the series back as it was.
        """
        summary = found["summary"]
        series_url = _event_url(found["calendar_id"], found["recurring_id"])
        on = f"from {spoken_day(_day_of(found), today)} on"
        if times:
            new = _read_event({"start": times[0]})
            at = "all day" if new["all_day"] else spoken_clock_time(new["start"].hour, new["start"].minute).replace(":00", "")
        if times and new_name:
            message = f"Moved {summary} to {at} {on} and renamed it {new_name}."
        elif times:
            message = f"Moved {summary} to {at} {on}."
        else:
            message = f"Renamed {summary} to {new_name} {on}."
        try:
            series = self._get(series_url, {})
            rules = series.get("recurrence")
            if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
                raise _SeriesUnreadable("The series has no rules")
            # By when this one was meant to be, which the series' rules make it - not where it's
            # been moved to since.
            this = _google_when(found["event"].get("originalStartTime") or found["event"]["start"])
            day = this.astimezone().date() if isinstance(this, datetime) else this
            # The new times - or for a new name alone, the series' own - on the day this one was
            # meant to be, in the zone the series repeats in, which Google needs of a repeating event.
            moved = _in_zone(_on_day(times or (series["start"], series["end"]), day),
                             (series["start"] or {}).get("timeZone"))
            if this == _google_when(series["start"]):
                changed, back = {}, {}
                if times:
                    changed["start"], changed["end"] = _replacing(moved, (series["start"], series["end"]))
                    back["start"], back["end"] = _replacing((series["start"], series["end"]), moved)
                if new_name:
                    changed["summary"], back["summary"] = new_name, series.get("summary")
                self._send_once("PATCH", series_url, changed)
                logger.info("Changed a repeating event on Google's calendar")
                return {"success": True, "message": message,
                        "undo": _undo(new_name or summary, lambda: self._send_once("PATCH", series_url, back),
                                      f"Put {summary} back as it was.", f"I can't tell whether {summary} was put back.")}
            # The new series is a copy with none of the guests (see COPIED), so cutting the old one would
            # take their future times away - and the Meet link they join by. Left to Google Calendar, which
            # splits a series with its guests and asks them.
            attendees = series.get("attendees")
            if isinstance(attendees, list) and any(not (isinstance(each, dict) and each.get("self") is True)
                                                   for each in attendees):
                return {"success": False, "message": HAS_GUESTS}
            # Read before anything is written: Google forgets the times deleted on their own once the old
            # series ends before them, and the new one starts without them, so each is left out of it.
            cancelled = self._cancelled_from(series_url, this)
            # A COUNT counts from the series' first time, so the new series' is what was left of it.
            counted = any(re.search(r"[:;]COUNT=", rule, re.IGNORECASE) for rule in rules
                          if rule.upper().startswith("RRULE:"))
            new_series = {**_copy(series), "start": moved[0], "end": moved[1],
                          "recurrence": _rules_left(rules, self._times_before(series_url, this) if counted else 0)
                          + _exdates(cancelled, moved[0])}
            if new_name:
                new_series["summary"] = new_name
            self._send_once("PATCH", series_url, {"recurrence": _cut_rules(rules, this)})
        except _NotSure as e:
            # Sent, but not said to be done - and never sent again, so it's checked by the user.
            logger.warning(f"Couldn't tell whether Google changed a repeating event: {e}")
            return {"success": False, "message": f"I can't tell whether {summary} was changed, so check Google "
                                                 "Calendar."}
        except _SeriesUnreadable:
            # Raised before anything is written, so it's still as it was.
            logger.error("Couldn't read a repeating event's rules, so it's left as it was")
            return {"success": False, "message": RULES_UNREADABLE}
        except READ_FAILURES as e:
            return _change_failed(e, summary)
        # The series is cut short now, so whatever happens to the new one, it's never left that
        # way unsaid: put back as it was if the new one wasn't added, or may have been.
        try:
            new_id = self._insert(found["calendar_id"], new_series)
        except _NotSure as e:
            logger.warning(f"Couldn't tell whether Google added a repeating event's new series: {e}")
            if self._rules_back(series_url, rules):
                # The user is told to check Google Calendar anyway, so which of them came back isn't said.
                self._delete_again(found["calendar_id"], cancelled, today)
            return {"success": False, "message": f"I can't tell whether the new {summary} was added, so check Google "
                                                 "Calendar."}
        except READ_FAILURES as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.error(f"Couldn't add a repeating event's new series: {type(e).__name__}{f' {status}' if status else ''}")
            if self._rules_back(series_url, rules):
                came_back = self._delete_again(found["calendar_id"], cancelled, today)
                if came_back:
                    return {"success": False, "message": f"I couldn't change them, and I've put {summary} back, but "
                                                         f"{came_back}."}
                return {"success": False, "message": f"I couldn't change them, so I've left {summary} as it was."}
            return {"success": False, "message": f"I couldn't change them, and I can't be sure {summary} is as it was, "
                                                 "so check Google Calendar."}
        logger.info("Changed a repeating event on Google's calendar from one of its times on")
        new_url = _event_url(found["calendar_id"], new_id)

        def put_back():
            try:
                self._send_once("DELETE", new_url)
            except requests.HTTPError as e:
                # Deleted on the phone since - the old series is still to be put back.
                if not _gone(e):
                    raise
            try:
                self._send_once("PATCH", series_url, {"recurrence": rules})
            except READ_FAILURES as e:
                if _gone(e):
                    raise
                # The new series is off, but the old one is still cut short.
                raise _NotSure("Google wouldn't put it back", f"the old series not put back: {type(e).__name__}") \
                    from e
            return self._delete_again(found["calendar_id"], cancelled, today)

        return {"success": True, "message": message,
                "undo": _undo(summary, put_back, f"Put {summary} back as it was.",
                              f"I can't be sure {summary} is back as it was, so check Google Calendar.")}

    def _times_before(self, series_url: str, this: Union[date, datetime]) -> int:
        """
        How many times of a repeating event were meant to be before one of them - for its COUNT,
        which counts every time its rule makes, so one deleted on its own counts too, and each
        by when it was meant to be, wherever it's been moved since.

        Args:
            series_url: The series' address.
            this: When the time was meant to be, as the series' start is given.

        Returns:
            How many.

        Raises:
            The same as _pages, and KeyError, ValueError or TypeError for a time that can't be read.
        """
        until = _sort_key(this)
        listed = self._pages(f"{series_url}/instances",
                             {"timeMax": until.isoformat(), "showDeleted": "true", "maxResults": EVENTS_PAGE})
        return sum(1 for each in listed if _sort_key(_google_when(each.get("originalStartTime") or each["start"])) < until)

    def _cancelled_from(self, series_url: str, this: Union[date, datetime]) -> List[Tuple[str, Union[date, datetime]]]:
        """
        The times of a repeating event deleted on their own, from one of its times on, for as
        far as CANCELLED_LOOKED_FOR - for a cut of its series at that time, after which Google
        forgets them, so that putting the series back doesn't bring them back deleted. Google
        keeps each as a cancelled instance of the series, by an id of its own.

        Args:
            series_url: The series' address.
            this: When the time the series is cut at was meant to be, as the series' start is given.

        Returns:
            The id of each, and when it was meant to be, as the series' start is given.

        Raises:
            The same as _pages, and KeyError, ValueError or TypeError for a time that can't be read.
        """
        since = _sort_key(this)
        listed = self._pages(f"{series_url}/instances",
                             {"timeMin": since.isoformat(), "timeMax": (since + CANCELLED_LOOKED_FOR).isoformat(),
                              "showDeleted": "true", "maxResults": EVENTS_PAGE})
        cancelled = []
        for each in listed:
            if each.get("status") != "cancelled":
                continue
            meant = _google_when(each.get("originalStartTime") or each["start"])
            # Google's timeMin is a bound on when an event ends, so one meant just before, which is
            # still going on at this one, could be listed.
            if _sort_key(meant) >= since:
                if not isinstance(each["id"], str):
                    raise ValueError("A time's id isn't text")
                cancelled.append((each["id"], meant))
        return cancelled

    def _delete_again(self, calendar_id: str, cancelled: List[Tuple[str, Union[date, datetime]]],
                      today: date) -> Optional[str]:
        """
        Deletes each time of a repeating event once more, once its series is back - those
        deleted on their own before it was cut, which Google doesn't keep deleted. Each is
        sent once, and one that can't be doesn't stop the rest.

        Args:
            calendar_id: The series' calendar.
            cancelled: Each one's id and when it was meant to be, as _cancelled_from gives them.
            today: Today's date.

        Returns:
            None if each is deleted - or was gone already - else what to say of those which
            came back, for after "but": "Tuesday's time came back - remove it in Google Calendar".
        """
        back = []
        for instance_id, meant in cancelled:
            try:
                self._send_once("DELETE", _event_url(calendar_id, instance_id))
            except _NotSure as e:
                logger.warning(f"Couldn't tell whether Google deleted a time of a repeating event again: {e}")
                back.append(meant)
            except READ_FAILURES as e:
                # One gone already is as it's wanted.
                if not _gone(e):
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    logger.error(f"Couldn't delete a time of a repeating event again: "
                                 f"{type(e).__name__}{f' {status}' if status else ''}")
                    back.append(meant)
        if not back:
            return None
        return _still_back([meant.astimezone().date() if isinstance(meant, datetime) else meant
                            for meant in back], today)

    def _rules_back(self, series_url: str, rules: List[str]) -> bool:
        """
        Puts a repeating event's rules back as they were, once - after its series was cut short
        for a new one that wasn't added, or may not have been.

        Returns:
            Whether Google said it was done.
        """
        try:
            self._send_once("PATCH", series_url, {"recurrence": rules})
        except (_NotSure,) + READ_FAILURES as e:
            logger.error(f"Couldn't put a repeating event's rules back: {type(e).__name__}")
            return False
        return True

    def _to_change(self, search: str, day: Optional[date], at: Optional[str], following: bool, today: date,
                   now: datetime, calendar: Optional[str] = None) -> Union[Dict, str]:
        """
        The one event a change is for: matched by its words, as find matches them, on the day
        given or from today over SEARCH_DAYS - then at the time given, and on the calendar
        named, if they were.

        A time a repeating event was on that has started is never one: earlier times are the
        record of past shifts and lectures, so they stay as they were. One moved since has started
        when the time it was meant to be has, too - see _started. A one-off is changed whenever it
        was.

        Args:
            search: A word or two from its name, e.g. "dentist".
            day: The day it's on, or None.
            at: The time it starts, "HH:MM", or None.
            following: Whether it's from this one on - then of several times of one series,
                it's the earliest.
            today: Today's date.
            now: The time now.
            calendar: The calendar it's on, as the user named it, or None.

        Returns:
            The event, as events gives it with whole - or what to say when there isn't the
            one, or it's not Nova's to change.
        """
        words = [word for word in (each.strip(".,!?'\"") for each in (search or "").split()) if word]
        if not words:
            return "I didn't catch which event you meant."
        clock = None
        if at:
            match = AT_GIVEN.fullmatch(at.strip()) if isinstance(at, str) else None
            clock = (int(match.group(1)), int(match.group(2))) if match else None
            if clock is None or clock[0] > 23 or clock[1] > 59:
                return "I didn't catch the time."
        # "Every week from last Tuesday" would change the times since then, which have already been.
        if following and day is not None and day < today:
            return DAY_GONE
        first, days = (day, 1) if day else (today, SEARCH_DAYS)
        try:
            if isinstance(calendar, str) and calendar.strip():
                named = self._calendars_named(calendar, self.calendars())
                if not named:
                    return NO_SUCH_CALENDAR
                # A calendar named is read for itself, even one out of what's on - as an activity is.
                events = self.events(first, days, calendar_ids=[each["id"] for each in named], whole=True)
            else:
                events = self.events(first, days, leave_out=self.left_out(), whole=True)
        except READ_FAILURES as e:
            return _failed(e)["message"]
        patterns = [_called([word]) for word in words]
        found = [event for event in events if all(pattern.search(event["summary"]) for pattern in patterns)]
        if clock is not None:
            # "The 3 o'clock one" is at 3 exactly, else any time in that hour - and never all day.
            timed = [event for event in found if not event["all_day"]]
            found = [event for event in timed if (event["start"].hour, event["start"].minute) == clock] or \
                [event for event in timed if event["start"].hour == clock[0]]
        to_come = [event for event in found if not (event["recurring_id"] and _started(event, now))]
        if found and not to_come:
            return DAY_GONE
        found = to_come
        series = {(event["calendar_id"], event["recurring_id"]) for event in found}
        if following and len(series) == 1 and found[0]["recurring_id"]:
            found = found[:1]
        if not found:
            return f"I can't find the {' '.join(words)} on your calendar{f' {_on(day, today)}' if day else ''}."
        if len(found) > 1:
            # Named by their calendars too when they're on more than one - a lecture at 11 on
            # Uni and on the user's own would otherwise be said twice the same.
            several = len({event["calendar_id"] for event in found}) > 1
            said = [_found_said(event, today) + (f", on {event['calendar']}" if several and event["calendar"] else "")
                    for event in found[:MOST_FOUND]]
            if len(found) > MOST_FOUND:
                said.append(f"{len(found) - MOST_FOUND} more")
            return f"I found {len(found)}: {'; '.join(said[:-1])}; and {said[-1]}. Which one?"
        event = found[0]
        if not event["writable"]:
            return "I can't change that, Google only lets me change your own calendars."
        # Changing it would tell whoever sent it.
        if event["invite"]:
            return "That's an invite from someone else. Change it in Google Calendar."
        return event

    def _calendar_to_add_to(self, named: Optional[str]) -> Union[str, Tuple[Dict, Dict]]:
        """
        The calendar to add to, and the one new events go to.

        Args:
            named: The calendar the user named, or None.

        Returns:
            (the calendar, the one new events go to), or what to say when it can't be added to.

        Raises:
            The same as calendars.
        """
        calendars = self.calendars()
        default = self.new_events_calendar(calendars) or {"id": "primary", "name": "", "primary": True, "writable": True}
        if not isinstance(named, str) or not named.strip():
            return default, default
        matches = self._calendars_named(named, calendars)
        if not matches:
            return NO_SUCH_CALENDAR
        writable = [each for each in matches if each["writable"]]
        if not writable:
            return f"I can't add to {spoken_name(matches[0])} - Google only lets me add to your own calendars."
        return writable[0], default

    def _calendars_named(self, named: str, calendars: List[Dict]) -> List[Dict]:
        """
        The calendars the user named, to add to or change an event on: by a calendar's own
        name - "my work calendar" is Work - else an activity's word - "shifts" is Work too -
        else, for "my calendar", the user's own.

        Args:
            named: The calendar as the user named it.
            calendars: Google's list of the calendars, as calendars gives it.

        Returns:
            The calendars it names - none for a name no calendar has.
        """
        said = _as_said(named)
        # "My work calendar" is Work, and "my calendar" the user's own.
        bare = re.sub(r"(?:^| )calendar$", "", re.sub(r"^(?:my|the) ", "", said)).strip()
        # A name of no words - emoji alone - is never the one named, or "my calendar", which
        # is "" here, would be it.
        matches = [each for each in calendars if (tidied := _as_said(each["name"])) and tidied in (said, bare)]
        words = self.activity_words()
        word = words.get(said) or words.get(bare)
        if not matches and word is not None:
            matches = [self.activity_calendars()[word]]
        if not matches and bare in OWN_CALENDAR:
            matches = [next((each for each in calendars if each["primary"]), None) or
                       {"id": "primary", "name": "", "primary": True, "writable": True}]
        return matches

    def new_events_calendar(self, calendars: List[Dict]) -> Optional[Dict]:
        """
        The calendar new events go to: the one chosen while Nova can still add to it, else the primary.

        Args:
            calendars: Google's list of the calendars, as calendars gives it.

        Returns:
            The calendar, or None if the list hasn't got the primary.
        """
        chosen_id = self.choices.get("new_events")
        return next((each for each in calendars if each["id"] == chosen_id and each["writable"]), None) or \
            next((each for each in calendars if each["primary"]), None)

    def _insert(self, calendar_id: str, event: Dict) -> str:
        """
        Asks Google to add an event, once - see _send_once.

        Args:
            calendar_id: The calendar to add it to.
            event: The event, as Google's events.insert takes it.

        Returns:
            The new event's id.

        Raises:
            The same as _send_once, and _NotSure if Google's answer didn't give the event added.
        """
        answer = self._send_once("POST", f"{CALENDARS}/{quote(calendar_id, safe='')}/events", event)
        if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"]:
            raise _NotSure("Google's answer didn't say", "an answer without an event")
        return answer["id"]

    def _send_once(self, method: str, url: str, body: Optional[Dict] = None) -> Optional[Dict]:
        """
        Sends one write to Google - an event added, changed or deleted - once. It's the only
        way Nova writes to Google, so every write tells nobody (sendUpdates=none - no guest is
        emailed about it) and none is sent twice. Only a 401 is sent again, with a fresh access
        token: Google turned it down unread, so changed nothing. Once Google has said it was
        done, after_change is called - and for that alone, not for a refusal, an error or an
        answer which doesn't say.

        Args:
            method: "POST", "PATCH" or "DELETE".
            url: The address.
            body: What's sent, as JSON, or None.

        Returns:
            Google's answer, or None for a 204 - a delete's.

        Raises:
            _NotSure: If it was sent but Google's answer didn't say it was done - Google's
                own trouble, a 5xx, too.
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused it - an
                HTTPError of a 404 or 410 for an event gone (see EVENT_GONE).
        """
        if not network.online():
            raise requests.ConnectionError("No network connection")
        for _ in range(2):
            headers = {"Authorization": f"Bearer {self.sign_in.access_token()}"}
            try:
                response = self._session.request(method, url, params={"sendUpdates": "none"}, json=body,
                                                 headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.ConnectTimeout:
                # Never sent, so certainly not done.
                raise
            except requests.Timeout as e:
                # Sent, but unanswered - so it may well have been done.
                raise _NotSure(NO_ANSWER, "no answer in time") from e
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as e:
                if isinstance(e, requests.ConnectionError) and _never_connected(e):
                    raise
                # Cut off after it may have been sent - a reset, or an answer broken off.
                raise _NotSure(LOST, type(e).__name__) from e
            if response.status_code != 401:
                break
            self.sign_in.drop_access_token()
        if response.status_code == 401:
            raise SignInNeeded("Google turned Nova's access token down.")
        # Google's own trouble may come after it was done - a 502 or 504 is a front end
        # that got no answer from behind it, and a 500 backendError is one Google says to
        # retry - so asking again could do it twice. Reading, it's only out of reach.
        if response.status_code >= 500:
            raise _NotSure(TROUBLE, f"a {response.status_code}")
        response.raise_for_status()
        if response.status_code == 204:
            self._tell_of_change()
            return None
        try:
            answer = response.json()
        except ValueError:
            answer = None
        # Anything but Google's own answer - a page from a Wi-Fi sign-in, say - doesn't say it was done.
        if not isinstance(answer, dict):
            raise _NotSure("Google's answer didn't say", f"a {response.status_code} without an answer")
        self._tell_of_change()
        return answer

    def _tell_of_change(self):
        """Calls after_change, if there is one, on a daemon thread - so the reply never waits on it."""
        after_change = self.after_change
        if after_change is None:
            return

        def call():
            # Whatever it does, the event is already on Google's calendar, so a failure is only logged -
            # and as the kind of error alone, as an error's words could name the event.
            try:
                after_change()
            except Exception as e:
                logger.warning(f"Couldn't tell the rest of Nova the calendar changed: {type(e).__name__}")

        threading.Thread(target=call, daemon=True, name="calendar-changed").start()


class _NotSure(Exception):
    """A write was sent to Google, but whether it was done can't be told."""

    def __init__(self, said: str, why: str):
        """
        Args:
            said: How it's said, before "so I can't tell whether ... was added".
            why: Why, for the log - without the event, or its calendar's id.
        """
        super().__init__(why)
        self.said = said


class _SeriesUnreadable(ValueError):
    """A repeating event's rules can't be used: Google gave none, or none of the times the new series needs."""


# What reading the calendar can fail with: no sign-in, Google out of reach or refusing,
# or an answer of the wrong shape.
READ_FAILURES = (SignInNeeded, requests.RequestException, ValueError, KeyError, TypeError)


def _not_added(error: Exception) -> Dict:
    """
    What's said when an event certainly wasn't added, depending on why. As with _failed,
    only the kind of failure is logged.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    logger.error(f"Couldn't add to the calendar: {type(error).__name__}{f' {status}' if status else ''}")
    if isinstance(error, SignInNeeded):
        return {"success": False, "message": NEEDS_SIGN_IN}
    if _turned_down(error):
        return {"success": False, "message": "Google wouldn't add that."}
    return {"success": False, "message": CANT_REACH_GOOGLE}


def _not_changed(error: Exception) -> Dict:
    """
    What's said when an event certainly wasn't changed, removed or put back, depending on
    why - as _not_added has it.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    logger.error(f"Couldn't change the calendar: {type(error).__name__}{f' {status}' if status else ''}")
    if isinstance(error, SignInNeeded):
        return {"success": False, "message": NEEDS_SIGN_IN}
    if _turned_down(error):
        return {"success": False, "message": "Google wouldn't change that."}
    return {"success": False, "message": CANT_REACH_GOOGLE}


def _turned_down(error: Exception) -> bool:
    """
    Whether Google refused a write. Google slowing Nova down is Google out of reach, as
    when reading - only a 4xx otherwise is a refusal. Its own trouble, a 5xx, is never
    here: see _send_once.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(error, requests.HTTPError) and status is not None and 400 <= status < 500 and status != 429 \
        and not _rate_limited(error.response)


def _gone(error: Exception) -> bool:
    """Whether Google said the event asked about is gone - see EVENT_GONE."""
    return isinstance(error, requests.HTTPError) and getattr(error.response, "status_code", None) in EVENT_GONE


def _undo(summary: str, write: Callable[[], None], done: str, unclear: str) -> Callable[[], Dict]:
    """
    How to take a change to the calendar back, for "undo that" - sent once, as the change was.

    Args:
        summary: The event's name, e.g. "Dentist".
        write: Takes it back, raising as _send_once does. If it's taken back with something
            left it gives what to say of that after "but", as a str, as _delete_again does -
            anything else it gives is left unread.
        done: What's said once it's taken back, e.g. "Put Dentist back, Thursday at 3 PM.".
        unclear: What's said when Google's answer doesn't say whether it was.

    Returns:
        The undo, which answers with 'success' and a 'message', and never raises: an event
        gone since - deleted on the phone - is said as that, and nothing more is sent.
    """
    def undo() -> Dict:
        try:
            left = write()
        except _NotSure as e:
            logger.warning(f"Couldn't tell whether Google took a change back: {e}")
            return {"success": False, "message": unclear}
        except READ_FAILURES as e:
            if _gone(e):
                logger.info("An event to take a change back on was gone from Google's calendar")
                return {"success": False, "message": f"{summary} was already gone."}
            return _not_changed(e)
        # A series back with a time of it that came back too is said as that - not as all as it was.
        if isinstance(left, str):
            logger.warning("Took a change to Google's calendar back, but some deleted times came back")
            return {"success": False, "message": f"{done.rstrip('.')}, but {left}."}
        logger.info("Took a change to Google's calendar back")
        return {"success": True, "message": done}

    return undo


def _event_url(calendar_id: str, event_id: str) -> str:
    """The address of one event on a calendar - a time a repeating event is on, or its series."""
    return f"{CALENDARS}/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"


def _copy(event: Dict) -> Dict:
    """A copy of an event, to add in its place - see COPIED."""
    return {key: event[key] for key in COPIED if key in event}


def _google_when(when: Dict) -> Union[date, datetime]:
    """A start as Google gives one - {"date"} for all day, else {"dateTime"} - as a date, or a time with its offset."""
    return date.fromisoformat(when["date"]) if "date" in when else datetime.fromisoformat(when["dateTime"])


def _cut_rules(rules: List[str], before: Union[date, datetime]) -> List[str]:
    """
    A repeating event's rules, ended just before one of its times - for "remove the gym from
    Tuesday on", which leaves every time before it as it was.

    Args:
        rules: The series' recurrence, as Google gives it - "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10",
            and any EXDATE and RDATE lines.
        before: The time it ends before, as the series' start is given - a time for a timed
            series, a date for an all-day one.

    Returns:
        The rules, each RRULE ending before it.
    """
    # Each RRULE loses its COUNT and UNTIL and ends just before `before`: a second before it, in UTC,
    # for a timed series ("UNTIL=20261124T085959Z"), the day before for an all-day one
    # ("UNTIL=20261123"). EXDATE and RDATE lines are kept as they are.
    if isinstance(before, datetime):
        until = (before.astimezone(timezone.utc) - timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")
    else:
        until = (before - timedelta(days=1)).strftime("%Y%m%d")
    cut = []
    for rule in rules:
        if not rule.upper().startswith("RRULE:"):
            cut.append(rule)
            continue
        parts = [part for part in rule[len("RRULE:"):].split(";")
                 if part.split("=", 1)[0].upper() not in ("COUNT", "UNTIL")]
        cut.append(rule[:len("RRULE:")] + ";".join(parts + [f"UNTIL={until}"]))
    return cut


def _rules_left(rules: List[str], before: int) -> List[str]:
    """
    A repeating event's rules for a new series from one of its times on - for "move the gym to
    7 from Tuesday on", whose new series has only the times that were left.

    Args:
        rules: The series' recurrence, as Google gives it.
        before: How many of its times were meant to be before the one the new series is from.

    Returns:
        The rules, each RRULE's COUNT lowered by those before - "COUNT=10" with 4 before is
        "COUNT=6" - and an UNTIL, and EXDATE and RDATE lines, kept as they are.

    Raises:
        _SeriesUnreadable: If a COUNT can't be read, or has no times left.
    """
    left = []
    for rule in rules:
        if rule.upper().startswith("RRULE:"):
            parts = rule[len("RRULE:"):].split(";")
            for index, part in enumerate(parts):
                name, _, value = part.partition("=")
                if name.upper() == "COUNT":
                    try:
                        count = int(value) - before
                    except ValueError as e:
                        raise _SeriesUnreadable("A COUNT isn't a number") from e
                    if count < 1:
                        raise _SeriesUnreadable("The series has no times left")
                    parts[index] = f"{name}={count}"
            rule = rule[:len("RRULE:")] + ";".join(parts)
        left.append(rule)
    return left


def _exdates(cancelled: List[Tuple[str, Union[date, datetime]]], start: Dict) -> List[str]:
    """
    EXDATE lines leaving times of a repeating event out of a series of its own - for the times
    deleted on their own that a new series from one of its times would otherwise have again.

    Args:
        cancelled: Each one's id and when it was meant to be, as _cancelled_from gives them.
        start: The new series' start, as Google takes it - {"date"} or {"dateTime"}.

    Returns:
        One line each, in the form Google gives a series' own start: for a series all day,
        "EXDATE;VALUE=DATE:20261201", else the day at the series' time of day in UTC -
        "EXDATE:20261201T190000Z" - so a series at 7 PM has it at 6 PM UTC once the clocks go forward.
    """
    lines = []
    for _, meant in cancelled:
        day = meant.astimezone().date() if isinstance(meant, datetime) else meant
        if "date" in start:
            lines.append(f"EXDATE;VALUE=DATE:{day.strftime('%Y%m%d')}")
        else:
            at = datetime.combine(day, _google_when(start).astimezone().time()).astimezone(timezone.utc)
            lines.append(f"EXDATE:{at.strftime('%Y%m%dT%H%M%SZ')}")
    return lines


def _still_back(days: List[date], today: date) -> str:
    """
    What's said of times of a repeating event deleted on their own which came back - after
    "but": "Tuesday's time came back - remove it in Google Calendar".

    Args:
        days: The days they're on.
        today: Today's date.

    Returns:
        The days, as many as MOST_DAYS_SAID and then how many more.
    """
    said = [spoken_day(day, today) for day in sorted(days)]
    if len(said) == 1:
        return f"{said[0]}'s time came back - remove it in Google Calendar"
    named = said[:MOST_DAYS_SAID] + ([f"{len(said) - MOST_DAYS_SAID} more"] if len(said) > MOST_DAYS_SAID else [])
    return f"these times came back: {', '.join(named[:-1])} and {named[-1]} - remove them in Google Calendar"


def _change_failed(error: Exception, summary: str) -> Dict:
    """
    What's said when an event wasn't changed - or may have been, when Google's answer didn't
    say, so it's never sent again - or was gone meanwhile.

    Args:
        error: Why, as _send_once or reading raised it.
        summary: The event's name, e.g. "Dentist".

    Returns:
        Dict with 'success' False and what to say as the 'message'.
    """
    if isinstance(error, _NotSure):
        logger.warning(f"Couldn't tell whether Google changed an event: {error}")
        return {"success": False, "message": f"I can't tell whether {summary} was changed."}
    if _gone(error):
        logger.info("An event to change was already gone from Google's calendar")
        return {"success": False, "message": f"{summary} was already gone."}
    return _not_changed(error)


def _moved_times(first: Union[date, datetime], last: Union[date, datetime, None],
                 event: Dict) -> Union[str, Tuple[Dict, Dict]]:
    """
    An event's new start and end, for "move the dentist to Friday at 4".

    Args:
        first: When it's to start, as _when_said reads it - a day alone keeps the time one at a
            time is at, and keeps one all day all day.
        last: When it's to end, the same way - an end day is included - or None for the length
            it had: DEFAULT_LENGTH for one all day given a time, and as many days for one all day.
        event: The event, as events gives it with whole.

    Returns:
        (start, end) - each {"dateTime": local RFC 3339} or {"date"} - or what to say when they
        can't be used.

    Raises:
        ValueError: If Google's own end of an event all day can't be read.
    """
    if not isinstance(first, datetime) and event["all_day"]:
        if last is None:
            # Google's end day is the day after - one day's event ends on the next.
            end = (event["event"].get("end") or {}).get("date")
            days = max(date.fromisoformat(end) - event["start"], timedelta(days=1)) if end else timedelta(days=1)
        else:
            last = last.date() if isinstance(last, datetime) else last
            if last < first:
                return "That ends before it starts."
            days = last - first + timedelta(days=1)
        return {"date": first.isoformat()}, {"date": (first + days).isoformat()}
    if not isinstance(first, datetime):
        first = datetime.combine(first, event["start"].time()).astimezone()
    if last is not None and not isinstance(last, datetime):
        return "I didn't catch when that is."
    length = event["end"] - event["start"] if not event["all_day"] and event["end"] else DEFAULT_LENGTH
    # Added on, then put in the brain's zone again, as an event added is.
    last = last or (first + length).astimezone()
    if last <= first:
        return "That ends before it starts."
    return {"dateTime": first.isoformat()}, {"dateTime": last.isoformat()}


def _on_day(times: Tuple[Dict, Dict], day: date) -> Tuple[Dict, Dict]:
    """
    A start and end, as Google gives them, put on another day - at the same time on the clock,
    for as long: a time of a series, as a new series from it starts.
    """
    start, end = times
    if "date" in start:
        return {"date": day.isoformat()}, {"date": (day + (_google_when(end) - _google_when(start))).isoformat()}
    first, last = _google_when(start).astimezone(), _google_when(end)
    moved = datetime.combine(day, first.time()).astimezone()
    return {"dateTime": moved.isoformat()}, {"dateTime": (moved + (last - first)).astimezone().isoformat()}


def _in_zone(times: Tuple[Dict, Dict], zone: Optional[str]) -> Tuple[Dict, Dict]:
    """A repeating event's start and end, each at a time given the zone it repeats in - which Google needs of one."""
    if not zone:
        return times
    return tuple({**each, "timeZone": zone} if "dateTime" in each else each for each in times)


def _replacing(times: Tuple[Dict, Dict], was: Tuple[Dict, Dict]) -> Tuple[Dict, Dict]:
    """
    A start and end to patch over the ones an event has, with the kind they take the place of
    cleared - Google's patch keeps whatever it isn't sent, so one all day given a time is sent
    {"date": None, "dateTime": ...}.
    """
    return tuple({**{key: None for key in ("date", "dateTime") if key in old and key not in new}, **new}
                 for new, old in zip(times, was))


def _never_connected(error: Exception) -> bool:
    """
    Whether a requests ConnectionError is a connection never made - so nothing was sent -
    from the urllib3 errors underneath it: a NewConnectionError, which a name that won't
    resolve is too, or a connect timeout.
    """
    seen = set()
    stack = [error]
    while stack:
        each = stack.pop()
        if each is None or id(each) in seen:
            continue
        seen.add(id(each))
        if isinstance(each, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError)):
            return True
        stack += [getattr(each, "reason", None), each.__cause__, each.__context__]
        stack += [arg for arg in getattr(each, "args", ()) if isinstance(arg, BaseException)]
    return False


def _when_said(when: str) -> Union[date, datetime, None]:
    """
    A time the model gave, as a local datetime, or a date for a day alone - or None if
    it can't be read.
    """
    when = when.strip() if isinstance(when, str) else ""
    if not WHEN_GIVEN.fullmatch(when):
        return None
    try:
        if len(when) == 10:
            return date.fromisoformat(when)
        # The brain's own zone.
        return datetime.fromisoformat(when).astimezone()
    except ValueError:
        return None


def _time_said(when: Optional[str]) -> Union[date, datetime, day_time, None]:
    """
    As _when_said, or a time of day alone - "17:00" - as a time, for the event's own day, which
    is only known once it's found.
    """
    clock = AT_GIVEN.fullmatch(when.strip()) if isinstance(when, str) else None
    if clock is None:
        return _when_said(when)
    hour, minute = int(clock.group(1)), int(clock.group(2))
    return day_time(hour, minute) if hour <= 23 and minute <= 59 else None


def _event_times(start: str, end: Optional[str], all_day: Optional[bool]) -> Union[str, Tuple[Dict, Dict]]:
    """
    An event's start and end, as Google's events.insert takes them.

    Args:
        start: When it starts, local - "YYYY-MM-DD HH:MM", or "YYYY-MM-DD" for all day.
        end: When it ends, the same way, or None.
        all_day: Whether it's all day, even with a time given.

    Returns:
        (start, end) - each {"dateTime": local RFC 3339} or {"date"} - or what to say
        when they can't be used.
    """
    first = _when_said(start)
    last = _when_said(end) if end else None
    if first is None or (end and last is None):
        return "I didn't catch when that is."
    if all_day is True or not isinstance(first, datetime):
        first = first.date() if isinstance(first, datetime) else first
        last = (last.date() if isinstance(last, datetime) else last) if last else first
        if last < first:
            return "That ends before it starts."
        # Google's end day is the day after, so the end day given is included.
        return {"date": first.isoformat()}, {"date": (last + timedelta(days=1)).isoformat()}
    if last is not None and not isinstance(last, datetime):
        return "I didn't catch when that is."
    # Added on, then put in the brain's zone again, so an hour over the clocks going back is
    # still an hour, at its own offset.
    last = last or (first + DEFAULT_LENGTH).astimezone()
    # Nor at the moment it starts - an event of no length is a mishearing too.
    if last <= first:
        return "That ends before it starts."
    return {"dateTime": first.isoformat()}, {"dateTime": last.isoformat()}


def _first_day(start: Dict) -> date:
    """The day an event to add starts on, from its start as events.insert takes it."""
    return date.fromisoformat(start["date"]) if "date" in start else datetime.fromisoformat(start["dateTime"]).date()


def _added_when(start: Dict, today: date) -> str:
    """
    When an event added starts, as it's said back - the weekday and the day of the month
    always, so a mistake is heard: "Thursday the 29th at 3 PM", "Monday the 2nd of
    November, all day", and the year once it's not within the coming twelve months.
    """
    day = _first_day(start)
    if "date" in start:
        at = ", all day"
    else:
        when = datetime.fromisoformat(start["dateTime"])
        at = f" at {spoken_clock_time(when.hour, when.minute).replace(':00', '')}"
    # A year on is the same day of the month - the 28th of February, from a 29th.
    year_on = today.replace(year=today.year + 1, day=28) if (today.month, today.day) == (2, 29) else \
        today.replace(year=today.year + 1)
    if day >= year_on:
        month = f" of {day.strftime('%B')} {day.year}"
    elif (day.year, day.month) != (today.year, today.month):
        month = f" of {day.strftime('%B')}"
    else:
        month = ""
    return f"{day.strftime('%A')} the {_ordinal(day.day)}{month}{at}"


def named_by_address(calendar: Dict) -> bool:
    """
    Whether a calendar is named by an email address - as Google names the user's own, and
    a person's own shared with them, by theirs - which is never said, or told to Claude.
    """
    return ADDRESS.search(calendar["name"]) is not None


def spoken_name(calendar: Dict) -> str:
    """
    A calendar's name as it's said, and told to Claude: its own, or for one named by an
    email address, YOUR_CALENDAR or A_SHARED_CALENDAR.

    Args:
        calendar: A calendar, as GoogleCalendar.calendars gives it.

    Returns:
        e.g. "Work", "your calendar" or "a shared calendar".
    """
    if not named_by_address(calendar):
        return calendar["name"]
    return YOUR_CALENDAR if calendar["primary"] else A_SHARED_CALENDAR


def _rate_limited(response) -> bool:
    """Whether Google's error answer says it's slowing Nova down, rather than refusing it."""
    try:
        return any(error.get("reason") in RATE_LIMITED for error in response.json()["error"]["errors"])
    except (AttributeError, ValueError, KeyError, TypeError):
        return False


def _failed(error: Exception) -> Dict:
    """
    What's said when the calendar couldn't be read, depending on why. Only the kind of
    failure is logged: a requests error names the address, with a calendar's id in it -
    for the primary, the user's email address.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    logger.error(f"Couldn't read the calendar: {type(error).__name__}{f' {status}' if status else ''}")
    if isinstance(error, SignInNeeded):
        return {"success": False, "message": NEEDS_SIGN_IN}
    return {"success": False, "message": CANT_REACH_GOOGLE}


def _as_said(words: str) -> str:
    """Words as a request is matched by intents.py: lower case, letters, digits and apostrophes only."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", words.lower())).strip()


def _called(words: List[str]) -> re.Pattern:
    """
    Finds an event's name with a word in it starting with one of these - so "CSC"
    finds "CSC203 Lecture" and "shift" finds "Night shift", but neither turns up
    inside some other word.
    """
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + ")", re.IGNORECASE)


def _as_activities(events: List[Dict], activities: Dict[str, Dict], first: date) -> List[Dict]:
    """
    The user's activities - work, uni - as one stretch of time each, named after the
    activity. A day of uni was read out session by session, by each one's own name -
    "CSC2035/P01/01 <04-08> at 11:30 AM" - which took 25 seconds and meant little.
    Now it's "uni from 9 AM until 1 PM". Everything else is left as it is - an
    activity calendar's all-day events too, as an activity is a stretch of time: a
    training day on the Work calendar is "Training day, all day", not "work, all day".

    Args:
        events: From GoogleCalendar.events, earliest first - each with its "calendar_id".
        activities: The activity calendars, by each activity's word - see
            GoogleCalendar.activity_calendars. Every timed event on one is that activity.
        first: The first day asked about.

    Returns:
        The events, earliest first, with each activity once a day. Its stretches of
        time that day are in "spans", to be said with their ends as well as their
        starts.
    """
    # By id, as activity() reads them - not the name, which the user's own may share.
    by_id = {calendar["id"]: word for word, calendar in activities.items()}
    kept, theirs = [], {word: [] for word in activities}
    for event in events:
        word = by_id.get(event.get("calendar_id"))
        (theirs[word] if word is not None and not event["all_day"] else kept).append(event)
    for name, its_events in theirs.items():
        for day, on_day in _by_day(its_events, first).items():
            spans = _stretches(on_day)
            if spans:
                kept.append({"summary": name, "start": spans[0][0], "end": None, "all_day": False, "spans": spans,
                             "activity": True})
    return sorted(kept, key=lambda event: _sort_key(event["start"]))


def _by_day(events: List[Dict], first: date) -> Dict[date, List[Dict]]:
    """Events grouped by the day they start on - or the first day, for one already going."""
    by_day: Dict[date, List[Dict]] = {}
    for event in events:
        day = event["start"] if event["all_day"] else event["start"].date()
        by_day.setdefault(max(day, first), []).append(event)
    return by_day


def _stretches(events: List[Dict]) -> List[List]:
    """
    A day's timed events as stretches of time. Back-to-back sessions - uni's 2:30
    until 3:30, then 3:30 until 5:30 - are one stretch, 2:30 until 5:30, rather than
    read out one by one.
    """
    stretches = []
    for event in sorted((event for event in events if not event["all_day"]), key=lambda e: e["start"]):
        end = event.get("end") or event["start"]
        if stretches and event["start"] <= stretches[-1][1]:
            stretches[-1][1] = max(stretches[-1][1], end)
        else:
            stretches.append([event["start"], end])
    return stretches


def _span(start: datetime, end: datetime) -> str:
    """'from 2:30 PM until 5:30 PM', or 'at 2:30 PM' for something with no length."""
    said = spoken_clock_time(start.hour, start.minute)
    return f"from {said} until {spoken_clock_time(end.hour, end.minute)}" if end > start else f"at {said}"


def describe_activity(name: str, events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says when the user has an activity, a day at a time.

    Args:
        name: The activity, e.g. "work".
        events: Its events, from GoogleCalendar.events, earliest first.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "Tomorrow, you've got work from 9:00 AM until 5:30 PM." or "You haven't
        got uni tomorrow."
    """
    by_day = _by_day(events, first)
    sentences = []
    for day in sorted(by_day):
        times = ["all day"] if any(event["all_day"] for event in by_day[day]) else []
        times += [_span(start, end) for start, end in _stretches(by_day[day])]
        listed = times[0] if len(times) == 1 else ", ".join(times[:-1]) + ", and " + times[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {name} {listed}.")

    if sentences:
        return " ".join(sentences)
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You haven't got {name} {spoken_day(first, today)}."
    return f"You haven't got {name} between {spoken_day(first, today)} and {spoken_day(last, today)}."


def describe_next(name: str, events: List[Dict], count: int, first: date, now: datetime) -> str:
    """
    Says when the user next has an activity - once, or the next few times.

    Asked "when do I next have work?", every shift for the next fortnight used to be
    read out, when only the next one was wanted.

    Args:
        name: The activity, e.g. "work".
        events: Its events from the first day on, earliest first.
        count: How many times to say.
        first: The first day looked at - usually today.
        now: The time now, so one already over today isn't counted.

    Returns:
        e.g. "You're next at work tomorrow, from 3:30 PM until 11:30 PM." or "Your
        next 2 times at work: tomorrow, from ...; and on Saturday, from ...".
    """
    today = now.date()
    upcoming = []
    by_day = _by_day(events, first)
    for day in sorted(by_day):
        if any(event["all_day"] for event in by_day[day]) and day >= today:
            upcoming.append((day, "all day", None, None))
        upcoming += [(day, _span(start, end), start, end) for start, end in _stretches(by_day[day]) if end > now]
    upcoming = upcoming[:count]

    if not upcoming:
        return f"You haven't got {name} in the next two weeks."
    if len(upcoming) == 1:
        day, span, start, end = upcoming[0]
        if start is not None and start <= now:
            return f"You're at {name} now, until {spoken_clock_time(end.hour, end.minute)}."
        return f"You're next at {name} {_on(day, today)}, {span}."
    said = [f"{_on(day, today)}, {span}" for day, span, _, _ in upcoming]
    return f"Your next {len(upcoming)} times at {name}: {'; '.join(said[:-1])}; and {said[-1]}."


def _read_event(event: Dict) -> Dict:
    """One event from Google, with its start and end read into dates or local times."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" in start:
        when, all_day = datetime.fromisoformat(start["dateTime"]).astimezone(), False
        until = datetime.fromisoformat(end["dateTime"]).astimezone() if "dateTime" in end else None
    else:
        when, all_day, until = date.fromisoformat(start["date"]), True, None
    # Google's is always a string, but one that isn't mustn't raise out of the agenda.
    summary = event.get("summary") if isinstance(event.get("summary"), str) else None
    return {"summary": (summary or "something").strip(), "start": when, "end": until, "all_day": all_day}


def _sort_key(when) -> datetime:
    """Sorts all-day events before anything at a time on the same day."""
    return when if isinstance(when, datetime) else datetime.combine(when, day_time()).astimezone()


def spoken_day(day: date, today: date) -> str:
    """
    A day as it's said: "today", "tomorrow", "Thursday" within the week, or
    "Thursday the 30th of October" further off.
    """
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if today < day < today + timedelta(days=7):
        return day.strftime("%A")
    return f"{day.strftime('%A')} the {_ordinal(day.day)} of {day.strftime('%B')}"


def _ordinal(number: int) -> str:
    """A day of the month as it's said, e.g. "2nd" or "11th"."""
    suffix = "th" if 11 <= number % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _on(day: date, today: date) -> str:
    """'today' and 'tomorrow' stand alone; any other day is 'on Thursday'."""
    said = spoken_day(day, today)
    return said if said in ("today", "tomorrow") else f"on {said}"


def describe_agenda(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says what's on - a day at a time for a day or two, or as an overview for more.

    Args:
        events: From GoogleCalendar.events, earliest first - with the activities
            joined up, see _as_activities.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "On Thursday, you've got the dentist at 9:30 AM, and dinner with Sam at
        7:00 PM.", "Over the next week, you've got uni today and Thursday. There's
        also the dentist on Friday." or "You've got nothing on tomorrow."
    """
    if not events:
        return _nothing_on(first, days, today)
    if days >= OVERVIEW_FROM_DAYS:
        return _overview(events, first, days, today)
    return _day_by_day(events, first, days, today)


def _nothing_on(first: date, days: int, today: date) -> str:
    """What's said when there's nothing on - "You've got nothing on tomorrow", say."""
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You've got nothing on {spoken_day(first, today)}."
    if days == 2:
        return f"You've got nothing on {spoken_day(first, today)} or {spoken_day(last, today)}."
    return f"You've got nothing on from {spoken_day(first, today)} to {spoken_day(last, today)}."


def _day_by_day(events: List[Dict], first: date, days: int, today: date) -> str:
    """What's on, each day in full - see describe_agenda."""
    by_day = {first + timedelta(days=offset): [] for offset in range(days)}
    for event in events:
        when = event["start"]
        day = when if event["all_day"] else when.date()
        # An event which started before the days asked about, and runs into them.
        by_day.setdefault(max(day, first), []).append(event)

    sentences = []
    for day, on_day in by_day.items():
        if not on_day:
            continue
        shown = on_day[:MOST_EVENTS_SAID]
        said = [_event_said(event) for event in shown]
        if len(on_day) > len(shown):
            said.append(f"{len(on_day) - len(shown)} more")
        listed = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {listed}.")

    return " ".join(sentences) if sentences else _nothing_on(first, days, today)


def _overview(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    What's on over several days, without the times - see describe_agenda. Each
    activity is said with the days it's on - by name within a week, or as a count
    over longer - and then the other things, each with its day.
    """
    last = first + timedelta(days=days - 1)
    if first == today:
        over = {7: "Over the next week", 14: "Over the next two weeks"}.get(days, f"Over the next {days} days")
    else:
        over = f"From {spoken_day(first, today)} to {spoken_day(last, today)}"

    activity_days: Dict[str, List[date]] = {}
    others = []
    for event in events:
        day = max(event["start"] if event["all_day"] else event["start"].date(), first)
        if event.get("activity"):
            if day not in activity_days.setdefault(event["summary"], []):
                activity_days[event["summary"]].append(day)
        else:
            others.append((event, day))

    activities = []
    for name, on in activity_days.items():
        if len(on) == days:
            activities.append(f"{name} every day")
        elif days > 7:
            activities.append(f"{name} on {len(on)} day{'s' if len(on) != 1 else ''}")
        else:
            # Within a week a day's name is enough, even one further off than
            # spoken_day would name alone - the opening has already said which week.
            names = [spoken_day(day, today) if day <= today + timedelta(days=1) else day.strftime("%A")
                     for day in sorted(on)]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            activities.append(f"{name} {'' if names[0] in ('today', 'tomorrow') else 'on '}{listed}")

    named = [f"{event['summary']} {_on(day, today)}" for event, day in others[:MOST_OTHERS_IN_OVERVIEW]]
    if len(others) > len(named):
        named.append(f"{len(others) - len(named)} more")

    def joined(items: List[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]

    if not activities:
        return f"{over}, you've got {joined(named)}."
    said = f"{over}, you've got {joined(activities)}."
    return f"{said} There's also {joined(named)}." if named else said


def _event_said(event: Dict) -> str:
    """One event, as it's said: "the dentist at 9:30 AM", "uni from 9 AM until 1 PM", "Sam's birthday, all day"."""
    if event["all_day"]:
        return f"{event['summary']}, all day"
    if event.get("spans"):
        return f"{event['summary']} " + " and ".join(_span(start, end) for start, end in event["spans"])
    return f"{event['summary']} at {spoken_clock_time(event['start'].hour, event['start'].minute)}"


def describe_found(wanted: str, events: List[Dict], today: date) -> str:
    """
    Says when something looked for on the calendar is on.

    Args:
        wanted: What was looked for, e.g. "MOT".
        events: The events found, earliest first.
        today: Today's date.

    Returns:
        e.g. "You've got Car MOT on Thursday the 30th of October, at 9:30 AM." or
        "I can't see MOT on your calendar in the next three months."
    """
    if not events:
        return f"I can't see {wanted} on your calendar in the next three months."
    said = [_found_said(event, today) for event in events[:MOST_FOUND]]
    listed = said[0] if len(said) == 1 else "; ".join(said[:-1]) + "; and " + said[-1]
    more = f" And {len(events) - MOST_FOUND} more after that." if len(events) > MOST_FOUND else ""
    return f"You've got {listed}.{more}"


def _found_said(event: Dict, today: date) -> str:
    """One event found, as it's said: "Car MOT on Thursday, at 9:30 AM", "Holiday tomorrow, all day"."""
    when = "all day" if event["all_day"] else f"at {spoken_clock_time(event['start'].hour, event['start'].minute)}"
    return f"{event['summary']} {_on(_day_of(event), today)}, {when}"


def _day_of(event: Dict) -> date:
    """The day an event read starts on - its own, for one all day."""
    return event["start"] if event["all_day"] else event["start"].date()


def _today_and_now(today: Optional[date], now: Optional[datetime]) -> Tuple[date, datetime]:
    """
    Today and the time now, for a change: the clock's - or as a test gives them, with the
    start of the day for the time when only the day is given.
    """
    if now is None:
        now = datetime.combine(today, day_time()).astimezone() if today else datetime.now().astimezone()
    return today or now.date(), now


def _started(event: Dict, now: datetime) -> bool:
    """
    Whether an event read has started by now - one all day from the start of its day. A time of
    a repeating event moved since has started once either its own time or the one it was meant
    to be has: changing it from that one on would cut its series from when it was meant to be.
    """
    starts = [event["start"]]
    meant = (event.get("event") or {}).get("originalStartTime")
    if meant:
        try:
            starts.append(_google_when(meant))
        except (KeyError, ValueError, TypeError):
            # When it was meant to be can't be read, so it's never changed.
            return True
    return any(start <= now if isinstance(start, datetime) else start <= now.date() for start in starts)


def _when_changed(event: Dict, today: date) -> str:
    """
    When an event changed is on, as it's said back - "Thursday at 3 PM", "tomorrow at 10:30
    AM", "Friday, all day" - with the time as an event added is said.
    """
    if event["all_day"]:
        return f"{spoken_day(event['start'], today)}, all day"
    start = event["start"]
    return f"{spoken_day(start.date(), today)} at {spoken_clock_time(start.hour, start.minute).replace(':00', '')}"
