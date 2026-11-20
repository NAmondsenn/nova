"""
Google Calendar Test
Checks Nova's own Google sign-in - that it asks for only what Nova uses, refuses an
address from another sign-in, keeps the sign-in readable by Nova's user only, and
keeps an access token until it nearly runs out - and the calendar read from Google
with it, and said. A fake stands in for Google, answering as its API reference
gives it, so nothing ever reaches Google. Every id, secret, code and token is made up.
"""

import base64
import hashlib
import inspect
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests
import urllib3

from voice_assistant import google_calendar, storage
from voice_assistant.actions import Action, ActionExecutor
from voice_assistant.choices import ChoiceStore
from voice_assistant.google_calendar import (CANT_REACH_GOOGLE, NEEDS_SIGN_IN, ClientRefused, GoogleCalendar,
                                             GoogleSignIn, GoogleSignInError, SignInNeeded, describe_agenda,
                                             describe_found, named_by_address, spoken_name)
from voice_assistant.intents import LocalIntents

CLIENT_ID = "made-up-client.apps.example.com"
CLIENT_SECRET = "made-up-client-secret"
REFRESH_TOKEN = "made-up-refresh-token"
ACCESS_TOKEN = "made-up-access-token"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.ok = status < 400

    def json(self):
        return self.body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeGoogle:
    """
    Stands in for a requests.Session, answering each address from a list of answers
    and noting every request - an answer can be an exception, to be raised instead,
    and an answer that isn't a FakeResponse is a 200 with it as the body. A write is
    answered by its "METHOD address" - "DELETE https://..." - and a POST by its
    address alone too.
    """

    # Every FakeGoogle made in a test, for the tells_nobody fixture to check after it.
    _made = []

    def __init__(self, **answers):
        self.answers = {address: list(each) for address, each in answers.items()}
        self.asked = []
        # What each GET was sent with besides its address - its params, headers and timeout.
        self.fetched = []
        # Each POST, as it was sent.
        self.posted = []
        # Each write to Google - an event added, changed or deleted - as {"method", "url", "params", "json"}.
        self.sent = []
        FakeGoogle._made.append(self)

    def post(self, url, data=None, json=None, headers=None, timeout=None, **kwargs):
        # Nova only ever posts for a token - every write to an event goes through request, see
        # test_every_write_to_google_is_sent_once_and_tells_nobody.
        if url != google_calendar.TOKEN:
            raise AssertionError(f"Nothing should be posted to {url}")
        self.asked.append((url, dict(data or json or {})))
        self.posted.append({"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout})
        return self._answer(url)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, **kwargs):
        self.sent.append({"method": method, "url": url, "params": params, "json": json})
        if method == "POST":
            self.asked.append((url, dict(json or {})))
            self.posted.append({"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout})
        address = f"{method} {url}"
        if address not in self.answers and method == "POST":
            address = url
        if address not in self.answers:
            raise AssertionError(f"Nothing should be sent: {method} {url}")
        return self._answer(address)

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        if url not in self.answers:
            raise AssertionError(f"Nothing should be fetched from {url}")
        self.asked.append((url, dict(params or {})))
        self.fetched.append({"params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        return self._answer(url)

    def _answer(self, url):
        answer = self.answers[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)


@pytest.fixture(autouse=True)
def tells_nobody():
    """
    Checks, after every test here, that each write any FakeGoogle was sent told nobody -
    Google's sendUpdates=none - so no guest of an event is ever emailed about a change.
    """
    FakeGoogle._made = []
    yield
    for google in FakeGoogle._made:
        for sent in google.sent:
            assert sent["params"] == {"sendUpdates": "none"}, f"{sent['method']} {sent['url']}"


def _token_answer(access=ACCESS_TOKEN, expires_in=3600, refresh=REFRESH_TOKEN):
    body = {"access_token": access, "expires_in": expires_in, "token_type": "Bearer",
            "scope": " ".join(google_calendar.SCOPES)}
    if refresh:
        body["refresh_token"] = refresh
    return FakeResponse(200, body)


def _query(address):
    return {name: values[0] for name, values in parse_qs(urlsplit(address).query).items()}


def _pasted(state, code="made-up-code"):
    """The address Google's page sends the browser to, as the user pastes it back."""
    fields = {"state": state, "scope": " ".join(google_calendar.SCOPES)}
    if code:
        fields["code"] = code
    return google_calendar.REDIRECT + "?" + urlencode(fields)


@pytest.fixture
def token_file(tmp_path):
    return str(tmp_path / "data" / "google_token.json")


def _signed_in(token_file):
    storage.save_json(token_file, {"refresh_token": REFRESH_TOKEN, "scope": " ".join(google_calendar.SCOPES)})


# Signing in

def test_the_sign_in_asks_for_only_what_nova_uses(token_file):
    address = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle()).start()

    assert address.startswith(google_calendar.AUTHORISE + "?")
    fields = _query(address)
    assert fields["scope"].split(" ") == [
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.events.owned"]
    assert fields["access_type"] == "offline" and fields["prompt"] == "consent"
    assert fields["code_challenge_method"] == "S256" and fields["code_challenge"]
    assert fields["state"]
    assert fields["client_id"] == CLIENT_ID and fields["redirect_uri"] == google_calendar.REDIRECT
    assert fields["response_type"] == "code"
    # The secret is only ever sent to Google's token address, never put in a link.
    assert CLIENT_SECRET not in address


@pytest.mark.parametrize("started", [True, False])
def test_a_sign_in_finished_from_another_address_is_refused(token_file, started):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    if started:
        sign_in.start()

    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted("another-sign-ins-state"))

    assert google.asked == [] and not os.path.exists(token_file)


def test_a_pasted_state_that_isnt_plain_ascii_is_refused_as_another_sign_ins(token_file):
    # Compared as it came, a pasted "state=%C3%A9" raised a TypeError rather than saying so.
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    sign_in.start()

    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted("\u00e9"))

    assert google.asked == [] and not os.path.exists(token_file)


def test_an_address_without_googles_code_is_refused(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(ValueError, match="That address hasn't got Google's code in it."):
        sign_in.finish(_pasted(state, code=None))

    assert google.asked == [] and not os.path.exists(token_file)
    # The state is used once, so the same address can't finish it afterwards either.
    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


def test_finishing_saves_the_sign_in_for_novas_user_only(token_file):
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    fields = _query(sign_in.start())

    sign_in.finish(_pasted(fields["state"]))

    [(url, sent)] = google.asked
    assert url == google_calendar.TOKEN
    assert sent["code"] == "made-up-code" and sent["grant_type"] == "authorization_code"
    assert sent["redirect_uri"] == google_calendar.REDIRECT
    assert sent["client_id"] == CLIENT_ID and sent["client_secret"] == CLIENT_SECRET
    # The verifier sent is the one whose S256 was in the address.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == fields["code_challenge"]

    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    with open(token_file) as file:
        saved = json.load(file)
    assert saved == {"refresh_token": REFRESH_TOKEN, "scope": " ".join(google_calendar.SCOPES)}
    assert sign_in.signed_in()
    # The state and verifier are used once.
    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(fields["state"]))


# Using it

def test_an_access_token_is_kept_until_it_nearly_runs_out(token_file, monkeypatch):
    _signed_in(token_file)
    now = [1000.0]
    monkeypatch.setattr(google_calendar.time, "monotonic", lambda: now[0])
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer("first-access", refresh=None),
                                                    _token_answer("second-access", refresh=None)]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert sign_in.access_token() == "first-access"
    now[0] += 3500
    assert sign_in.access_token() == "first-access"
    assert len(google.asked) == 1
    [(_, sent)] = google.asked
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == REFRESH_TOKEN

    # Within a minute of running out, it's refreshed.
    now[0] += 41
    assert sign_in.access_token() == "second-access"
    assert len(google.asked) == 2


def test_a_revoked_sign_in_says_it_needs_doing_again(token_file):
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
        requests.ConnectionError("no network")]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()
    with pytest.raises(requests.RequestException):
        sign_in.access_token()


@pytest.mark.parametrize("status", [400, 401])
def test_a_client_google_no_longer_knows_says_the_sign_in_needs_doing_again(token_file, status):
    # The client deleted in Google Cloud, or its secret made anew: taken for Google being
    # out of reach, "I can't reach Google's calendar" would never have gone away.
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(status, {"error": "invalid_client", "error_description": "The OAuth client was not found."})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()


@pytest.mark.parametrize("status", [400, 401])
def test_a_client_google_wont_let_sign_in_says_the_sign_in_needs_doing_again(token_file, status):
    # A client of the wrong type - not a "Desktop app" - which trying again won't put right either.
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(status, {"error": "unauthorized_client", "error_description": "Unauthorized"})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()


def test_without_a_sign_in_it_needs_doing(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert not sign_in.signed_in()
    with pytest.raises(SignInNeeded):
        sign_in.access_token()
    assert google.asked == []


# Signing in, when it goes wrong

def test_a_sign_in_without_every_permission_isnt_kept(token_file):
    # Google lets each box on its page be unticked. Kept, the sign-in said "Signed in to
    # Google." - and adding an event would only have failed later, by voice.
    answer = _token_answer()
    answer.body["scope"] = " ".join(google_calendar.SCOPES[:2])
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    said = str(refused.value)
    assert "add, change and remove events on your own calendars" in said and "read your calendars' events" not in said
    assert "sign in again, with every box ticked" in said
    assert not os.path.exists(token_file)


def test_a_client_google_doesnt_know_at_the_sign_in_is_told_apart(token_file):
    # A mistyped GOOGLE_CLIENT_SECRET at first setup, which trying again never helps.
    google = FakeGoogle(**{google_calendar.TOKEN: [FakeResponse(401, {"error": "invalid_client",
                                                                      "error_description": "Unauthorized"})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(ClientRefused):
        sign_in.finish(_pasted(state))

    assert not os.path.exists(token_file)


def test_a_google_sign_in_that_couldnt_be_saved_isnt_said_to_have_worked(token_file, monkeypatch, caplog):
    def full(path, data, indent=None):
        raise OSError(28, "No space left on device", path)

    monkeypatch.setattr(google_calendar.storage, "save_json", full)
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]}))
    state = _query(sign_in.start())["state"]
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GoogleSignInError, match="Couldn't save the sign-in - check Nova's data folder."):
        sign_in.finish(_pasted(state))

    assert not sign_in.signed_in()
    # Not kept in memory either, as if it had worked until a restart.
    assert sign_in._access is None
    for secret in (REFRESH_TOKEN, ACCESS_TOKEN, "made-up-code", CLIENT_SECRET):
        assert secret not in caplog.text


def test_two_google_finishes_at_once_use_the_state_once(token_file):
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer() for _ in range(8)]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    address = _pasted(_query(sign_in.start())["state"])
    finished = []
    ready = threading.Barrier(8)

    def finish():
        ready.wait()
        try:
            sign_in.finish(address)
            finished.append(True)
        except ValueError:
            pass

    threads = [threading.Thread(target=finish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert finished == [True] and len(google.asked) == 1


def test_urllib3_never_logs_googles_addresses_whatever_novas_level():
    # Its debug lines have each address asked for - an events address has a calendar's
    # id in it, and the user's own is their email address. Run alone, so nothing else
    # Nova imports has quietened it first.
    checked = subprocess.run(
        [sys.executable, "-c", "import logging, voice_assistant.google_calendar; "
                               "assert not logging.getLogger('urllib3.connectionpool').isEnabledFor(logging.INFO)"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_a_started_sign_in_runs_out(token_file, monkeypatch):
    # Kept for ever, a sign-in started and left would still take its address days later.
    now = [1000.0]
    monkeypatch.setattr(google_calendar.time, "monotonic", lambda: now[0])
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    state = _query(sign_in.start())["state"]
    now[0] += 601
    with pytest.raises(GoogleSignInError, match="That sign-in ran out - start again."):
        sign_in.finish(_pasted(state))
    assert google.asked == [] and not os.path.exists(token_file)

    # Just within the ten minutes, it's taken.
    state = _query(sign_in.start())["state"]
    now[0] += 599
    sign_in.finish(_pasted(state))
    assert sign_in.signed_in()


@pytest.mark.parametrize("error, logged", [("access_denied", "access_denied"),
                                           ("bad\nINFO forged line", "an unreadable error")])
def test_a_cancelled_google_sign_in_is_answered_as_spotifys_is(token_file, caplog, error, logged):
    # Cancel, on Google's page, sends the browser back with error= and the right state,
    # and no code - which isn't the user copying too little of the address.
    caplog.set_level(logging.INFO)
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError, match="Google didn't accept that sign-in - start again."):
        sign_in.finish(f"{google_calendar.REDIRECT}?{urlencode({'error': error, 'state': state})}")

    assert google.asked == [] and not os.path.exists(token_file)
    assert f"Google turned the sign-in down: {logged}" in caplog.text and "forged" not in caplog.text
    # Used up, as any other finish.
    with pytest.raises(GoogleSignInError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


@pytest.mark.parametrize("answer, code", [
    (FakeResponse(400, {"error": "redirect_uri_mismatch", "error_description": "Not what the client lists"}),
     "redirect_uri_mismatch"),
    (FakeResponse(403, {"error": "access_denied", "error_description": "The project's off"}), "access_denied"),
    # Anything but a plain error code is only its status - the answer's own words never.
    (FakeResponse(400, {"error": "<b>made-up</b>", "error_description": "Words"}), "HTTP 400"),
    (FakeResponse(404, ["not", "an", "error"]), "HTTP 404"),
])
def test_google_refusing_the_sign_in_otherwise_names_its_error(token_file, caplog, answer, code):
    # Said as Google out of reach, a client set up wrongly sent the user the wrong way.
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    assert str(refused.value) == f"Google refused the sign-in ({code}) - check the client in Google Cloud."
    assert not os.path.exists(token_file)
    for words in ("Not what the client lists", "The project's off", "<b>", "Words"):
        assert words not in caplog.text


@pytest.mark.parametrize("answer", [
    FakeResponse(429, {"error": "rate_limit_exceeded", "error_description": "Words"}),
    FakeResponse(429, ["not", "an", "error"]),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits",
                                                          "reason": "userRateLimitExceeded"}]}}),
])
def test_google_slowing_the_sign_in_down_says_to_try_again_not_to_check_the_client(token_file, caplog, answer):
    # A busy Google isn't a client set up wrongly, which "check the client in Google Cloud" says.
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    assert str(refused.value) == "Google is slowing Nova down - start the sign-in again in a minute."
    assert f"HTTP {answer.status_code}" in caplog.text and "Words" not in caplog.text
    assert not os.path.exists(token_file)
    # The sign-in is used up, so it is started afresh.
    with pytest.raises(GoogleSignInError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


@pytest.mark.parametrize("changed", [
    {"access_token": None},
    {"access_token": ""},
    {"refresh_token": None},
    {"scope": ["https://www.googleapis.com/auth/calendar.events.owned"]},
    {"token_type": "mac"},
    {"token_type": None},
])
def test_an_answer_from_google_that_isnt_a_whole_sign_in_isnt_kept(token_file, changed):
    # Saved without an access token, the page said "Couldn't reach Google" for a sign-in it
    # had kept - and one with no scope as text didn't answer at all.
    answer = _token_answer()
    for name, value in changed.items():
        if value is None:
            answer.body.pop(name, None)
        else:
            answer.body[name] = value
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError, match="Google didn't give Nova a sign-in to keep - try again."):
        sign_in.finish(_pasted(state))

    assert not os.path.exists(token_file) and sign_in._access is None


class RunsAtOnce:
    """Stands in for threading.Thread, doing its work as it's started."""

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def test_a_sign_in_reads_the_list_again_at_once(make_calendar, monkeypatch):
    calendar = make_calendar()
    calendar.calendars()
    calendar._session.answers[LIST] = [_calendar_list(_entry("calendar-gym", "Gym"))]
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)

    calendar.read_list_again()

    assert [each["id"] for each in calendar.known()] == ["calendar-gym"]
    assert "gym" in calendar.activity_words()


def test_a_list_read_under_way_cant_put_the_last_accounts_list_back(make_calendar, monkeypatch):
    # A read begun with the account before's sign-in, which answers once the new account's
    # read - started by its sign-in - has finished.
    calendar = make_calendar()
    google = calendar._session
    google.answers[LIST] = [_calendar_list(_entry("calendar-old", "Old", primary=True)),
                            _calendar_list(_entry("calendar-new", "New", primary=True))]
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)
    asked = google.get

    def get(url, *args, **kwargs):
        answer = asked(url, *args, **kwargs)
        if url == LIST and len([each for each in google.asked if each[0] == LIST]) == 1:
            calendar.read_list_again()
        return answer

    monkeypatch.setattr(google, "get", get)
    calendar.calendars()

    assert [each["id"] for each in calendar.known()] == ["calendar-new"]


class NeverRuns:
    """Stands in for threading.Thread, for work still under way when the test looks."""

    def __init__(self, target, **kwargs):
        pass

    def start(self):
        pass


@pytest.mark.parametrize("thread", [RunsAtOnce, NeverRuns], ids=["new list read first", "new list not read yet"])
def test_a_list_read_under_way_as_another_account_signs_in_gives_the_list_kept(make_calendar, monkeypatch,
                                                                               thread):
    # The page shows what it's given, and checks a tap against what's kept - so the two
    # are the new account's list, whichever read finishes first, never one of each.
    calendar = make_calendar()
    google = calendar._session
    google.answers[LIST] = [_calendar_list(_entry("calendar-old", "Old", primary=True)),
                            _calendar_list(_entry("calendar-new", "New", primary=True))]
    monkeypatch.setattr(google_calendar.threading, "Thread", thread)
    asked = google.get

    def get(url, *args, **kwargs):
        answer = asked(url, *args, **kwargs)
        if url == LIST and len([each for each in google.asked if each[0] == LIST]) == 1:
            calendar.read_list_again()
        return answer

    monkeypatch.setattr(google, "get", get)
    given = calendar.calendars()

    assert [each["id"] for each in given] == [each["id"] for each in calendar.known()] == ["calendar-new"]


def test_nova_says_google_is_signed_in_to_on_the_settings_page():
    assert NEEDS_SIGN_IN == "I need signing in to Google again. That's done on the settings page."


def test_the_command_on_the_brain_is_gone():
    # The settings page signs in now, so there's no second way in to keep safe.
    assert not hasattr(google_calendar, "main")
    assert "__main__" not in inspect.getsource(google_calendar)


# Reading the calendars

LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# Made as the file is read, before the london fixture has set the time zone, so
# given it themselves.
LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def london(monkeypatch):
    """
    Runs every test on UK time, whatever the machine's own time zone. Calendar times are
    said in the brain's own time, and the events here are written in BST - so on
    GitHub, which runs on UTC, uni "from 11:30 AM" came out as "from 10:30 AM".
    """
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class FakeSignIn:
    """Stands in for GoogleSignIn, with an access token that's always good."""

    def access_token(self):
        return ACCESS_TOKEN

    def drop_access_token(self):
        pass


def _events_url(calendar_id):
    return f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"


def _entry(calendar_id, name, primary=False, access_role="owner", override=None):
    """One calendar in Google's calendarList, as its API reference gives it."""
    entry = {"kind": "calendar#calendarListEntry", "etag": '"1"', "id": calendar_id, "summary": name,
             "timeZone": "Europe/London", "accessRole": access_role, "selected": True}
    if primary:
        entry["primary"] = True
    if override:
        entry["summaryOverride"] = override
    return entry


def _calendar_list(*entries, next_page=None):
    page = {"kind": "calendar#calendarList", "etag": '"1"', "items": list(entries)}
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _when(value):
    """An event's start or end as Google gives it - a date for all day, or a time."""
    return {"date": value} if len(value) == 10 else {"dateTime": value, "timeZone": "Europe/London"}


def _google_event(summary, start, end=None, **more):
    """One event, as Google's events.list gives it."""
    event = {"kind": "calendar#event", "id": f"made-up-{abs(hash((summary, start)))}", "status": "confirmed",
             "summary": summary, "start": _when(start)}
    if end:
        event["end"] = _when(end)
    event.update(more)
    return event


def _events_page(*events, next_page=None):
    page = {"kind": "calendar#events", "summary": "A calendar", "timeZone": "Europe/London", "items": list(events)}
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _choices(tmp_path, activities=None, whats_on=None):
    """
    The calendars' choices, in calendars.json - which are activities, and which are out of
    what's on, by calendar id.
    """
    choices = ChoiceStore(str(tmp_path / "calendars.json"), google_calendar.CALENDAR_CHOICES)
    if activities:
        choices.change(lambda state: state.update(activities=activities))
    if whats_on:
        choices.change(lambda state: state.update(whats_on=whats_on))
    return choices


@pytest.fixture
def make_calendar(monkeypatch, tmp_path):
    """
    Makes GoogleCalendars reading from a FakeGoogle: one calendar, Personal - the user's
    own - with the events given, unless calendars gives each calendar's id and its events -
    each read reads times over. Each is named from its id: "calendar-uni-timetable" is
    "Uni timetable".
    """
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)

    def make(events=(), calendars=None, activities=None, reads=1, whats_on=None):
        calendars = calendars if calendars is not None else {"calendar-personal": list(events)}
        google = FakeGoogle(**{LIST: [_calendar_list(*[_entry(calendar_id, _named(calendar_id),
                                                              primary=calendar_id == "calendar-personal")
                                                       for calendar_id in calendars])],
                               **{_events_url(calendar_id): [_events_page(*its_events)] * reads
                                  for calendar_id, its_events in calendars.items()}})
        return GoogleCalendar(FakeSignIn(), _choices(tmp_path, activities, whats_on), session=google)

    return make


def _named(calendar_id):
    return calendar_id.split("-", 1)[1].replace("-", " ").capitalize()


def _read(calendar):
    """The events requests made, each as (calendar id, params)."""
    prefix = "https://www.googleapis.com/calendar/v3/calendars/"
    return [(url[len(prefix):-len("/events")], params) for url, params in calendar._session.asked
            if url.startswith(prefix)]


def test_every_calendar_in_googles_list_is_read(monkeypatch, tmp_path):
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)
    google = FakeGoogle(**{
        LIST: [_calendar_list(_entry("calendar-personal", "Personal", primary=True),
                              _entry("calendar-uni", "My timetable", access_role="reader", override="Uni"),
                              next_page="page-2"),
               _calendar_list(_entry("calendar-work", "Work", access_role="writer"))],
        _events_url("calendar-personal"): [_events_page(
            _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00"))],
        _events_url("calendar-uni"): [_events_page(
            _google_event("CSC2035 Lecture", "2026-10-22T11:30:00+01:00", "2026-10-22T12:30:00+01:00"))],
        _events_url("calendar-work"): [_events_page(
            _google_event("Shift", "2026-10-22T07:00:00+01:00", "2026-10-22T09:00:00+01:00"))],
    })
    calendar = GoogleCalendar(FakeSignIn(), _choices(tmp_path), session=google)

    events = calendar.events(date(2026, 10, 22), 1)

    assert [(event["summary"], event["calendar"]) for event in events] == [
        ("Shift", "Work"), ("Dentist", "Personal"), ("CSC2035 Lecture", "Uni")]
    # Both pages of the list, the second asked for by Google's token for it.
    assert [params.get("pageToken") for url, params in google.asked if url == LIST] == [None, "page-2"]
    assert calendar.known() == [
        {"id": "calendar-personal", "name": "Personal", "primary": True, "writable": True, "timeZone": "Europe/London"},
        {"id": "calendar-uni", "name": "Uni", "primary": False, "writable": False, "timeZone": "Europe/London"},
        {"id": "calendar-work", "name": "Work", "primary": False, "writable": False, "timeZone": "Europe/London"}]
    for calendar_id, params in _read(calendar):
        assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
        assert params["maxResults"] == 250
        assert params["timeMin"] == "2026-10-22T00:00:00+01:00" and params["timeMax"] == "2026-10-23T00:00:00+01:00"
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal", "calendar-uni",
                                                                    "calendar-work"]
    # Each with Nova's access token, and a time limit.
    assert all(fetched["headers"] == {"Authorization": f"Bearer {ACCESS_TOKEN}"} and fetched["timeout"]
               for fetched in google.fetched)

    # The list is kept for a while, and only the calendars given are read when some are.
    google.answers[_events_url("calendar-work")].append(_events_page())
    calendar.events(date(2026, 10, 22), 1, calendar_ids=["calendar-work"])
    assert [url for url, _ in google.asked].count(LIST) == 2
    assert [calendar_id for calendar_id, _ in _read(calendar)][-1] == "calendar-work"
    assert len(_read(calendar)) == 4


def test_every_page_of_events_is_read(make_calendar):
    calendar = make_calendar()
    calendar._session.answers[_events_url("calendar-personal")] = [
        _events_page(_google_event("Dentist", "2026-10-22T09:30:00+01:00"), next_page="events-2"),
        _events_page(_google_event("Dinner with Sam", "2026-10-22T19:00:00+01:00"), next_page="events-3"),
        _events_page(_google_event("Bin day", "2026-10-22", "2026-10-23"))]

    events = calendar.events(date(2026, 10, 22), 1)

    assert [event["summary"] for event in events] == ["Bin day", "Dentist", "Dinner with Sam"]
    pages = [params for _, params in _read(calendar)]
    assert [params.get("pageToken") for params in pages] == [None, "events-2", "events-3"]
    # Every page is for the same days.
    assert len({(params["timeMin"], params["timeMax"]) for params in pages}) == 1


def test_twenty_pages_of_events_are_read_and_no_more(make_calendar):
    # A token which always leads to another page - Google looping - would otherwise never stop.
    calendar = make_calendar()
    endless = _events_page(_google_event("Dentist", "2026-10-22T09:30:00+01:00"), next_page="again")
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 30

    with pytest.raises(ValueError, match="Google gave more than 20 pages"):
        calendar.events(date(2026, 10, 22), 1)

    assert len(_read(calendar)) == 20
    # Said as Google out of reach, as any answer of the wrong shape is.
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 30
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 21)) == {
        "success": False, "message": CANT_REACH_GOOGLE}

    # The twentieth page being the last is no trouble.
    last = _events_page(_google_event("Bin day", "2026-10-22", "2026-10-23"))
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 19 + [last]
    assert len(calendar.events(date(2026, 10, 22), 1)) == 20
    assert len(_read(calendar)) == 60


def test_a_repeating_event_is_on_each_of_its_days(make_calendar):
    # singleEvents=true has Google give each time it's on, as Home Assistant did.
    lectures = [_google_event("CSC2035 Lecture", f"2026-10-{day}T11:00:00+01:00", f"2026-10-{day}T12:00:00+01:00",
                              recurringEventId="made-up-weekly-lecture",
                              originalStartTime={"dateTime": f"2026-10-{day}T11:00:00+01:00",
                                                 "timeZone": "Europe/London"})
                for day in (20, 22)]
    calendar = make_calendar(lectures)

    assert calendar.find("lecture", date(2026, 10, 20), today=date(2026, 10, 20))["message"] == (
        "You've got CSC2035 Lecture today, at 11:00 AM; and CSC2035 Lecture on Thursday, at 11:00 AM.")


def test_an_all_day_event_is_said_on_its_day(make_calendar):
    calendar = make_calendar([_google_event("Sam's birthday", "2026-10-30", "2026-10-31")], reads=2)

    assert calendar.agenda(date(2026, 10, 30), 1, today=date(2026, 10, 20))["message"] == \
        "On Friday the 30th of October, you've got Sam's birthday, all day."
    # Google's end is the day after - the 30th is the only day it's on.
    [event] = calendar.events(date(2026, 10, 30), 1)
    assert (event["start"], event["end"], event["all_day"]) == (date(2026, 10, 30), None, True)


def test_a_cancelled_event_is_left_out(make_calendar):
    # A time taken off a repeating event comes as its own entry, cancelled.
    calendar = make_calendar([
        _google_event("CSC2035 Lecture", "2026-10-22T11:00:00+01:00", "2026-10-22T12:00:00+01:00"),
        {"kind": "calendar#event", "id": "made-up-cancelled", "status": "cancelled",
         "recurringEventId": "made-up-weekly-lecture",
         "originalStartTime": {"dateTime": "2026-10-22T15:00:00+01:00"}},
        _google_event("Football", "2026-10-22T18:00:00+01:00", "2026-10-22T19:00:00+01:00", status="cancelled")])

    assert [event["summary"] for event in calendar.events(date(2026, 10, 22), 1)] == ["CSC2035 Lecture"]


def test_an_event_after_the_clocks_go_back_is_said_at_its_own_time(make_calendar):
    # The clocks went back on Sunday the 25th of October 2026. Google gives a winter time
    # in London as UTC, with a Z.
    calendar = make_calendar([_google_event("Lecture", "2026-10-24T09:00:00+01:00", "2026-10-24T10:00:00+01:00"),
                              _google_event("Lecture", "2026-10-26T09:00:00Z", "2026-10-26T10:00:00Z")])

    assert calendar.find("lecture", date(2026, 10, 24), today=date(2026, 10, 24))["message"] == (
        "You've got Lecture today, at 9:00 AM; and Lecture on Monday, at 9:00 AM.")

    # Three days over the change are each a whole day, from local midnight to local midnight.
    calendar = make_calendar()
    calendar.events(date(2026, 10, 24), 3)
    [(_, params)] = _read(calendar)
    assert params["timeMin"] == "2026-10-24T00:00:00+01:00" and params["timeMax"] == "2026-10-27T00:00:00+00:00"


@pytest.mark.parametrize("down", ["no network", "not answering", "a server error", "a wrong answer"])
def test_without_the_internet_it_says_it_cant_reach_google(make_calendar, monkeypatch, caplog, down):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    if down == "no network":
        monkeypatch.setattr(google_calendar.network, "online", lambda: False)

    for asked in (lambda: calendar.agenda(date(2026, 10, 22), 1), lambda: calendar.find("MOT", date(2026, 10, 22)),
                  lambda: calendar.activity("work", date(2026, 10, 22))):
        for calendar_id in ("calendar-personal", "calendar-work"):
            url = _events_url(calendar_id)
            # Each error names the address, with the calendar's id in it, as requests' own do.
            calendar._session.answers[url] = [{
                "no network": _events_page(),
                "not answering": requests.ConnectionError(f"Max retries exceeded with url: {url}"),
                "a server error": requests.HTTPError(f"503 Server Error: Backend Error for url: {url}",
                                                     response=FakeResponse(503, {"error": {"code": 503}})),
                "a wrong answer": {"items": "nothing"}}[down]]
        with caplog.at_level(logging.INFO):
            assert asked() == {"success": False, "message": CANT_REACH_GOOGLE}
    if down == "no network":
        # Nothing is even tried - not the sign-in either.
        assert calendar._session.asked == []
    # The calendar's id - for the primary, the user's email address - is never logged.
    assert caplog.records and not any("calendar-" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("status", [403, 404])
def test_a_calendar_google_wont_give_is_left_out_and_the_rest_read(make_calendar, caplog, status):
    # Deleted while the list was kept, or no longer shared: one calendar said "I can't
    # reach Google's calendar" for every one of them.
    calendar = make_calendar(calendars={"calendar-personal": [
        _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00")], "calendar-gone": []})
    # Answered as Google does, and raised by _get's own raise_for_status - past the 401 retry.
    calendar._session.answers[_events_url("calendar-gone")] = [FakeResponse(status, {"error": {
        "code": status, "errors": [{"domain": "global", "reason": "notFound" if status == 404 else "forbidden"}]}})]

    with caplog.at_level(logging.INFO):
        assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
            "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not any("calendar-" in record.getMessage() for record in caplog.records)
    # Google's list is asked for afresh next time, so a deleted calendar is gone from it.
    calendar._session.answers[LIST].append(_calendar_list(_entry("calendar-personal", "Personal", primary=True)))
    calendar._session.answers[_events_url("calendar-personal")].append(_events_page())
    calendar.agenda(date(2026, 10, 22), 1)
    assert [url for url, _ in calendar._session.asked].count(LIST) == 2


@pytest.mark.parametrize("answer", [
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits",
                                                          "reason": "userRateLimitExceeded"}]}}),
    FakeResponse(429, {"error": {"code": 429, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
])
def test_google_slowing_nova_down_is_google_out_of_reach_not_a_calendar_gone(make_calendar, answer):
    # Left out as if gone, the calendar refused for a moment would have been "nothing on".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[_events_url("calendar-work")] = [answer]
    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._known_at is not None


@pytest.mark.parametrize("status", [403, 404])
def test_an_activity_whose_calendar_google_wont_give_says_it_cant_reach_google(make_calendar, status):
    # Never "You haven't got gym tomorrow" when the Gym calendar wasn't read at all.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-gym": []})
    calendar._session.answers[_events_url("calendar-gym")] = [FakeResponse(status, {"error": {"code": status}})]
    assert calendar.activity("gym", date(2026, 10, 21)) == {"success": False, "message": CANT_REACH_GOOGLE}


DENTIST = _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00")
SHIFT = _google_event("Shift", "2026-10-22T07:00:00+01:00", "2026-10-22T09:00:00+01:00")


def test_a_calendar_out_of_whats_on_isnt_read_for_it(make_calendar):
    # Chosen out on the page before a restart: nothing's known of Google's list yet, so
    # it's left out of the calendars read once that's been read, not before.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False}, reads=2)
    assert calendar.known() == [] and calendar.left_out() == {"calendar-work"}

    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert calendar.find("shift", date(2026, 10, 22), today=date(2026, 10, 20))["message"] == \
        "I can't see shift on your calendar in the next three months."
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal"] * 2


def test_a_calendar_gone_doesnt_stop_whats_on_with_one_left_out(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT], "calendar-gone": []},
                             whats_on={"calendar-work": False})
    calendar._session.answers[_events_url("calendar-gone")] = [FakeResponse(404, {"error": {"code": 404}})]

    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal", "calendar-gone"]
    # One chosen out that's gone from Google's list is no trouble either.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST]}, whats_on={"calendar-deleted": False})
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20))["success"]


def test_an_activity_out_of_whats_on_is_still_read_for_itself(make_calendar):
    # Out of what's on, so a day isn't full of shifts - but "am I working on Thursday?" still knows.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False})
    assert calendar.activity("work", date(2026, 10, 22), today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got work from 7:00 AM until 9:00 AM."}
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-work"]


def test_an_activity_chosen_on_the_page_is_heard_at_the_next_request(make_calendar, tmp_path):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar.calendars()
    intents = LocalIntents(activities=calendar.activity_words)
    assert "personal" not in calendar.activity_words()

    calendar.choose("calendar-personal", activity=True)
    calendar.choose("calendar-work", activity=False)
    assert "personal" in calendar.activity_words() and "work" not in calendar.activity_words()
    [action] = intents.match("When's personal tomorrow?")["actions"]
    assert action.params["activity"] == "personal"
    # Kept for after a restart, and what's on is left as it was.
    assert ChoiceStore(str(tmp_path / "calendars.json"), google_calendar.CALENDAR_CHOICES).state == {
        "activities": {"calendar-personal": True, "calendar-work": False}, "whats_on": {}, "new_events": None}

    calendar.choose("calendar-work", whats_on=False)
    calendar.choose("calendar-personal", whats_on=False)
    calendar.choose("calendar-personal", whats_on=True)
    assert calendar.left_out() == {"calendar-work"}
    # Only a calendar Google's showing.
    with pytest.raises(ValueError, match="^Google isn't showing that calendar just now.$"):
        calendar.choose("calendar-made-up", activity=True)
    assert "calendar-made-up" not in calendar.choices.get("activities")


def test_a_calendar_out_of_whats_on_is_out_of_a_briefing_too(make_calendar):
    # A routine's briefing, and Claude, ask through get_calendar - which is the agenda.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False})
    result = ActionExecutor(calendar=calendar).execute(Action("get_calendar", {"start": "2026-10-22", "days": 1}))
    assert "Dentist" in result.say and "work" not in result.say.lower()
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal"]


def test_an_activity_switch_is_never_on_for_a_calendar_that_cant_be_one(make_calendar):
    # One with no name, or a name another calendar listed first has, is never heard or
    # told to Claude - so the page's switch for it isn't "on", whatever's chosen.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []},
                             activities={"calendar-blank": True, "calendar-work-too": True})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-work-too", "Work"), _entry("calendar-blank", ""), _entry("calendar-gym", "Gym"))]
    listed = {each["id"]: each for each in calendar.calendars()}
    assert {calendar_id: calendar.is_activity(each) for calendar_id, each in listed.items()} == {
        "calendar-personal": False, "calendar-work": True, "calendar-work-too": None, "calendar-blank": None,
        "calendar-gym": True}
    # With the first switched off, the second of the name is the one heard.
    calendar.choose("calendar-work", activity=False)
    assert calendar.is_activity(listed["calendar-work"]) is False
    assert calendar.is_activity(listed["calendar-work-too"]) is True


def test_an_event_without_a_name_in_words_is_still_said(make_calendar):
    calendar = make_calendar([_google_event(42, "2026-10-22T09:30:00+01:00"),
                              _google_event(None, "2026-10-22T11:00:00+01:00")])
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20))["message"] == \
        "On Thursday, you've got something at 9:30 AM, and something at 11:00 AM."


def test_a_sign_in_that_needs_doing_again_says_so(make_calendar, token_file):
    calendar = make_calendar()
    # Not signed in at all, so nothing is asked of Google.
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle())

    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": NEEDS_SIGN_IN}
    assert calendar._session.asked == []


def test_an_access_token_google_turns_down_is_got_afresh_once(make_calendar, token_file):
    # A sign-in removed in the Google account turns its access token down straight away.
    # Kept until it ran out, that would have been "I can't reach Google" for an hour.
    _signed_in(token_file)
    calendar = make_calendar(reads=2)
    url = _events_url("calendar-personal")
    google = calendar._session
    google.answers[google_calendar.TOKEN] = [_token_answer("first-access", refresh=None),
                                             _token_answer("second-access", refresh=None)]
    google.answers[url] = [FakeResponse(401, {"error": {"code": 401}}), _events_page()]
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert calendar.agenda(date(2026, 10, 22), 1)["success"] is True
    assert [fetched["headers"]["Authorization"] for fetched in google.fetched if fetched["params"].get("timeMin")] \
        == ["Bearer first-access", "Bearer second-access"]

    # Turned down afresh, it's the sign-in that needs doing again.
    google.answers[google_calendar.TOKEN] = [_token_answer("third-access", refresh=None)]
    google.answers[url] = [FakeResponse(401, {"error": {"code": 401}}), FakeResponse(401, {"error": {"code": 401}})]
    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": NEEDS_SIGN_IN}


def test_the_calendar_is_off_without_googles_keys(monkeypatch, make_config, caplog, tmp_path):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    with caplog.at_level("INFO"):
        assert GoogleCalendar.from_env(make_config()) is None
    assert len([record for record in caplog.records if "GOOGLE_CLIENT_ID" in record.getMessage()]) == 1

    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(google_calendar.paths, "GOOGLE_TOKEN_FILE", "/made/up/google_token.json")
    monkeypatch.setattr(google_calendar.paths, "DATA_DIR", str(tmp_path))
    calendar = GoogleCalendar.from_env(make_config())
    assert calendar.sign_in.client_id == CLIENT_ID and calendar.sign_in.path == "/made/up/google_token.json"
    # Every calendar in what's on, and new events to the primary, to begin with.
    assert calendar.choices.path == str(tmp_path / "calendars.json")
    assert calendar.choices.state == {"activities": {}, "whats_on": {}, "new_events": None}


# The calendar's words, moved from Home Assistant's

def test_the_calendar_is_read_for_the_days_asked_about(make_calendar):
    thursday = date(2026, 10, 22)
    calendar = make_calendar([
        _google_event("Dinner with Sam", "2026-10-22T19:00:00+01:00"),
        _google_event("Dentist", "2026-10-22T09:30:00+01:00"),
        _google_event("Bin day", "2026-10-22", "2026-10-23"),
    ])
    events = calendar.events(thursday, 1)

    assert [event["summary"] for event in events] == ["Bin day", "Dentist", "Dinner with Sam"]
    [(_, sent)] = _read(calendar)
    start = datetime.fromisoformat(sent["timeMin"])
    assert start.date() == thursday and datetime.fromisoformat(sent["timeMax"]) - start == timedelta(days=1)


def _event(summary, when, all_day=False):
    return {"summary": summary, "start": when, "all_day": all_day}


TODAY = date(2026, 10, 20)


def test_a_day_is_said_event_by_event():
    events = [_event("Bin day", date(2026, 10, 22), all_day=True),
              _event("Dentist", datetime(2026, 10, 22, 9, 30))]
    assert describe_agenda(events, date(2026, 10, 22), 1, TODAY) == \
        "On Thursday, you've got Bin day, all day, and Dentist at 9:30 AM."


def test_a_free_day_says_so():
    assert describe_agenda([], TODAY + timedelta(days=1), 1, TODAY) == "You've got nothing on tomorrow."
    assert describe_agenda([], date(2026, 10, 24), 2, TODAY) == "You've got nothing on Saturday or Sunday."


def test_several_days_only_say_the_ones_with_something_on():
    events = [_event("Football", datetime(2026, 10, 25, 10, 0))]
    assert describe_agenda(events, date(2026, 10, 24), 2, TODAY) == "On Sunday, you've got Football at 10:00 AM."


def test_a_busy_day_is_cut_short():
    events = [_event(f"Meeting {n}", datetime(2026, 10, 20, 9 + n, 0)) for n in range(7)]
    assert describe_agenda(events, TODAY, 1, TODAY).endswith("Meeting 4 at 1:00 PM, and 2 more.")


def test_a_day_further_off_is_said_in_full():
    assert describe_agenda([], date(2026, 10, 30), 1, TODAY) == "You've got nothing on Friday the 30th of October."


def _activity(name, day):
    """An activity's day, as _as_activities gives it."""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9)
    return {"summary": name, "start": start, "all_day": False, "spans": [[start, start + timedelta(hours=4)]],
            "activity": True}


def test_a_week_is_an_overview_without_the_times():
    # Every time of every day, "uni from 11:30 AM until 1 PM and from 2:30 PM until
    # 4:30 PM", took 52 seconds for a week.
    events = sorted([_activity("uni", TODAY), _activity("uni", date(2026, 10, 22)), _activity("uni", date(2026, 10, 26)),
                     _activity("work", date(2026, 10, 21)), _activity("work", date(2026, 10, 24)),
                     _event("Payday", datetime(2026, 10, 21, 16)), _event("Haircut", datetime(2026, 10, 23, 12, 20))],
                    key=lambda event: event["start"])
    assert describe_agenda(events, TODAY, 7, TODAY) == (
        "Over the next week, you've got uni today, Thursday and Monday, and work tomorrow and Saturday. "
        "There's also Payday tomorrow, and Haircut on Friday.")


def test_a_fortnight_counts_the_days_and_names_only_a_few_things():
    # Read out whole, a fortnight once took three and a half minutes.
    events = ([_activity("work", TODAY + timedelta(days=n)) for n in range(14)]
              + [_activity("uni", TODAY + timedelta(days=n)) for n in (0, 2, 7)]
              + [_event(f"Meeting {n}", datetime(2026, 10, 21, 10) + timedelta(days=n)) for n in range(7)])
    events.sort(key=lambda event: event["start"])
    said = describe_agenda(events, TODAY, 14, TODAY)
    assert said.startswith("Over the next two weeks, you've got work every day, and uni on 3 days. There's also "
                           "Meeting 0 tomorrow, Meeting 1 on Thursday,")
    assert said.count("Meeting") == 5 and said.endswith(", and 2 more.")


def test_an_overview_of_another_week_says_which_one():
    events = [_activity("uni", date(2026, 10, 27)), _event("Dentist", datetime(2026, 10, 29, 9, 30))]
    assert describe_agenda(events, date(2026, 10, 26), 7, TODAY) == (
        "From Monday to Sunday the 1st of November, you've got uni on Tuesday. "
        "There's also Dentist on Thursday the 29th of October.")
    assert describe_agenda([], date(2026, 10, 26), 7, TODAY) == \
        "You've got nothing on from Monday to Sunday the 1st of November."


UNI_DAY = [
    _google_event("CSC2035/P01/01 <04-07>", "2026-10-20T11:30:00+01:00", "2026-10-20T12:30:00+01:00"),
    _google_event("CSC2032/T01/01", "2026-10-20T14:30:00+01:00", "2026-10-20T15:30:00+01:00"),
    _google_event("CSC2035/L01/01 <04-07>", "2026-10-20T15:30:00+01:00", "2026-10-20T17:30:00+01:00"),
    _google_event("Discussion group", "2026-10-20T18:00:00+01:00", "2026-10-20T19:00:00+01:00"),
]


def test_whats_on_says_a_day_of_uni_as_uni(make_calendar):
    # Each session was read out by its own name - "CSC2035/P01/01 <04-07> at 11:30
    # AM" - which took 25 seconds for one day, and meant little. The Uni calendar's
    # sessions are uni, whatever each is called; the user's own calendar's are as they are.
    calendar = make_calendar(calendars={"calendar-personal": UNI_DAY[3:], "calendar-uni": UNI_DAY[:3]})
    assert calendar.agenda(TODAY, today=TODAY)["message"] == (
        "Today, you've got uni from 11:30 AM until 12:30 PM and from 2:30 PM until 5:30 PM, "
        "and Discussion group at 6:00 PM.")


def test_one_thing_is_looked_for_months_ahead(make_calendar):
    calendar = make_calendar(UNI_DAY + [
        _google_event("Car MOT", "2026-11-30T09:30:00+00:00", "2026-11-30T10:30:00+00:00")])
    assert calendar.find("mot", TODAY, today=TODAY) == {
        "success": True, "message": "You've got Car MOT on Monday the 30th of November, at 9:30 AM."}
    [(_, sent)] = _read(calendar)
    assert (datetime.fromisoformat(sent["timeMax"]).date() - datetime.fromisoformat(sent["timeMin"]).date()).days == 90


def test_looking_for_something_which_isnt_there_says_so(make_calendar):
    calendar = make_calendar(UNI_DAY)
    assert calendar.find("MOT", TODAY, today=TODAY)["message"] == \
        "I can't see MOT on your calendar in the next three months."


def test_several_found_are_said_together_up_to_a_few():
    events = [_event("Football", datetime(2026, 10, 21, 10, 0)), _event("Sam's birthday", date(2026, 10, 22), True)]
    assert describe_found("football", events[:1] * 5, TODAY) == (
        "You've got Football tomorrow, at 10:00 AM; Football tomorrow, at 10:00 AM; and Football tomorrow, "
        "at 10:00 AM. And 2 more after that.")
    assert describe_found("sam", events[1:], TODAY) == "You've got Sam's birthday on Thursday, all day."


# The activities - each calendar but the user's own, by its name

def test_every_event_on_the_work_calendar_is_work(make_calendar):
    calendar = make_calendar(calendars={
        "calendar-personal": [_google_event("Work drinks", "2026-10-21T19:00:00+01:00", "2026-10-21T21:00:00+01:00")],
        "calendar-work": [_google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"),
                          _google_event("Cover", "2026-10-21T08:00:00+01:00", "2026-10-21T12:00:00+01:00")]})

    assert calendar.activity("work", date(2026, 10, 21), today=date(2026, 10, 20)) == {
        "success": True,
        "message": "Tomorrow, you've got work from 8:00 AM until noon, and from 3:30 PM until 10:00 PM."}
    # Only the Work calendar is read - "Work drinks", on the user's own, isn't work.
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-work"]


def test_a_calendar_added_later_is_an_activity_by_itself(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": [], "calendar-gym": [
        _google_event("Legs", "2026-10-21T18:00:00+01:00", "2026-10-21T19:00:00+01:00")]})
    google = calendar._session
    google.answers[LIST] = [_calendar_list(_entry("calendar-personal", "Personal", primary=True),
                                           _entry("calendar-work", "Work")),
                            _calendar_list(_entry("calendar-personal", "Personal", primary=True),
                                           _entry("calendar-work", "Work"), _entry("calendar-gym", "Gym"))]
    intents = LocalIntents(activities=calendar.activity_words)
    calendar.calendars()
    # Until Google lists it, "gym" is Claude's.
    assert intents.match("When's gym tomorrow?") is None

    calendar.calendars(max_age=0)
    tomorrow = date.today() + timedelta(days=1)
    [action] = intents.match("When's gym tomorrow?")["actions"]
    assert (action.tool, action.params) == ("get_activity", {"activity": "gym", "start": tomorrow.isoformat(),
                                                             "days": 1})
    assert calendar.activity("gym", date(2026, 10, 21), today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got gym from 6:00 PM until 7:00 PM."
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-gym"]


def test_the_primary_calendar_isnt_an_activity(make_calendar, tmp_path):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar.calendars()
    assert calendar.activity_calendars() == {
        "work": {"id": "calendar-work", "name": "Work", "primary": False, "writable": True,
                 "timeZone": "Europe/London"}}

    # Until it's switched on in calendars.json - and Work, switched off there, isn't one.
    calendar.choices = _choices(tmp_path, {"calendar-personal": True, "calendar-work": False})
    assert list(calendar.activity_calendars()) == ["personal"]
    assert calendar.activity("work", date(2026, 10, 21)) == {
        "success": False, "message": "I haven't got a calendar called work."}


def test_an_activity_nova_hasnt_got_says_so(make_calendar):
    # It used to say to add it to home_assistant.activities - now there's nothing to edit.
    calendar = make_calendar()
    assert calendar.activity("gym", date(2026, 10, 21)) == {
        "success": False, "message": "I haven't got a calendar called gym."}
    assert calendar.activity("Gym ", date(2026, 10, 21))["message"] == "I haven't got a calendar called Gym."
    assert calendar.activity("", date(2026, 10, 21)) == {
        "success": False, "message": "I didn't catch which of your calendars you meant."}
    assert _read(calendar) == []


def test_uni_sessions_back_to_back_are_said_as_one_stretch(make_calendar):
    # From the user's calendar: a practical, a tutorial, then a lecture straight after it.
    calendar = make_calendar(calendars={"calendar-personal": UNI_DAY[3:], "calendar-uni": UNI_DAY[:3]})
    assert calendar.activity("uni", date(2026, 10, 20), today=date(2026, 10, 20))["message"] == \
        "Today, you've got uni from 11:30 AM until 12:30 PM, and from 2:30 PM until 5:30 PM."


def test_a_day_off_says_so(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []}, reads=2)
    assert calendar.activity("work", date(2026, 10, 24), 2, today=date(2026, 10, 20))["message"] == \
        "You haven't got work between Saturday and Sunday."
    assert calendar.activity("work", date(2026, 10, 21), today=date(2026, 10, 20))["message"] == \
        "You haven't got work tomorrow."


def test_split_shifts_and_all_day_work_are_both_said():
    from voice_assistant.google_calendar import describe_activity
    events = [{"summary": "Shift", "start": datetime(2026, 10, 22, 9, 0), "end": datetime(2026, 10, 22, 13, 0),
               "all_day": False},
              {"summary": "Shift", "start": datetime(2026, 10, 22, 14, 0), "end": datetime(2026, 10, 22, 18, 0),
               "all_day": False},
              {"summary": "Shift", "start": date(2026, 10, 23), "end": None, "all_day": True}]
    assert describe_activity("work", events, date(2026, 10, 22), 2, TODAY) == (
        "On Thursday, you've got work from 9:00 AM until 1:00 PM, and from 2:00 PM until 6:00 PM. "
        "On Friday, you've got work all day.")


# Google's own calendars - a country's holidays, the contacts' birthdays - with the ids it gives them.
HOLIDAYS = "en.uk#holiday@group.v.calendar.google.com"
BIRTHDAYS = "addressbook#contacts@group.v.calendar.google.com"


def test_googles_own_calendars_arent_activities(make_calendar, tmp_path):
    # Every non-primary calendar was an activity, so "Christmas Day" was said as
    # "holidays in united kingdom", and everyone's birthday as "birthdays".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry(HOLIDAYS, "Holidays in United Kingdom", access_role="reader"),
        _entry(BIRTHDAYS, "Birthdays", access_role="reader"))]
    calendar._session.answers[_events_url(HOLIDAYS)] = [_events_page(
        _google_event("Christmas Day", "2026-12-25", "2026-12-26"))]
    calendar._session.answers[_events_url(BIRTHDAYS)] = [_events_page(
        _google_event("Sam's birthday", "2026-12-25", "2026-12-26"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page()]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page()]

    assert calendar.agenda(date(2026, 12, 25), 1, today=date(2026, 12, 24))["message"] == \
        "Tomorrow, you've got Christmas Day, all day, and Sam's birthday, all day."
    assert list(calendar.activity_calendars()) == ["work"]
    # Unless switched on in calendars.json.
    calendar.choices = _choices(tmp_path, {HOLIDAYS: True})
    assert list(calendar.activity_calendars()) == ["work", "holidays in united kingdom"]


# Calendars named by an email address - the user's own primary, as Google names it, and
# another person's shared with them. Both addresses are made up.
OWN_ADDRESS = "me@example.com"
THEIR_ADDRESS = "someone@example.com"


def _named_by_addresses(make_calendar, tmp_path, activities=None):
    """A calendar whose primary and a shared one are named by addresses, beside Work - each with a day on."""
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []},
                             activities=activities)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-theirs", THEIR_ADDRESS, access_role="reader"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page(
        _google_event("Dentist", "2026-10-21T08:00:00+01:00", "2026-10-21T08:30:00+01:00"))]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page(
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"))]
    calendar._session.answers[_events_url("calendar-theirs")] = [_events_page(
        _google_event("Office", "2026-10-21T09:00:00+01:00", "2026-10-21T17:00:00+01:00"))]
    return calendar


@pytest.mark.parametrize("name", [
    OWN_ADDRESS, "ME@EXAMPLE.COM", "first.last+cal@sub.example.co.uk", "a_b-c@x-y.io", "user@xn--p1ai.xn--p1ai",
    "\u540d\u524d@\u4f8b\u3048.\u65e5\u672c", " someone@example.com ", "Alice's calendar (alice@example.com)",
])
def test_a_name_with_an_email_address_in_it_is_named_by_an_address(name):
    # The privacy rule: a name holding a real address is never said, or told to Claude.
    assert named_by_address({"name": name})
    assert spoken_name({"name": name, "primary": True}) == "your calendar"
    assert spoken_name({"name": name, "primary": False}) == "a shared calendar"


@pytest.mark.parametrize("name", ["Gym @ 6", "Gym @6", "Gym@6", "Gym@6.30", "Meet @ 6.30pm", "Dinner @ Mario's.",
                                  "@", "Work @ Tesco", "", "Gym @ 6, me@"])
def test_a_name_with_an_at_sign_but_no_address_is_said_as_it_is(name):
    assert not named_by_address({"name": name})
    assert spoken_name({"name": name, "primary": False}) == name


def test_a_calendar_called_gym_at_six_is_an_activity_said_by_its_name(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-gym": []},
                             activities={"calendar-gym": True}, reads=2)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-gym", "Gym @ 6"))]
    calendar._session.answers[_events_url("calendar-gym")] = [_events_page(
        _google_event("Class", "2026-10-21T18:00:00+01:00", "2026-10-21T19:00:00+01:00"))] * 2
    assert [event["calendar"] for event in calendar.events(date(2026, 10, 21), 1)] == ["Gym @ 6"]
    assert list(calendar.activity_calendars()) == ["gym @ 6"]
    assert "gym 6" in calendar.activity_words()
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == (
        "Tomorrow, you've got gym @ 6 from 6:00 PM until 7:00 PM.")


def test_a_calendar_named_by_an_address_isnt_an_activity(make_calendar, tmp_path, caplog):
    # Another person's calendar shared with the user is named by their email address. As
    # an activity it went to Claude in get_activity's description, and was said aloud.
    calendar = _named_by_addresses(make_calendar, tmp_path)
    with caplog.at_level(logging.INFO):
        message = calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"]
    assert message == ("Tomorrow, you've got Dentist at 8:00 AM, Office at 9:00 AM, and work from 3:30 PM "
                       "until 10:00 PM.")
    assert list(calendar.activity_calendars()) == ["work"]
    assert not any("example" in word for word in calendar.activity_words())
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_a_calendar_named_by_an_address_switched_on_is_said_without_it(make_calendar, tmp_path, caplog):
    calendar = _named_by_addresses(make_calendar, tmp_path,
                                   activities={"calendar-personal": True, "calendar-theirs": True})
    with caplog.at_level(logging.INFO):
        message = calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"]
    assert message == ("Tomorrow, you've got your calendar from 8:00 AM until 8:30 AM, a shared calendar from "
                       "9:00 AM until 5:00 PM, and work from 3:30 PM until 10:00 PM.")
    assert list(calendar.activity_calendars()) == ["your calendar", "work", "a shared calendar"]
    # Nor can it be asked about by name - "your" and "a" would be heard as activities.
    assert not any("example" in word or word.split()[0] in ("your", "a") for word in calendar.activity_words())
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_an_events_calendar_is_never_given_by_an_address(make_calendar, tmp_path):
    calendar = _named_by_addresses(make_calendar, tmp_path)
    found = calendar.events(date(2026, 10, 21), 1)
    assert {event["calendar_id"]: event["calendar"] for event in found} == {
        "calendar-personal": "your calendar", "calendar-work": "Work", "calendar-theirs": "a shared calendar"}


def test_an_all_day_event_on_an_activity_calendar_is_said_by_its_own_name(make_calendar):
    # An activity is a stretch of time - a training day on the Work calendar isn't "work, all day".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": [
        _google_event("Training day", "2026-10-21", "2026-10-22"),
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00")]})
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got Training day, all day, and work from 3:30 PM until 10:00 PM."


def test_the_agenda_takes_an_activity_by_its_calendar_not_its_name(make_calendar, tmp_path):
    # The user's own calendar, called Work too, isn't the Work activity.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[LIST] = [_calendar_list(_entry("calendar-personal", "Work", primary=True),
                                                      _entry("calendar-work", "Work"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page(
        _google_event("Dentist", "2026-10-21T09:00:00+01:00", "2026-10-21T10:00:00+01:00"))]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page(
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"))]
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got Dentist at 9:00 AM, and work from 3:30 PM until 10:00 PM."


def test_shifts_and_working_are_work_even_beside_a_calendar_starting_with_them(make_calendar):
    calendar, _ = _heard(make_calendar, "Work", "Working group", "Shift swaps")
    words = calendar.activity_words()
    assert words["working"] == "work" and words["shift"] == "work" and words["shifts"] == "work"
    assert words["working group"] == "working group" and words["shift swaps"] == "shift swaps"


def test_two_names_that_tidy_alike_are_the_first_listed(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": []})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-work-again", "Work!"))]
    calendar.calendars()
    assert calendar.activity_words()["work"] == "work"


def _heard(make_calendar, *names):
    """LocalIntents hearing the activities of calendars with these names, beside the user's own."""
    calendar = make_calendar(calendars={"calendar-personal": [], **{
        f"calendar-{name.lower().replace(' ', '-')}": [] for name in names}})
    calendar.calendars()
    return calendar, LocalIntents(activities=calendar.activity_words)


def test_the_same_words_still_mean_work_and_uni(make_calendar):
    calendar, intents = _heard(make_calendar, "Uni", "Work")
    today = date.today()

    def asked(text):
        [action] = intents.match(text)["actions"]
        return action.tool, action.params

    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    assert asked("Am I working on Saturday?") == ("get_activity", {"activity": "work", "start": saturday.isoformat(),
                                                                   "days": 1})
    assert asked("When's my next lecture?") == ("get_activity", {"activity": "uni", "start": today.isoformat(),
                                                                 "count": 1})
    assert asked("What are my next three shifts?") == ("get_activity", {"activity": "work",
                                                                        "start": today.isoformat(), "count": 3})
    assert calendar.activity_words()["classes"] == "uni" and calendar.activity_words()["works"] == "work"


@pytest.mark.parametrize("text, heard", [
    # A first word alone, in a bare shape, is an everyday question - plan 1's sunset and home.
    ("When is sunset?", None), ("When's sunset?", None), ("Am I at home?", None), ("Am I home?", None),
    # With a day, or the whole name, or a shape that plainly names an activity, it's the calendar.
    ("When's sunset tomorrow?", "sunset yoga"), ("Am I at home on Saturday?", "home group"),
    ("When is sunset yoga?", "sunset yoga"), ("What time do I have sunset?", "sunset yoga"),
    ("When's my next sunset?", "sunset yoga"),
])
def test_a_first_word_alone_isnt_heard_in_a_bare_question(make_calendar, text, heard):
    _, intents = _heard(make_calendar, "Sunset yoga", "Home group")
    result = intents.match(text)
    assert (result["actions"][0].params["activity"] if result else None) == heard


@pytest.mark.parametrize("name, text, heard", [
    ("C++ club", "What time do I have c club tomorrow?", "c++ club"),
    ("C++ club", "What's the time?", None),
    ("C++ club", "Have I got a cold?", None),
    ("A", "What time do I have a tomorrow?", "a"),
    ("A", "Have I got a meeting today?", None),
    ("A", "Set a timer for ten minutes.", None),
    ("A", "Am I at a loss?", None),
])
def test_a_calendar_named_oddly_is_still_heard_strictly(make_calendar, name, text, heard):
    _, intents = _heard(make_calendar, name)
    result = intents.match(text)
    activities = [action.params["activity"] for action in (result or {}).get("actions", [])
                  if action.tool == "get_activity"]
    assert activities == ([heard] if heard else [])


def test_a_calendar_of_two_words_is_heard_by_its_first(make_calendar):
    calendar, intents = _heard(make_calendar, "Uni timetable")
    assert calendar.activity_words() == {
        "uni timetable": "uni timetable", "uni timetables": "uni timetable", "uni": "uni timetable",
        "university": "uni timetable", "lecture": "uni timetable", "lectures": "uni timetable",
        "class": "uni timetable", "classes": "uni timetable"}
    [action] = intents.match("What time do I have uni tomorrow?")["actions"]
    assert action.params["activity"] == "uni timetable"

    # Not once "Uni clubs" is there too - which of them would "uni" be?
    calendar, intents = _heard(make_calendar, "Uni timetable", "Uni clubs")
    assert "uni" not in calendar.activity_words() and "lecture" not in calendar.activity_words()
    assert intents.match("What time do I have uni tomorrow?") is None
    [action] = intents.match("What time do I have uni clubs tomorrow?")["actions"]
    assert action.params["activity"] == "uni clubs"


# The next time

def _shift(day, start_hour, end_hour):
    return {"summary": "Shift", "start": datetime(2026, 10, day, start_hour, 0, tzinfo=LONDON),
            "end": datetime(2026, 10, day, end_hour, 0, tzinfo=LONDON), "all_day": False}


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LONDON)
SHIFTS = [_shift(20, 8, 11), _shift(21, 15, 23), _shift(24, 14, 22), _shift(25, 14, 21)]


def test_the_next_time_is_just_the_one():
    # Asked "when do I next have work?", every shift for a fortnight used to be read out.
    from voice_assistant.google_calendar import describe_next
    # This morning's shift is over, so it's tomorrow's.
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), NOW) == \
        "You're next at work tomorrow, from 3:00 PM until 11:00 PM."


def test_the_next_few_times_are_said_together():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("work", SHIFTS, 3, date(2026, 10, 20), NOW) == (
        "Your next 3 times at work: tomorrow, from 3:00 PM until 11:00 PM; on Saturday, from 2:00 PM until "
        "10:00 PM; and on Sunday, from 2:00 PM until 9:00 PM.")


def test_being_there_now_says_so():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), datetime(2026, 10, 20, 9, 30, tzinfo=LONDON)) == \
        "You're at work now, until 11:00 AM."


def test_nothing_coming_up_says_so():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("uni", [], 1, date(2026, 10, 20), NOW) == "You haven't got uni in the next two weeks."


# Adding to the calendar

SATURDAY = date(2026, 10, 24)


@pytest.fixture
def for_adding(monkeypatch, tmp_path):
    """
    Makes GoogleCalendars to add to: Personal, the user's own and primary, and Work, which
    they own too - and Uni, shared with them, which Google won't let Nova add to. Each
    calendar takes one event, unless answers gives what Google answers.
    """
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)

    def make(new_events=None, answers=None, primary="Personal"):
        entries = [_entry("calendar-personal", primary, primary=True), _entry("calendar-work", "Work"),
                   _entry("calendar-uni", "Uni", access_role="reader")]
        google = FakeGoogle(**{LIST: [_calendar_list(*entries)],
                               **{_events_url(entry["id"]): list(answers or [_inserted()]) for entry in entries}})
        choices = _choices(tmp_path)
        if new_events:
            choices.change(lambda state: state.update(new_events=new_events))
        return GoogleCalendar(FakeSignIn(), choices, session=google)

    return make


def _inserted():
    """Google's answer to an event added - the event, as events.insert gives it back."""
    return {"kind": "calendar#event", "id": "made-up-event", "status": "confirmed"}


def _added(calendar):
    """Each event sent to be added, as (calendar id, what was sent)."""
    prefix = "https://www.googleapis.com/calendar/v3/calendars/"
    return [(posted["url"][len(prefix):-len("/events")], posted["json"]) for posted in calendar._session.posted]


def _said(reply):
    """A reply as it's said - without the "undo" a change has."""
    return {key: value for key, value in reply.items() if key != "undo"}


def test_a_new_event_goes_to_the_primary_calendar_and_lasts_an_hour(for_adding):
    calendar = for_adding()

    assert _said(calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)) == {
        "success": True, "message": "Added Dentist, Thursday the 29th at 3 PM."}
    # The clocks went back on the 25th, so the 29th is on GMT.
    assert _added(calendar) == [("calendar-personal", {"summary": "Dentist",
                                                       "start": {"dateTime": "2026-10-29T15:00:00+00:00"},
                                                       "end": {"dateTime": "2026-10-29T16:00:00+00:00"}})]
    [posted] = calendar._session.posted
    assert posted["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert posted["timeout"] == google_calendar.REQUEST_TIMEOUT
    assert google_calendar.DEFAULT_LENGTH == timedelta(hours=1)

    # Before they went back, it's on BST, and an end given is kept.
    calendar = for_adding()
    assert calendar.add("Coffee with Sam", "2026-10-24 10:30", end="2026-10-24 11:15", today=SATURDAY)["message"] \
        == "Added Coffee with Sam, Saturday the 24th at 10:30 AM."
    assert _added(calendar)[0][1]["start"] == {"dateTime": "2026-10-24T10:30:00+01:00"}
    assert _added(calendar)[0][1]["end"] == {"dateTime": "2026-10-24T11:15:00+01:00"}


def test_with_no_time_its_all_day(for_adding):
    calendar = for_adding()
    assert _said(calendar.add("Holiday", "2026-10-30", today=SATURDAY)) == {
        "success": True, "message": "Added Holiday, Friday the 30th, all day."}
    assert _added(calendar)[0][1] == {"summary": "Holiday", "start": {"date": "2026-10-30"},
                                      "end": {"date": "2026-10-31"}}

    # all_day makes a time all day too, and an end day is included.
    calendar = for_adding()
    calendar.add("Holiday", "2026-10-30 09:00", end="2026-11-01", all_day=True, today=SATURDAY)
    assert _added(calendar)[0][1] == {"summary": "Holiday", "start": {"date": "2026-10-30"},
                                      "end": {"date": "2026-11-02"}}


def test_a_time_that_cant_be_read_or_ends_first_isnt_added(for_adding):
    calendar = for_adding()
    # Only exactly "YYYY-MM-DD HH:MM" or "YYYY-MM-DD" - "20261029" was a time, at midnight.
    for start, end in (("Thursday at 3", None), ("2026-10-29 15:00", "about four"), ("", None), ("20261029", None),
                       ("2026-10-29T15:00", None), ("2026-10-29 15:00+01:00", None), ("2026-W44-4", None),
                       ("2026-10-29 15", None), ("2026-10-29 3:00", None), (None, None)):
        assert calendar.add("Dentist", start, end=end, today=SATURDAY) == {
            "success": False, "message": "I didn't catch when that is."}
    for start, end in (("2026-10-29 15:00", "2026-10-29 14:00"), ("2026-10-30", "2026-10-29"),
                       ("2026-10-29 15:00", "2026-10-29 15:00")):
        assert calendar.add("Dentist", start, end=end, today=SATURDAY) == {
            "success": False, "message": "That ends before it starts."}
    assert calendar.add("  ", "2026-10-29 15:00", today=SATURDAY) == {
        "success": False, "message": "I didn't catch what to add."}
    assert calendar._session.posted == []


def test_a_calendar_named_is_used(for_adding):
    for named in ("work", "Work", "shifts"):
        calendar = for_adding()
        assert _said(calendar.add("Shift", "2026-10-31 09:00", calendar=named, today=SATURDAY)) == {
            "success": True, "message": "Added Shift to Work, Saturday the 31st at 9 AM."}, named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work"]

    # The one new events go to anyway isn't named.
    calendar = for_adding()
    assert calendar.add("Dentist", "2026-10-29 15:00", calendar="personal", today=SATURDAY)["message"] == \
        "Added Dentist, Thursday the 29th at 3 PM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"]

    # The user's own, whatever Google calls it.
    for named in ("my calendar", "primary", "personal", "my own calendar", "Nathaniel"):
        calendar = for_adding(primary="Nathaniel")
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
            "Added Dentist, Thursday the 29th at 3 PM.", named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"], named
    # "My work calendar" is still Work.
    calendar = for_adding()
    calendar.add("Shift", "2026-10-31 09:00", calendar="my work calendar", today=SATURDAY)
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work"]


def test_a_calendar_whose_name_is_no_words_is_never_the_one_named(for_adding):
    # "My calendar" is "" once "my" and "calendar" are taken off, as is a name of emoji
    # alone - so "add it to my calendar" went to the party calendar, and said "to 🎉".
    for named in ("my calendar", "calendar", "the calendar"):
        calendar = for_adding()
        calendar._session.answers[LIST] = [_calendar_list(
            _entry("calendar-personal", "Personal", primary=True), _entry("calendar-party", "🎉"))]
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
            "Added Dentist, Thursday the 29th at 3 PM.", named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"], named


def test_the_chosen_calendar_is_used_until_its_gone(for_adding):
    calendar = for_adding(new_events="calendar-work")
    assert calendar.add("Shift", "2026-10-31 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 31st at 9 AM."
    # The primary, named, is now the one that isn't where new events go.
    assert calendar.add("Dentist", "2026-10-29 15:00", calendar="personal", today=SATURDAY)["message"] == \
        "Added Dentist to Personal, Thursday the 29th at 3 PM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work", "calendar-personal"]

    # Deleted in Google, new events go to the primary again.
    calendar._known_at = None
    calendar._session.answers[LIST].append(_calendar_list(_entry("calendar-personal", "Personal", primary=True)))
    calendar._session.answers[_events_url("calendar-personal")].append(_inserted())
    assert calendar.add("Shift", "2026-11-07 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 7th of November at 9 AM."
    assert _added(calendar)[-1][0] == "calendar-personal"

    # And one chosen that Nova can't add to is never tried.
    calendar = for_adding(new_events="calendar-uni")
    calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"]


def test_new_events_only_go_to_a_calendar_nova_can_add_to(for_adding):
    calendar = for_adding()
    calendar.calendars()
    with pytest.raises(ValueError, match="^Nova can only add to your own calendars.$"):
        calendar.set_new_events("calendar-uni")
    with pytest.raises(ValueError, match="^Google isn't showing that calendar just now.$"):
        calendar.set_new_events("calendar-made-up")
    assert calendar.choices.get("new_events") is None


def test_new_events_go_to_the_primary_until_another_is_chosen(for_adding):
    calendar = for_adding()
    calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)
    # Used from the next one, with no restart.
    calendar.set_new_events("calendar-work")
    assert calendar.add("Shift", "2026-10-31 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 31st at 9 AM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal", "calendar-work"]
    assert calendar.choices.get("new_events") == "calendar-work"


def test_a_calendar_nova_cant_add_to_says_so(for_adding):
    calendar = for_adding()
    assert calendar.add("Lecture", "2026-10-29 15:00", calendar="uni", today=SATURDAY) == {
        "success": False, "message": "I can't add to Uni - Google only lets me add to your own calendars."}
    assert calendar.add("Gym", "2026-10-29 15:00", calendar="gym", today=SATURDAY) == {
        "success": False, "message": "I haven't got a calendar called that."}
    assert calendar._session.posted == []


def test_a_calendar_named_by_an_address_is_never_said_when_adding(for_adding, caplog):
    # Google names the user's own primary by their email address.
    calendar = for_adding(new_events="calendar-work", primary=OWN_ADDRESS, answers=[_inserted()] * 3)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-theirs", THEIR_ADDRESS, access_role="reader"),
        _entry("calendar-mine-too", "other@example.com"))]
    calendar._session.answers[_events_url("calendar-mine-too")] = [_inserted()]
    with caplog.at_level(logging.INFO):
        for named in ("my calendar", "personal", OWN_ADDRESS):
            assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
                "Added Dentist to your calendar, Thursday the 29th at 3 PM.", named
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar="other@example.com", today=SATURDAY)["message"] \
            == "Added Dentist to a shared calendar, Thursday the 29th at 3 PM."
        assert calendar.add("Lecture", "2026-10-29 15:00", calendar=THEIR_ADDRESS, today=SATURDAY) == {
            "success": False, "message": "I can't add to a shared calendar - Google only lets me add to your own "
                                         "calendars."}
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"] * 3 + ["calendar-mine-too"]
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_an_event_next_month_says_which_month(for_adding):
    calendar = for_adding(answers=[_inserted()] * 5)
    assert calendar.add("Dentist", "2026-11-02 10:30", today=date(2026, 10, 30))["message"] == \
        "Added Dentist, Monday the 2nd of November at 10:30 AM."
    assert calendar.add("Dentist", "2026-11-02", today=date(2026, 11, 1))["message"] == \
        "Added Dentist, Monday the 2nd, all day."
    # The year too, once it's not within the coming twelve months - a year misheard is heard.
    assert calendar.add("Dentist", "2027-10-23 15:00", today=SATURDAY)["message"] == \
        "Added Dentist, Saturday the 23rd of October at 3 PM."
    assert calendar.add("Dentist", "2027-10-24 15:00", today=SATURDAY)["message"] == \
        "Added Dentist, Sunday the 24th of October 2027 at 3 PM."
    assert calendar.add("Dentist", "2028-01-03", today=SATURDAY)["message"] == \
        "Added Dentist, Monday the 3rd of January 2028, all day."


def test_a_day_already_gone_isnt_added(for_adding):
    # Claude getting the week or the year wrong is the likeliest mishearing.
    calendar = for_adding()
    for start in ("2026-10-23 15:00", "2026-10-23", "2025-10-29 15:00"):
        assert calendar.add("Dentist", start, today=SATURDAY) == {
            "success": False, "message": "That day's already gone - which day did you mean?"}, start
    assert calendar._session.posted == []
    # Earlier today is added - something just done, logged.
    assert calendar.add("Run", "2026-10-24 07:00", today=SATURDAY)["message"] == "Added Run, Saturday the 24th at 7 AM."


@pytest.mark.parametrize("zone, start, end", [
    # 1:30 comes twice in London on the 25th - first on BST - and an hour on, it's 1:30 on GMT.
    ("Europe/London", "2026-10-25T01:30:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("UTC", "2026-10-25T01:30:00+00:00", "2026-10-25T02:30:00+00:00"),
])
def test_an_event_as_the_clocks_go_back_lasts_an_hour(for_adding, monkeypatch, zone, start, end):
    # Set here, whatever the run's own zone, so each is checked in both runs.
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    calendar = for_adding()
    assert calendar.add("Night out", "2026-10-25 01:30", today=SATURDAY)["message"] == \
        "Added Night out, Sunday the 25th at 1:30 AM."
    assert _added(calendar)[0][1]["start"] == {"dateTime": start} and _added(calendar)[0][1]["end"] == {"dateTime": end}


def _refused(status, reason="forbidden"):
    return FakeResponse(status, {"error": {"code": status, "errors": [{"domain": "global", "reason": reason}]}})


LOST = "I lost Google partway through, so I can't tell whether Dentist was added."
TROUBLE = "Google had trouble, so I can't tell whether Dentist was added."


@pytest.mark.parametrize("answer, said, posts", [
    # Refused.
    (_refused(403), "Google wouldn't add that.", 1),
    (_refused(400, "invalid"), "Google wouldn't add that.", 1),
    # Google slowing Nova down - as when reading.
    (_refused(403, "rateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    (_refused(403, "userRateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    (_refused(429, "rateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    # Sent, but unanswered - so it may well have been added, and isn't sent again.
    (requests.ReadTimeout("Read timed out."), "Google didn't answer in time, so I can't tell whether Dentist was "
                                              "added.", 1),
    (FakeResponse(200, "not an event"), "Google's answer didn't say, so I can't tell whether Dentist was added.", 1),
    # Google's own trouble - a front end that got no answer from behind it, or a backendError,
    # which Google says to retry - may have come after it was added.
    (_refused(500, "backendError"), TROUBLE, 1),
    (_refused(502, "backendError"), TROUBLE, 1),
    (_refused(503, "backendError"), TROUBLE, 1),
    (FakeResponse(504, "Gateway Timeout"), TROUBLE, 1),
    (requests.ConnectionError(urllib3.exceptions.ProtocolError(
        "Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))), LOST, 1),
    (requests.ConnectionError("Connection aborted."), LOST, 1),
    (requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"), LOST, 1),
    (requests.exceptions.ContentDecodingError("Received response with content-encoding: gzip"), LOST, 1),
    # Never sent, so certainly not added.
    (requests.ConnectTimeout("Connection timed out."), CANT_REACH_GOOGLE, 1),
    (requests.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/calendar/v3", reason=(
        urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")))), CANT_REACH_GOOGLE, 1),
    # Turned down before it ran out, the access token is got afresh once - Google added nothing.
    (_refused(401, "authError"), NEEDS_SIGN_IN, 2),
])
def test_nothing_is_said_added_unless_google_took_it(for_adding, caplog, answer, said, posts):
    calendar = for_adding(answers=[answer, answer])
    with caplog.at_level(logging.INFO):
        assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {"success": False, "message": said}
    assert len(calendar._session.posted) == posts
    # Neither the calendar's id - for the primary, the user's email address - nor the event is logged.
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)


def test_adding_without_google_says_why(for_adding, monkeypatch, token_file):
    calendar = for_adding()
    monkeypatch.setattr(google_calendar.network, "online", lambda: False)
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {
        "success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._session.asked == []

    monkeypatch.setattr(google_calendar.network, "online", lambda: True)
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle())
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {"success": False, "message": NEEDS_SIGN_IN}
    assert calendar._session.posted == []


def test_an_event_added_can_be_taken_back_off(for_adding):
    calendar = for_adding()
    calendar._session.answers[f"DELETE {_event_url('calendar-personal', 'made-up-event')}"] = [DELETED]
    reply = calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)
    assert callable(reply["undo"])
    assert reply["undo"]() == {"success": True, "message": "Took Dentist back off your calendar."}
    # By the id Google gave the event it added, on the calendar it was added to.
    assert calendar._session.sent[-1] == _write("DELETE", _event_url("calendar-personal", "made-up-event"))

    # Sent once, and an answer that doesn't say whether it went is said as that.
    calendar._session.answers[f"DELETE {_event_url('calendar-work', 'made-up-event')}"] = [
        requests.ReadTimeout("Read timed out."), DELETED]
    reply = calendar.add("Shift", "2026-10-31 09:00", calendar="work", today=SATURDAY)
    assert reply["undo"]() == {"success": False, "message": "I can't tell whether Shift was taken off."}
    assert [(sent["method"], sent["url"]) for sent in calendar._session.sent[2:]] == [
        ("POST", _events_url("calendar-work")), ("DELETE", _event_url("calendar-work", "made-up-event"))]


def test_every_write_to_google_is_sent_once_and_tells_nobody():
    # Adding, removing and putting back all go through _send_once, the one request that writes
    # to Google - sent once, with sendUpdates=none, which the tells_nobody fixture checks of
    # every write in this file's tests.
    source = inspect.getsource(google_calendar)
    assert source.count(".request(") == 1 and ".request(" in inspect.getsource(GoogleCalendar._send_once)
    # The only thing posted otherwise is for a token.
    assert source.count(".post(") == 1 and ".post(" in inspect.getsource(GoogleSignIn._ask_for_token)
    assert not re.search(r"\.(patch|put|delete)\(", source)
    assert {name for name in vars(FakeGoogle) if not name.startswith("_")} == {"get", "post", "request"}


# Changing what's on the calendar

# Asked on Saturday the 21st of November 2026 - on GMT, which Google gives in London with a Z.
NOVEMBER_21 = date(2026, 11, 21)
THURSDAY = date(2026, 11, 26)

# Google's answer to a delete: a 204, with nothing in it.
DELETED = FakeResponse(204, None)


def _event_url(calendar_id, event_id):
    return f"{_events_url(calendar_id)}/{quote(event_id, safe='')}"


def _write(method, url, json=None):
    """A write as FakeGoogle notes it - telling nobody, as every write does."""
    return {"method": method, "url": url, "params": {"sendUpdates": "none"}, "json": json}


def _own(summary, start, end, event_id, calendar_id="calendar-personal", **more):
    """One of the user's own events, as events.list gives it - organised by the calendar it's on."""
    return _google_event(summary, start, end, id=event_id, organizer={"email": calendar_id, "self": True}, **more)


def _occurrence(summary, series_id, start, end, meant=None):
    """
    One time a repeating event is on, as events.list gives it with singleEvents: an id of its
    own - its series' and the time it was meant to be, in UTC - its series' id, and that time.
    """
    meant = meant or start
    stamp = meant.replace("-", "") if len(meant) == 10 else \
        datetime.fromisoformat(meant).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _own(summary, start, end, f"{series_id}_{stamp}", recurringEventId=series_id, originalStartTime=_when(meant))


def _series(summary, series_id, start, end, *rules, **more):
    """A repeating event itself, as events.get gives it - its first time, and its rules."""
    return _own(summary, start, end, series_id, recurrence=list(rules), **more)


# Gym every Tuesday at 6 PM from the 3rd of November, ten times, but not the 10th.
GYM_RULES = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10", "EXDATE;TZID=Europe/London:20261110T180000"]
GYM = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *GYM_RULES)
GYM_24TH = _occurrence("Gym", "made-up-gym", "2026-11-24T18:00:00Z", "2026-11-24T19:00:00Z")
GYM_1ST = _occurrence("Gym", "made-up-gym", "2026-12-01T18:00:00Z", "2026-12-01T19:00:00Z")
DENTIST_THURSDAY = _own("Dentist", "2026-11-26T15:00:00Z", "2026-11-26T16:00:00Z", "made-up-dentist")


@pytest.fixture
def for_changing(monkeypatch, tmp_path):
    """
    Makes GoogleCalendars to change events on: Personal, the user's own and primary, and Work,
    which they own too - and Uni, shared with them, which Nova can only read. Each has the
    events given, as events.list gives them with singleEvents, read reads times over; Google
    answers anything else - a series read by its address, or a write by "METHOD address" -
    from answers.
    """
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)

    def make(personal=(), work=(), uni=(), answers=None, reads=1):
        entries = [_entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
                   _entry("calendar-uni", "Uni", access_role="reader")]
        events = {"calendar-personal": personal, "calendar-work": work, "calendar-uni": uni}
        google = FakeGoogle(**{LIST: [_calendar_list(*entries)],
                               **{_events_url(calendar_id): [_events_page(*its_events)] * reads
                                  for calendar_id, its_events in events.items()},
                               **(answers or {})})
        return GoogleCalendar(FakeSignIn(), _choices(tmp_path), session=google)

    return make


def test_an_event_is_removed_and_said_back(for_changing, caplog):
    dentist = _event_url("calendar-personal", "made-up-dentist")
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"DELETE {dentist}": [DELETED]})
    with caplog.at_level(logging.INFO):
        reply = calendar.remove("dentist", today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Dentist, Thursday at 3 PM."}
    assert callable(reply["undo"])
    assert calendar._session.sent == [_write("DELETE", dentist)]
    # Neither the calendar's id - for the primary, the user's email address - nor the event is logged.
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)

    # On the day given, and on Work, which the user owns too. One that doesn't repeat is just
    # removed, from this one on or not.
    shift = _own("Shift", "2026-11-26T09:00:00Z", "2026-11-26T17:00:00Z", "made-up-shift", calendar_id="calendar-work")
    calendar = for_changing(work=[shift], answers={f"DELETE {_event_url('calendar-work', 'made-up-shift')}": [DELETED]})
    assert _said(calendar.remove("shift", day=THURSDAY, following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Removed Shift, Thursday at 9 AM."}
    assert calendar._session.sent == [_write("DELETE", _event_url("calendar-work", "made-up-shift"))]

    holiday = _own("Holiday", "2026-11-22", "2026-11-23", "made-up-holiday")
    calendar = for_changing(personal=[holiday],
                            answers={f"DELETE {_event_url('calendar-personal', 'made-up-holiday')}": [DELETED]})
    assert _said(calendar.remove("holiday", today=NOVEMBER_21)) == {
        "success": True, "message": "Removed Holiday, tomorrow, all day."}


def test_several_matches_remove_nothing_and_ask_which(for_changing):
    personal = [_own("Dentist", "2026-11-23T09:00:00Z", "2026-11-23T09:30:00Z", "made-up-dentist-1"), DENTIST_THURSDAY,
                _own("Dentist check-up", "2026-12-01T11:30:00Z", "2026-12-01T12:00:00Z", "made-up-dentist-3")]
    work = [_own("Dentist", "2026-11-27T11:30:00Z", "2026-11-27T12:00:00Z", "made-up-dentist-2",
                 calendar_id="calendar-work")]
    other_gym = _occurrence("Gym", "made-up-gym-class", "2026-11-26T18:00:00Z", "2026-11-26T19:00:00Z")
    for asked, events, said in (
            # On more than one calendar, each is named by its calendar too.
            (("dentist", False), {"personal": personal, "work": work},
             "I found 4: Dentist on Monday, at 9 AM, on Personal; Dentist on Thursday, at 3 PM, on Personal; "
             "Dentist on Friday, at 11:30 AM, on Work; and 1 more. Which one?"),
            (("dentist", False), {"personal": personal[:2]},
             "I found 2: Dentist on Monday, at 9 AM; and Dentist on Thursday, at 3 PM. Which one?"),
            # A repeating event's times are several, unless it's from this one on - and two series are
            # several too, unless it's all of them.
            (("gym", False), {"personal": [GYM_24TH, GYM_1ST]},
             "I found 2: Gym on Tuesday, at 6 PM; and Gym on Tuesday the 1st of December, at 6 PM. Which one?"),
            (("gym", False), {"personal": [GYM_24TH, other_gym, GYM_1ST]},
             "I found 3: Gym on Tuesday, at 6 PM; Gym on Thursday, at 6 PM; and Gym on Tuesday the 1st of "
             "December, at 6 PM. Which one?")):
        calendar = for_changing(**events)
        search, following = asked
        # Nothing changed, so there's nothing to undo.
        assert calendar.remove(search, following=following, today=NOVEMBER_21) == {"success": False, "message": said}
        assert calendar._session.sent == []


def test_nothing_found_says_where_it_looked(for_changing):
    calendar = for_changing(personal=[DENTIST_THURSDAY], reads=3)
    assert calendar.remove("optician", day=THURSDAY, today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the optician on your calendar on Thursday."}
    assert calendar.remove("optician", day=date(2026, 11, 22), today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the optician on your calendar tomorrow."}
    assert calendar.remove("optician", today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the optician on your calendar."}
    # On every calendar, the day given, else from today over the three months find looks over.
    assert google_calendar.SEARCH_DAYS == 90
    assert [calendar_id for calendar_id, _ in _read(calendar)] == \
        ["calendar-personal", "calendar-work", "calendar-uni"] * 3
    assert [(params["timeMin"], params["timeMax"]) for _, params in _read(calendar)] == \
        [("2026-11-26T00:00:00+00:00", "2026-11-27T00:00:00+00:00")] * 3 + \
        [("2026-11-22T00:00:00+00:00", "2026-11-23T00:00:00+00:00")] * 3 + \
        [("2026-11-21T00:00:00+00:00", "2027-02-19T00:00:00+00:00")] * 3
    # Nothing heard to look for isn't looked for.
    assert calendar.remove("  ", today=NOVEMBER_21) == {
        "success": False, "message": "I didn't catch which event you meant."}
    assert len(_read(calendar)) == 9 and calendar._session.sent == []


def test_a_time_picks_the_event_at_it(for_changing):
    events = [_own("Dentist", "2026-11-26", "2026-11-27", "made-up-dentist-day"),
              _own("Dentist", "2026-11-26T09:00:00Z", "2026-11-26T09:30:00Z", "made-up-dentist-9"),
              _own("Dentist", "2026-11-26T15:00:00Z", "2026-11-26T15:30:00Z", "made-up-dentist-3"),
              _own("Dentist", "2026-11-26T15:30:00Z", "2026-11-26T16:00:00Z", "made-up-dentist-half-3")]
    # Exactly at the time given, else in its hour - "the 9 o'clock one" - and never one all day.
    for at, removed, said in (("15:30", "made-up-dentist-half-3", "3:30 PM"), ("15:00", "made-up-dentist-3", "3 PM"),
                              ("09:30", "made-up-dentist-9", "9 AM")):
        url = _event_url("calendar-personal", removed)
        calendar = for_changing(personal=events, answers={f"DELETE {url}": [DELETED]})
        assert _said(calendar.remove("dentist", day=THURSDAY, at=at, today=NOVEMBER_21)) == {
            "success": True, "message": f"Removed Dentist, Thursday at {said}."}, at
        assert calendar._session.sent == [_write("DELETE", url)], at

    calendar = for_changing(personal=events, reads=3)
    assert calendar.remove("dentist", day=THURSDAY, at="15:45", today=NOVEMBER_21) == {"success": False, "message": (
        "I found 2: Dentist on Thursday, at 3 PM; and Dentist on Thursday, at 3:30 PM. Which one?")}
    assert calendar.remove("dentist", day=THURSDAY, at="12:00", today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the dentist on your calendar on Thursday."}
    assert calendar.remove("dentist", day=THURSDAY, today=NOVEMBER_21)["message"] == (
        "I found 4: Dentist on Thursday, all day; Dentist on Thursday, at 9 AM; Dentist on Thursday, at 3 PM; "
        "and 1 more. Which one?")
    # A time that can't be read isn't guessed at.
    assert calendar.remove("dentist", day=THURSDAY, at="three", today=NOVEMBER_21) == {
        "success": False, "message": "I didn't catch the time."}
    assert calendar._session.sent == []


def test_an_event_on_a_calendar_nova_cant_write_isnt_removed(for_changing):
    lecture = _own("Lecture", "2026-11-26T11:00:00Z", "2026-11-26T12:00:00Z", "made-up-lecture",
                   calendar_id="calendar-uni")
    calendar = for_changing(uni=[lecture])
    assert calendar.remove("lecture", today=NOVEMBER_21) == {
        "success": False, "message": "I can't change that, Google only lets me change your own calendars."}
    assert calendar._session.sent == []


def test_an_invite_is_left_alone(for_changing):
    # On the user's own calendar, but organised by someone else - changing it would tell them.
    dinner = _google_event("Dinner", "2026-11-26T19:00:00Z", "2026-11-26T21:00:00Z", id="made-up-dinner",
                           organizer={"email": "someone@example.com", "displayName": "Someone"})
    for invite in (dinner, {**dinner, "organizer": {"email": "someone@example.com", "self": False}}):
        calendar = for_changing(personal=[invite])
        assert calendar.remove("dinner", today=NOVEMBER_21) == {
            "success": False, "message": "That's an invite from someone else. Change it in Google Calendar."}
        assert calendar._session.sent == []


def test_one_day_of_a_repeating_event_is_removed_by_its_own_id(for_changing):
    tuesday = _event_url("calendar-personal", GYM_24TH["id"])
    calendar = for_changing(personal=[GYM_24TH], answers={f"DELETE {tuesday}": [DELETED], f"PATCH {tuesday}": [GYM_24TH]})
    reply = calendar.remove("gym", day=date(2026, 11, 24), today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym, Tuesday at 6 PM."}
    # Its own id - its series' and its time - so Google removes that day only, and the series isn't read.
    assert GYM_24TH["id"] == "made-up-gym_20261124T180000Z"
    assert calendar._session.sent == [_write("DELETE", tuesday)]
    assert _event_url("calendar-personal", "made-up-gym") not in [url for url, _ in calendar._session.asked]
    # Put back the same way.
    assert reply["undo"]() == {"success": True, "message": "Put Gym back, Tuesday at 6 PM."}
    assert calendar._session.sent[-1] == _write("PATCH", tuesday, {"status": "confirmed"})


def test_from_this_one_on_cuts_the_series_just_before_it(for_changing):
    series = _event_url("calendar-personal", "made-up-gym")
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        series: [GYM], f"{series}/instances": [_events_page()], f"PATCH {series}": [GYM, GYM]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    # From the earliest: ended a second before it, in UTC, so the times before it - and the day it
    # skips - stay as they were.
    assert calendar._session.sent == [_write("PATCH", series, {"recurrence": [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z", "EXDATE;TZID=Europe/London:20261110T180000"]})]
    # Undone, its rules are put back as they were.
    assert reply["undo"]() == {"success": True, "message": "Put Gym back from Tuesday on."}
    assert calendar._session.sent[-1] == _write("PATCH", series, {"recurrence": GYM_RULES})

    # From the day given.
    calendar = for_changing(personal=[GYM_1ST], answers={
        series: [GYM], f"{series}/instances": [_events_page()], f"PATCH {series}": [GYM]})
    assert _said(calendar.remove("gym", day=date(2026, 12, 1), following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Removed Gym from Tuesday the 1st of December on."}
    assert calendar._session.sent[0]["json"]["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T175959Z"


def test_from_the_first_one_on_removes_the_whole_series(for_changing):
    series = _event_url("calendar-personal", "made-up-gym")
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)
    # The first time, moved to 7 that day - it's the first by when it was meant to be, which Google
    # gives with a Z.
    moved = _occurrence("Gym", "made-up-gym", "2026-11-24T19:00:00Z", "2026-11-24T20:00:00Z",
                        meant="2026-11-24T18:00:00Z")
    calendar = for_changing(personal=[moved, GYM_1ST], answers={
        series: [first], f"{series}/instances": [_events_page()], f"DELETE {series}": [DELETED],
        f"PATCH {series}": [first]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    assert calendar._session.sent == [_write("DELETE", series)]
    # Undone, the series is set back to confirmed.
    assert reply["undo"]() == {"success": True, "message": "Put Gym back from Tuesday on."}
    assert calendar._session.sent[-1] == _write("PATCH", series, {"status": "confirmed"})

    # Or, when Google won't, a copy of it is added - with its rules.
    calendar = for_changing(personal=[moved], answers={series: [first], f"{series}/instances": [_events_page()],
                                                       f"DELETE {series}": [DELETED],
                                                       f"PATCH {series}": [_refused(404, "notFound")],
                                                       f"POST {_events_url('calendar-personal')}": [_inserted()]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == {
        "success": True, "message": "Put Gym back from Tuesday on."}
    assert calendar._session.sent[-1] == _write("POST", _events_url("calendar-personal"), {
        "summary": "Gym", "start": first["start"], "end": first["end"], "recurrence": GYM_RULES})


def test_cut_rules_end_a_count_series_and_an_all_day_series_just_before():
    cut = google_calendar._cut_rules
    # Kept by COUNT, cut at a time on BST: 9 AM in London is 8 AM in UTC.
    assert cut(["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10"], datetime(2026, 10, 20, 9, 0, tzinfo=LONDON)) == \
        ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261020T075959Z"]
    # On GMT, as Google gives it - and an UNTIL is replaced, wherever it was.
    assert cut(["RRULE:FREQ=WEEKLY;UNTIL=20270101T000000Z;BYDAY=TU"], datetime.fromisoformat("2026-11-24T09:00:00Z")) \
        == ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T085959Z"]
    # Half past midnight on BST is the day before, in UTC.
    assert cut(["RRULE:FREQ=DAILY;COUNT=30"], datetime(2026, 10, 20, 0, 30, tzinfo=LONDON)) == \
        ["RRULE:FREQ=DAILY;UNTIL=20261019T232959Z"]
    # An all-day series ends the day before, as a date.
    assert cut(["RRULE:FREQ=YEARLY;COUNT=5"], date(2026, 11, 24)) == ["RRULE:FREQ=YEARLY;UNTIL=20261123"]
    assert cut(["RRULE:FREQ=MONTHLY;BYMONTHDAY=1;COUNT=12"], date(2027, 1, 1)) == \
        ["RRULE:FREQ=MONTHLY;BYMONTHDAY=1;UNTIL=20261231"]
    # EXDATE lines are kept as they are, and so are the times an RDATE line adds before the cut.
    assert cut(["EXDATE;VALUE=DATE:20261117", "RRULE:FREQ=WEEKLY;COUNT=10", "RDATE;VALUE=DATE:20261119"],
               date(2026, 11, 24)) == \
        ["EXDATE;VALUE=DATE:20261117", "RRULE:FREQ=WEEKLY;UNTIL=20261123", "RDATE;VALUE=DATE:20261119"]


def test_cut_rules_drop_the_times_an_rdate_adds_from_the_cut_on():
    cut = google_calendar._cut_rules
    rrule = "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10"
    # All day: the 24th, the cut, and the 26th go; the 19th stays - and the line goes if none is left.
    assert cut([rrule, "RDATE;VALUE=DATE:20261119,20261126,20261124"], date(2026, 11, 24)) == [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261123", "RDATE;VALUE=DATE:20261119"]
    assert cut(["RDATE;VALUE=DATE:20261124,20261126", rrule], date(2026, 11, 24)) == [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261123"]
    # Timed, on GMT: a time in its own zone, in UTC, and one with no zone - which is the local one, London here.
    at_nine = datetime(2026, 11, 24, 9, 0, tzinfo=LONDON)
    assert cut(["RDATE;TZID=Europe/London:20261119T180000,20261124T090000,20261124T180000"], at_nine) == [
        "RDATE;TZID=Europe/London:20261119T180000"]
    assert cut(["RDATE:20261124T085959Z,20261124T090000Z"], at_nine) == ["RDATE:20261124T085959Z"]
    assert cut(["RDATE:20261124T080000,20261124T100000"], at_nine) == ["RDATE:20261124T080000"]
    # In another zone, 9 AM on the 24th in London is 4 AM in New York, so 8 AM there is later.
    assert cut(["RDATE;TZID=America/New_York:20261124T030000,20261124T080000"], at_nine) == [
        "RDATE;TZID=America/New_York:20261124T030000"]
    # A period is by its start, and a day against a time is by the day it is on in London.
    assert cut(["RDATE;VALUE=PERIOD:20261119T180000Z/PT1H,20261125T180000Z/PT1H"], at_nine) == [
        "RDATE;VALUE=PERIOD:20261119T180000Z/PT1H"]
    assert cut(["RDATE;VALUE=DATE:20261123,20261124"], at_nine) == ["RDATE;VALUE=DATE:20261123"]
    assert cut(["RDATE:20261123T235959Z,20261124T000000Z"], date(2026, 11, 24)) == ["RDATE:20261123T235959Z"]
    # Times which can't be read are never guessed at.
    for rule in ("RDATE:not a time", "RDATE;TZID=Nowhere/Made_Up:20261119T180000", "RDATE:20261340"):
        with pytest.raises(google_calendar._SeriesUnreadable):
            cut([rule], at_nine)


def test_an_imported_series_removed_from_a_time_on_has_no_rdate_from_it_on(for_changing):
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=10", "RDATE;TZID=Europe/London:20261119T180000,20261126T180000",
             "RDATE:20261201T180000Z"]
    imported = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *rules)
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [imported], GYM_INSTANCES: [_events_page()], f"PATCH {GYM_URL}": [imported, imported]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    # The 26th and the 1st would have stayed on the calendar, so they go from the rules - the 19th stays.
    assert calendar._session.sent == [_write("PATCH", GYM_URL, {"recurrence": [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z", "RDATE;TZID=Europe/London:20261119T180000"]})]
    # Undone, the old rules go back whole.
    assert reply["undo"]() == {"success": True, "message": "Put Gym back from Tuesday on."}
    assert calendar._session.sent[-1] == _write("PATCH", GYM_URL, {"recurrence": rules})


@pytest.mark.parametrize("status", [404, 410])
def test_an_event_already_gone_is_said_plainly(for_changing, status):
    # Deleted on the phone meanwhile - Google's 404 for an event it's forgotten, 410 for one deleted.
    gone = _refused(status, "notFound" if status == 404 else "deleted")
    dentist = _event_url("calendar-personal", "made-up-dentist")
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"DELETE {dentist}": [gone]})
    assert calendar.remove("dentist", today=NOVEMBER_21) == {"success": False, "message": "Dentist was already gone."}
    assert calendar._session.sent == [_write("DELETE", dentist)]

    # From this one on: the series gone when it's read, deleted, or cut - and nothing else sent.
    series = _event_url("calendar-personal", "made-up-gym")
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00Z", "2026-11-24T19:00:00Z", *GYM_RULES)
    for answers, sent in (({series: [gone]}, []), ({series: [first], f"DELETE {series}": [gone]}, ["DELETE"]),
                          ({series: [GYM], f"PATCH {series}": [gone]}, ["PATCH"])):
        calendar = for_changing(personal=[GYM_24TH], answers={f"{series}/instances": [_events_page()], **answers})
        assert calendar.remove("gym", following=True, today=NOVEMBER_21) == {
            "success": False, "message": "Gym was already gone."}
        assert [each["method"] for each in calendar._session.sent] == sent

    # Undoing: a series cut, then deleted on the phone.
    calendar = for_changing(personal=[GYM_24TH], answers={
        series: [GYM], f"{series}/instances": [_events_page()], f"PATCH {series}": [GYM, gone]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == {
        "success": False, "message": "Gym was already gone."}
    assert [each["method"] for each in calendar._session.sent] == ["PATCH", "PATCH"]
    # And an event added, then deleted on the phone.
    added = _event_url("calendar-personal", "made-up-event")
    calendar = for_changing(answers={f"POST {_events_url('calendar-personal')}": [_inserted()], f"DELETE {added}": [gone]})
    assert calendar.add("Dentist", "2026-11-26 15:00", today=NOVEMBER_21)["undo"]() == {
        "success": False, "message": "Dentist was already gone."}
    assert [(each["method"], each["url"]) for each in calendar._session.sent] == [
        ("POST", _events_url("calendar-personal")), ("DELETE", added)]


UNCLEAR = "I can't tell whether Dentist was removed."


@pytest.mark.parametrize("answer, said, sends", [
    # Sent, but whether it was removed can't be told - so it isn't sent again.
    (requests.ReadTimeout("Read timed out."), UNCLEAR, 1),
    (_refused(500, "backendError"), UNCLEAR, 1),
    (FakeResponse(504, "Gateway Timeout"), UNCLEAR, 1),
    (requests.ConnectionError(urllib3.exceptions.ProtocolError(
        "Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))), UNCLEAR, 1),
    (requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"), UNCLEAR, 1),
    (FakeResponse(200, "not an answer"), UNCLEAR, 1),
    # Never sent, refused, or Google slowing Nova down - so certainly not removed.
    (requests.ConnectTimeout("Connection timed out."), CANT_REACH_GOOGLE, 1),
    (_refused(403), "Google wouldn't change that.", 1),
    (_refused(429, "rateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    # Turned down unread, the access token is got afresh once.
    (_refused(401, "authError"), NEEDS_SIGN_IN, 2),
])
def test_an_unclear_answer_is_never_sent_again(for_changing, caplog, answer, said, sends):
    dentist = _event_url("calendar-personal", "made-up-dentist")
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"DELETE {dentist}": [answer, answer]})
    with caplog.at_level(logging.INFO):
        assert calendar.remove("dentist", today=NOVEMBER_21) == {"success": False, "message": said}
    assert calendar._session.sent == [_write("DELETE", dentist)] * sends
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)


def test_a_removed_event_is_put_back_as_it_was(for_changing):
    dentist = _event_url("calendar-personal", "made-up-dentist")
    calendar = for_changing(personal=[DENTIST_THURSDAY],
                            answers={f"DELETE {dentist}": [DELETED], f"PATCH {dentist}": [DENTIST_THURSDAY]})
    undo = calendar.remove("dentist", today=NOVEMBER_21)["undo"]
    # Set back to confirmed, so its reminders and guests come back with it - and said back.
    assert undo() == {"success": True, "message": "Put Dentist back, Thursday at 3 PM."}
    assert calendar._session.sent == [_write("DELETE", dentist), _write("PATCH", dentist, {"status": "confirmed"})]


def test_a_removed_event_google_wont_confirm_is_put_back_as_a_copy(for_changing):
    dentist = _event_url("calendar-personal", "made-up-dentist")
    events = _events_url("calendar-personal")
    full = {**DENTIST_THURSDAY, "description": "Bring the form", "location": "Made-up Dental",
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
            "colorId": "5", "transparency": "transparent", "visibility": "private", "etag": '"2"',
            "htmlLink": "https://www.google.com/calendar/event?eid=made-up", "iCalUID": "made-up-dentist@google.com",
            "attendees": [{"email": "someone@example.com"}]}
    # What the user sees of it - not its id, nor its guests, who'd be asked again.
    copy = {key: full[key] for key in ("summary", "description", "location", "start", "end", "reminders", "colorId",
                                       "transparency", "visibility")}
    for refused in (_refused(404, "notFound"), _refused(410, "deleted"), _refused(400, "invalid")):
        calendar = for_changing(personal=[full], answers={f"DELETE {dentist}": [DELETED], f"PATCH {dentist}": [refused],
                                                          f"POST {events}": [_inserted()]})
        assert calendar.remove("dentist", today=NOVEMBER_21)["undo"]() == {
            "success": True, "message": "Put Dentist back, Thursday at 3 PM."}, refused.status_code
        assert calendar._session.sent[1:] == [_write("PATCH", dentist, {"status": "confirmed"}),
                                              _write("POST", events, copy)], refused.status_code

    # Google slowing Nova down, or not saying whether it set it back, is never followed by a copy -
    # it may be back already.
    for answer, said in ((_refused(429, "rateLimitExceeded"), CANT_REACH_GOOGLE),
                         (requests.ReadTimeout("Read timed out."), "I can't tell whether Dentist was put back.")):
        calendar = for_changing(personal=[full], answers={f"DELETE {dentist}": [DELETED], f"PATCH {dentist}": [answer]})
        assert calendar.remove("dentist", today=NOVEMBER_21)["undo"]() == {"success": False, "message": said}
        assert [each["method"] for each in calendar._session.sent] == ["DELETE", "PATCH"]


TUESDAY_EVENING = datetime(2026, 11, 24, 20, 0, tzinfo=LONDON)
GONE = {"success": False, "message": "That day's already gone - which day did you mean?"}


def test_from_a_day_already_gone_on_changes_nothing(for_changing):
    # "Every week from last Tuesday" would take the times since then too - the record of them.
    calendar = for_changing(personal=[GYM_24TH])
    assert calendar.remove("gym", day=date(2026, 11, 17), following=True, today=NOVEMBER_21) == GONE
    assert calendar._session.asked == [] and calendar._session.sent == []


def test_from_this_one_on_starts_from_the_next_time_to_come(for_changing):
    # "Remove gym every week" said at 8 on a Tuesday evening: that day's session is over, so
    # it's from the next one, and today's stays.
    series = _event_url("calendar-personal", "made-up-gym")
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        series: [GYM], f"{series}/instances": [_events_page()], f"PATCH {series}": [GYM]})
    assert _said(calendar.remove("gym", following=True, now=TUESDAY_EVENING)) == {
        "success": True, "message": "Removed Gym from Tuesday the 1st of December on."}
    assert calendar._session.sent == [_write("PATCH", series, {"recurrence": [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T175959Z", "EXDATE;TZID=Europe/London:20261110T180000"]})]

    # One all day has started from the start of its day.
    bins = _event_url("calendar-personal", "made-up-bins")
    calendar = for_changing(personal=[_occurrence("Bins", "made-up-bins", "2026-11-24", "2026-11-25"),
                                      _occurrence("Bins", "made-up-bins", "2026-12-01", "2026-12-02")],
                            answers={bins: [_series("Bins", "made-up-bins", "2026-11-03", "2026-11-04",
                                                    "RRULE:FREQ=WEEKLY;COUNT=20")],
                                     f"{bins}/instances": [_events_page()], f"PATCH {bins}": [{}]})
    assert _said(calendar.remove("bins", following=True, now=datetime(2026, 11, 24, 7, 0, tzinfo=LONDON))) == {
        "success": True, "message": "Removed Bins from Tuesday the 1st of December on."}
    assert calendar._session.sent == [_write("PATCH", bins, {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20261130"]})]


def test_a_time_a_repeating_event_has_started_is_never_changed(for_changing):
    # Not even that day's alone: past sessions and shifts are the record of them. Under way
    # counts as started, and one all day from the start of its day.
    bins_today = _occurrence("Bins", "made-up-bins", "2026-11-24", "2026-11-25")
    for search, events, now in (("gym", [GYM_24TH], TUESDAY_EVENING),
                                ("gym", [GYM_24TH], datetime(2026, 11, 24, 18, 30, tzinfo=LONDON)),
                                ("bins", [bins_today], datetime(2026, 11, 24, 7, 0, tzinfo=LONDON))):
        calendar = for_changing(personal=events)
        assert calendar.remove(search, day=date(2026, 11, 24), now=now) == GONE, (search, now)
        assert calendar._session.sent == []

    # Beside one to come, it isn't one of those found.
    first_of_december = _event_url("calendar-personal", GYM_1ST["id"])
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={f"DELETE {first_of_december}": [DELETED]})
    assert _said(calendar.remove("gym", now=TUESDAY_EVENING)) == {
        "success": True, "message": "Removed Gym, Tuesday the 1st of December at 6 PM."}
    assert calendar._session.sent == [_write("DELETE", first_of_december)]


def test_a_one_off_event_already_past_is_still_removed(for_changing):
    # Only a repeating event's past times are a record - a one-off can still be put right after.
    dentist = _own("Dentist", "2026-11-24T09:00:00Z", "2026-11-24T09:30:00Z", "made-up-dentist-past")
    url = _event_url("calendar-personal", "made-up-dentist-past")
    for day, now, said in ((None, TUESDAY_EVENING, "today"),
                           (date(2026, 11, 24), datetime(2026, 11, 26, 12, 0, tzinfo=LONDON),
                            "Tuesday the 24th of November")):
        calendar = for_changing(personal=[dentist], answers={f"DELETE {url}": [DELETED]})
        assert _said(calendar.remove("dentist", day=day, now=now)) == {
            "success": True, "message": f"Removed Dentist, {said} at 9 AM."}, said
        assert calendar._session.sent == [_write("DELETE", url)], said


LECTURE = _own("Lecture", "2026-11-26T11:00:00Z", "2026-11-26T12:00:00Z", "made-up-lecture")
UNI_LECTURE = _own("Lecture", "2026-11-26T11:00:00Z", "2026-11-26T12:00:00Z", "made-up-uni-lecture",
                   calendar_id="calendar-uni")


def test_the_calendars_are_named_when_the_matches_are_on_several(for_changing):
    # Said the same twice, the user couldn't tell them apart.
    calendar = for_changing(personal=[LECTURE], uni=[UNI_LECTURE])
    assert calendar.remove("lecture", today=NOVEMBER_21) == {"success": False, "message": (
        "I found 2: Lecture on Thursday, at 11 AM, on Personal; and Lecture on Thursday, at 11 AM, on Uni. "
        "Which one?")}
    assert calendar._session.sent == []


def test_a_calendar_named_narrows_the_matches(for_changing):
    # Named as an event is added to one - by its name, "my ... calendar", or an activity's word.
    mine = _event_url("calendar-personal", "made-up-lecture")
    for named in ("personal", "my calendar", "my personal calendar"):
        calendar = for_changing(personal=[LECTURE], uni=[UNI_LECTURE], answers={f"DELETE {mine}": [DELETED]})
        assert _said(calendar.remove("lecture", calendar=named, today=NOVEMBER_21)) == {
            "success": True, "message": "Removed Lecture, Thursday at 11 AM."}, named
        assert calendar._session.sent == [_write("DELETE", mine)], named
        # Only the calendar named is read.
        assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal"], named
    for named in ("uni", "my uni calendar", "lectures"):
        calendar = for_changing(personal=[LECTURE], uni=[UNI_LECTURE])
        assert calendar.remove("lecture", calendar=named, today=NOVEMBER_21) == {
            "success": False, "message": "I can't change that, Google only lets me change your own calendars."}, named
        assert calendar._session.sent == [], named
    calendar = for_changing(personal=[LECTURE], uni=[UNI_LECTURE])
    assert calendar.remove("lecture", calendar="gym", today=NOVEMBER_21) == {
        "success": False, "message": "I haven't got a calendar called that."}
    assert _read(calendar) == [] and calendar._session.sent == []


def test_nothing_found_on_a_calendar_named_says_that_calendar(for_changing):
    # Said by the calendar's own name, not "your calendar", which it might not be - and the day, as before.
    calendar = for_changing(personal=[LECTURE], work=[], uni=[UNI_LECTURE], reads=4)
    assert calendar.remove("dentist", calendar="work", today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the dentist on Work."}
    assert calendar.remove("dentist", calendar="work", day=THURSDAY, today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the dentist on Work on Thursday."}
    # The user's own is said by its name too.
    assert calendar.remove("dentist", calendar="my calendar", today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the dentist on Personal."}
    # And with no calendar named, it's looked for on them all, as before.
    assert calendar.remove("dentist", today=NOVEMBER_21) == {
        "success": False, "message": "I can't find the dentist on your calendar."}


# Moving and renaming

DENTIST_URL = _event_url("calendar-personal", "made-up-dentist")
GYM_URL = _event_url("calendar-personal", "made-up-gym")
PERSONAL_EVENTS = _events_url("calendar-personal")
# Google's answer to an event changed - the event, as events.patch gives it back.
CHANGED = {"kind": "calendar#event", "id": "made-up-dentist", "status": "confirmed"}
# Gym on Tuesday the 24th, meant for 6 that evening, moved to Thursday at 6.
GYM_MOVED_ON = _occurrence("Gym", "made-up-gym", "2026-11-26T18:00:00Z", "2026-11-26T19:00:00Z",
                           meant="2026-11-24T18:00:00Z")
# The gym's times before the 24th, as instances gives them with showDeleted: the 10th's was
# deleted on its own.
BEFORE_THE_24TH = [_occurrence("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z"),
                   {"kind": "calendar#event", "id": "made-up-gym_20261110T180000Z", "status": "cancelled",
                    "recurringEventId": "made-up-gym", "originalStartTime": _when("2026-11-10T18:00:00Z")},
                   _occurrence("Gym", "made-up-gym", "2026-11-17T18:00:00Z", "2026-11-17T19:00:00Z")]
CUT_AT_THE_24TH = {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z",
                                  "EXDATE;TZID=Europe/London:20261110T180000"]}
# How the gym's instances are asked for in a cut at the 24th: those deleted on their own, from it on, and the
# times before it, for the COUNT.
GYM_INSTANCES = f"{GYM_URL}/instances"
# Looked for two years on - 730 days - and no further: a series with no end is listed on and on.
FROM_THE_24TH = {"timeMin": "2026-11-24T18:00:00+00:00", "timeMax": "2028-11-23T18:00:00+00:00",
                 "showDeleted": "true", "maxResults": 250}
# The times before it for the COUNT are read 60 days past it: one moved on from before it has a start after it.
BEFORE_THE_24TH_ASKED = {"timeMax": "2027-01-23T18:00:00+00:00", "showDeleted": "true", "maxResults": 250}


def _splitting(**answers):
    """A calendar with the gym's next times, to change from the 24th on - Google answering as answers give it."""
    # Read for those deleted on their own, from the 24th on, and then for the COUNT, up to it.
    return {"personal": [GYM_24TH, GYM_1ST], "answers": {GYM_URL: [GYM], f"{GYM_URL}/instances": [
        _events_page(), _events_page(*BEFORE_THE_24TH)], f"POST {PERSONAL_EVENTS}": [_inserted()], **answers}}


def test_an_event_moves_keeping_its_length(for_changing, caplog):
    # Half an hour on Thursday at 3, moved to Friday at 4, is still half an hour.
    dentist = _own("Dentist", "2026-11-26T15:00:00Z", "2026-11-26T15:30:00Z", "made-up-dentist")
    calendar = for_changing(personal=[dentist], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    with caplog.at_level(logging.INFO):
        reply = calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved Dentist to Friday at 4 PM."}
    assert callable(reply["undo"])
    # One PATCH, of its times alone.
    assert calendar._session.sent == [_write("PATCH", DENTIST_URL, {
        "start": {"dateTime": "2026-11-27T16:00:00+00:00"}, "end": {"dateTime": "2026-11-27T16:30:00+00:00"}})]
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)

    # An end given is kept.
    calendar = for_changing(personal=[dentist], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    calendar.change("dentist", start="2026-11-27 16:00", end="2026-11-27 17:15", today=NOVEMBER_21)
    assert calendar._session.sent[0]["json"]["end"] == {"dateTime": "2026-11-27T17:15:00+00:00"}

    # One that was all day lasts an hour once it's given a time - with its date cleared, as
    # Google's patch keeps whatever it isn't sent.
    holiday = _event_url("calendar-personal", "made-up-holiday")
    calendar = for_changing(personal=[_own("Holiday", "2026-11-22", "2026-11-23", "made-up-holiday")],
                            answers={f"PATCH {holiday}": [CHANGED]})
    assert _said(calendar.change("holiday", start="2026-11-27 10:00", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Holiday to Friday at 10 AM."}
    assert calendar._session.sent == [_write("PATCH", holiday, {
        "start": {"date": None, "dateTime": "2026-11-27T10:00:00+00:00"},
        "end": {"date": None, "dateTime": "2026-11-27T11:00:00+00:00"}})]

    # Times that can't be read aren't looked for, and an end before the start isn't sent.
    calendar = for_changing(personal=[dentist])
    for start, end in (("Friday at 4", None), ("2026-11-27 16:00", "about five"), ("2026-11-27T16:00", None)):
        assert calendar.change("dentist", start=start, end=end, today=NOVEMBER_21) == {
            "success": False, "message": "I didn't catch when that is."}, (start, end)
    # An end with no start is no new time.
    assert calendar.change("dentist", end="2026-11-27 17:00", summary="Dentist check-up", today=NOVEMBER_21) == {
        "success": False, "message": "I didn't catch when that is."}
    assert calendar.change("dentist", end="2026-11-27 17:00", today=NOVEMBER_21) == {
        "success": False, "message": "I didn't catch what to change."}
    assert calendar._session.asked == []
    for start, end in (("2026-11-27 16:00", "2026-11-27 15:00"), ("2026-11-27 16:00", "2026-11-27 16:00")):
        calendar = for_changing(personal=[dentist])
        assert calendar.change("dentist", start=start, end=end, today=NOVEMBER_21) == {
            "success": False, "message": "That ends before it starts."}, end
        assert calendar._session.sent == []


def test_an_all_day_event_stays_all_day_on_a_new_day(for_changing):
    # Three days from tomorrow - Google's end is the day after the last - moved to Friday: still three.
    holiday = _own("Holiday", "2026-11-22", "2026-11-25", "made-up-holiday")
    url = _event_url("calendar-personal", "made-up-holiday")
    calendar = for_changing(personal=[holiday], answers={f"PATCH {url}": [CHANGED]})
    assert _said(calendar.change("holiday", start="2026-11-27", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Holiday to Friday, all day."}
    assert calendar._session.sent == [_write("PATCH", url, {"start": {"date": "2026-11-27"},
                                                            "end": {"date": "2026-11-30"}})]

    # An end day given is included, as adding has it.
    calendar = for_changing(personal=[holiday], answers={f"PATCH {url}": [CHANGED]})
    calendar.change("holiday", start="2026-11-27", end="2026-11-28", today=NOVEMBER_21)
    assert calendar._session.sent[0]["json"] == {"start": {"date": "2026-11-27"}, "end": {"date": "2026-11-29"}}


def test_a_date_alone_keeps_a_timed_events_time(for_changing):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="2026-11-27", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Dentist to Friday at 3 PM."}
    assert calendar._session.sent == [_write("PATCH", DENTIST_URL, {
        "start": {"dateTime": "2026-11-27T15:00:00+00:00"}, "end": {"dateTime": "2026-11-27T16:00:00+00:00"}})]

    # The time it's at on the clock, over the clocks going back - 9:30 on BST is 9:30 on GMT.
    dentist = _own("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00", "made-up-dentist")
    calendar = for_changing(personal=[dentist], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="2026-10-29", today=date(2026, 10, 20))) == {
        "success": True, "message": "Moved Dentist to Thursday the 29th of October at 9:30 AM."}
    assert calendar._session.sent[0]["json"] == {"start": {"dateTime": "2026-10-29T09:30:00+00:00"},
                                                 "end": {"dateTime": "2026-10-29T10:00:00+00:00"}}


def test_a_time_alone_is_on_the_events_own_day(for_changing):
    # "Move the dentist to 5": Claude has no day to give, so the time alone is on the one found.
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="17:00", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Dentist to Thursday at 5 PM."}
    assert calendar._session.sent == [_write("PATCH", DENTIST_URL, {
        "start": {"dateTime": "2026-11-26T17:00:00+00:00"}, "end": {"dateTime": "2026-11-26T18:00:00+00:00"}})]

    # An end given as a time alone keeps that day too - after a start of a time alone, or of a day.
    for start in ("17:00", "2026-11-26 17:00", "2026-11-26"):
        calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
        calendar.change("dentist", start=start, end="18:30", today=NOVEMBER_21)
        assert calendar._session.sent[0]["json"]["end"] == {"dateTime": "2026-11-26T18:30:00+00:00"}, start

    # One all day is given the time on its own day, and lasts an hour.
    holiday = _event_url("calendar-personal", "made-up-holiday")
    calendar = for_changing(personal=[_own("Holiday", "2026-11-22", "2026-11-23", "made-up-holiday")],
                            answers={f"PATCH {holiday}": [CHANGED]})
    calendar.change("holiday", start="10:00", today=NOVEMBER_21)
    assert calendar._session.sent == [_write("PATCH", holiday, {
        "start": {"date": None, "dateTime": "2026-11-22T10:00:00+00:00"},
        "end": {"date": None, "dateTime": "2026-11-22T11:00:00+00:00"}})]

    # A time which isn't one isn't looked for, and an end with no start is no new time, as before.
    calendar = for_changing(personal=[DENTIST_THURSDAY])
    for start, end in (("25:00", None), ("17:60", None), ("17:00", "later"), ("5pm", None)):
        assert calendar.change("dentist", start=start, end=end, today=NOVEMBER_21) == {
            "success": False, "message": "I didn't catch when that is."}, (start, end)
    assert calendar.change("dentist", end="18:30", summary="Dentist check-up", today=NOVEMBER_21) == {
        "success": False, "message": "I didn't catch when that is."}
    assert calendar._session.asked == [] and calendar._session.sent == []
    assert calendar.change("dentist", start="18:00", end="17:00", today=NOVEMBER_21) == {
        "success": False, "message": "That ends before it starts."}
    assert calendar._session.sent == []


def test_a_time_alone_for_a_day_gone_isnt_moved_to(for_changing):
    # An event on a day before today - looked for there by its day - is gone, as a date said is.
    gone = _own("Dentist", "2026-11-19T15:00:00Z", "2026-11-19T16:00:00Z", "made-up-dentist")
    calendar = for_changing(personal=[gone])
    assert calendar.change("dentist", day=date(2026, 11, 19), start="17:00", today=NOVEMBER_21) == GONE
    assert calendar._session.sent == []

    # Earlier today is fine, as it is for a date.
    earlier = _own("Dentist", "2026-11-21T09:00:00Z", "2026-11-21T10:00:00Z", "made-up-dentist")
    calendar = for_changing(personal=[earlier], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="08:00", today=NOVEMBER_21))["success"]


def test_an_event_is_renamed(for_changing):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", summary=" Dentist check-up ", today=NOVEMBER_21)) == {
        "success": True, "message": "Renamed Dentist to Dentist check-up, Thursday at 3 PM."}
    assert calendar._session.sent == [_write("PATCH", DENTIST_URL, {"summary": "Dentist check-up"})]

    # Nothing heard to change, nothing is looked for.
    calendar = for_changing(personal=[DENTIST_THURSDAY])
    for summary, start in ((None, None), ("  ", None), ("", "")):
        assert calendar.change("dentist", start=start, summary=summary, today=NOVEMBER_21) == {
            "success": False, "message": "I didn't catch what to change."}, (summary, start)
    assert calendar._session.asked == [] and calendar._session.sent == []


def test_moved_and_renamed_at_once(for_changing):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="2026-11-27 16:00", summary="Dentist check-up",
                                 today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Dentist to Friday at 4 PM and renamed it Dentist check-up."}
    assert calendar._session.sent == [_write("PATCH", DENTIST_URL, {
        "start": {"dateTime": "2026-11-27T16:00:00+00:00"}, "end": {"dateTime": "2026-11-27T17:00:00+00:00"},
        "summary": "Dentist check-up"})]


def test_a_day_gone_isnt_moved_to(for_changing):
    # The week or the year misheard, as when adding - so nothing is even looked for.
    calendar = for_changing(personal=[DENTIST_THURSDAY])
    for start in ("2026-11-20 15:00", "2026-11-20", "2025-11-27 16:00"):
        assert calendar.change("dentist", start=start, today=NOVEMBER_21) == GONE, start
    assert calendar._session.asked == [] and calendar._session.sent == []

    # Earlier today is fine.
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    assert _said(calendar.change("dentist", start="2026-11-21 08:00", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Dentist to today at 8 AM."}


@pytest.mark.parametrize("answer, said", [
    # Sent, but whether it changed can't be told - so it isn't sent again.
    (requests.ReadTimeout("Read timed out."), "I can't tell whether Dentist was changed."),
    (_refused(500, "backendError"), "I can't tell whether Dentist was changed."),
    (FakeResponse(200, "not an answer"), "I can't tell whether Dentist was changed."),
    # Deleted on the phone meanwhile.
    (_refused(404, "notFound"), "Dentist was already gone."),
    (_refused(410, "deleted"), "Dentist was already gone."),
    # Refused, or never sent.
    (_refused(403), "Google wouldn't change that."),
    (requests.ConnectTimeout("Connection timed out."), CANT_REACH_GOOGLE),
])
def test_a_change_is_sent_once_whatever_google_answers(for_changing, caplog, answer, said):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [answer, answer]})
    with caplog.at_level(logging.INFO):
        assert calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21) == {
            "success": False, "message": said}
    assert len(calendar._session.sent) == 1
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)


def test_a_change_is_undone_to_the_old_times_and_name(for_changing):
    old = {"start": DENTIST_THURSDAY["start"], "end": DENTIST_THURSDAY["end"]}
    for asked, back, said in (
            ({"start": "2026-11-27 16:00"}, old, "Moved Dentist back to Thursday at 3 PM."),
            ({"summary": "Dentist check-up"}, {"summary": "Dentist"}, "Renamed it back to Dentist."),
            ({"start": "2026-11-27 16:00", "summary": "Dentist check-up"}, {**old, "summary": "Dentist"},
             "Put Dentist back as it was, Thursday at 3 PM.")):
        calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED, CHANGED]})
        undo = calendar.change("dentist", today=NOVEMBER_21, **asked)["undo"]
        assert undo() == {"success": True, "message": said}, said
        assert len(calendar._session.sent) == 2 and calendar._session.sent[-1] == _write("PATCH", DENTIST_URL, back)

    # One that was all day is put back all day, with the time it was given cleared.
    holiday = _own("Holiday", "2026-11-22", "2026-11-23", "made-up-holiday")
    url = _event_url("calendar-personal", "made-up-holiday")
    calendar = for_changing(personal=[holiday], answers={f"PATCH {url}": [CHANGED, CHANGED]})
    assert calendar.change("holiday", start="2026-11-27 10:00", today=NOVEMBER_21)["undo"]() == {
        "success": True, "message": "Moved Holiday back to tomorrow, all day."}
    assert calendar._session.sent[-1] == _write("PATCH", url, {"start": {"dateTime": None, "date": "2026-11-22"},
                                                               "end": {"dateTime": None, "date": "2026-11-23"}})

    # Sent once too: an unclear answer is said as that, and one deleted since as gone, by the name it has now.
    for answer, said in ((requests.ReadTimeout("Read timed out."), "I can't tell whether Dentist was put back."),
                         (_refused(410, "deleted"), "Dentist check-up was already gone.")):
        calendar = for_changing(personal=[DENTIST_THURSDAY],
                                answers={f"PATCH {DENTIST_URL}": [CHANGED, answer, CHANGED]})
        undo = calendar.change("dentist", summary="Dentist check-up", today=NOVEMBER_21)["undo"]
        assert undo() == {"success": False, "message": said}
        assert len(calendar._session.sent) == 2


def test_all_of_them_only_changes_the_time(for_changing):
    # Every Tuesday moved to Wednesdays would need its rule changed - that's left to Google Calendar.
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], reads=2)
    for start in ("2026-11-25 19:00", "2026-11-25"):
        assert calendar.change("gym", start=start, following=True, today=NOVEMBER_21) == {
            "success": False, "message": "I can only change the time for all of them. Move the days in Google "
                                         "Calendar."}, start
    assert calendar._session.sent == [] and GYM_URL not in [url for url, _ in calendar._session.asked]

    # That day's alone can move to another day, by its own id.
    tuesday = _event_url("calendar-personal", GYM_24TH["id"])
    calendar = for_changing(personal=[GYM_24TH], answers={f"PATCH {tuesday}": [CHANGED]})
    assert _said(calendar.change("gym", day=date(2026, 11, 24), start="2026-11-25 19:00", today=NOVEMBER_21)) == {
        "success": True, "message": "Moved Gym to Wednesday at 7 PM."}
    assert calendar._session.sent == [_write("PATCH", tuesday, {
        "start": {"dateTime": "2026-11-25T19:00:00+00:00"}, "end": {"dateTime": "2026-11-25T20:00:00+00:00"}})]


def test_from_the_first_one_on_changes_the_series_itself(for_changing):
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)
    old = {"start": first["start"], "end": first["end"]}
    # Its own first date at the new time, in the zone it repeats in, which Google needs of a repeating event.
    moved = {"start": {"dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"},
             "end": {"dateTime": "2026-11-24T20:00:00+00:00", "timeZone": "Europe/London"}}
    for asked, sent, back, said in (
            ({"start": "2026-11-24 19:00"}, moved, old, "Moved Gym to 7 PM from Tuesday on."),
            ({"summary": "Gym class"}, {"summary": "Gym class"}, {"summary": "Gym"},
             "Renamed Gym to Gym class from Tuesday on."),
            ({"start": "2026-11-24 19:00", "summary": "Gym class"}, {**moved, "summary": "Gym class"},
             {**old, "summary": "Gym"}, "Moved Gym to 7 PM from Tuesday on and renamed it Gym class."),
            # A time alone - on that Tuesday, which is all Claude knows of the day.
            ({"start": "19:00"}, moved, old, "Moved Gym to 7 PM from Tuesday on.")):
        calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={GYM_URL: [first],
                                                                       f"PATCH {GYM_URL}": [first, first]})
        reply = calendar.change("gym", following=True, today=NOVEMBER_21, **asked)
        assert _said(reply) == {"success": True, "message": said}, said
        assert calendar._session.sent == [_write("PATCH", GYM_URL, sent)], said
        # Undone, the series is put back as it was.
        assert reply["undo"]() == {"success": True, "message": "Put Gym back as it was."}, said
        assert calendar._session.sent[-1] == _write("PATCH", GYM_URL, back), said


def test_from_this_one_on_splits_the_series_with_the_times_left(for_changing):
    # Before the 1st of December the series - from the 3rd of November, ten times - was meant to be
    # on four Tuesdays: the 10th's, deleted on its own, still counts, as a COUNT counts it, and the
    # 8th's, moved to the 30th, isn't one of them.
    before = [*BEFORE_THE_24TH, GYM_24TH,
              _occurrence("Gym", "made-up-gym", "2026-11-30T18:00:00Z", "2026-11-30T19:00:00Z",
                          meant="2026-12-08T18:00:00Z")]
    instances = f"{GYM_URL}/instances"
    calendar = for_changing(personal=[GYM_1ST], answers={
        GYM_URL: [GYM], instances: [_events_page(), _events_page(*before)], f"PATCH {GYM_URL}": [GYM],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    reply = calendar.change("gym", day=date(2026, 12, 1), start="2026-12-01 19:00", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved Gym to 7 PM from Tuesday the 1st of December on."}
    # Those deleted on their own read from when this one was meant to be, then the times before it counted, with
    # those deleted - read 60 days on, for any moved on from before it.
    assert [params for url, params in calendar._session.asked if url == instances] == [
        {"timeMin": "2026-12-01T18:00:00+00:00", "timeMax": "2028-11-30T18:00:00+00:00", "showDeleted": "true",
         "maxResults": 250},
        {"timeMax": "2027-01-30T18:00:00+00:00", "showDeleted": "true", "maxResults": 250}]
    # The old series ended just before it, then a new one from it at the new time, with the six times left.
    assert calendar._session.sent == [
        _write("PATCH", GYM_URL, {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T175959Z",
                                                 "EXDATE;TZID=Europe/London:20261110T180000"]}),
        _write("POST", PERSONAL_EVENTS, {
            "summary": "Gym", "start": {"dateTime": "2026-12-01T19:00:00+00:00", "timeZone": "Europe/London"},
            "end": {"dateTime": "2026-12-01T20:00:00+00:00", "timeZone": "Europe/London"},
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=6", "EXDATE;TZID=Europe/London:20261110T180000"]})]

    # Renamed from the next one: the series' own times from it, under the new name - and an UNTIL is
    # kept as it is, so nothing is counted.
    rules = ["RRULE:FREQ=WEEKLY;UNTIL=20270105T180000Z;BYDAY=TU"]
    until = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *rules)
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [until], instances: [_events_page()], f"PATCH {GYM_URL}": [until],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert _said(calendar.change("gym", summary="Gym class", following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Renamed Gym to Gym class from Tuesday on."}
    assert calendar._session.sent == [
        _write("PATCH", GYM_URL, {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z"]}),
        _write("POST", PERSONAL_EVENTS, {
            "summary": "Gym class", "start": {"dateTime": "2026-11-24T18:00:00+00:00", "timeZone": "Europe/London"},
            "end": {"dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"}, "recurrence": rules})]
    assert [params for url, params in calendar._session.asked if url == instances] == [FROM_THE_24TH]


def test_a_series_in_another_zone_keeps_its_own_clock_time_when_it_is_split(for_changing):
    # Gym at 6 PM in New York, every Tuesday from the 27th of October, when New York was on EDT (-4) and
    # London already on GMT. Renamed from the 10th, when New York is on EST (-5): 6 PM there is 11 PM in London,
    # not the 10 PM which was London's clock on the first day, which would be 5 PM in New York.
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
    series = {**_series("Gym", "made-up-gym", "2026-10-27T22:00:00Z", "2026-10-27T23:00:00Z", *rules),
              "start": {"dateTime": "2026-10-27T18:00:00-04:00", "timeZone": "America/New_York"},
              "end": {"dateTime": "2026-10-27T19:00:00-04:00", "timeZone": "America/New_York"}}
    tenth = _occurrence("Gym", "made-up-gym", "2026-11-10T23:00:00Z", "2026-11-11T00:00:00Z")
    seventeenth = _occurrence("Gym", "made-up-gym", "2026-11-17T23:00:00Z", "2026-11-18T00:00:00Z")
    calendar = for_changing(personal=[tenth, seventeenth], answers={
        GYM_URL: [series], GYM_INSTANCES: [_events_page()], f"PATCH {GYM_URL}": [series],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    reply = calendar.change("gym", summary="Gym class", following=True, today=date(2026, 11, 5))
    assert _said(reply) == {"success": True, "message": "Renamed Gym to Gym class from Tuesday on."}
    assert calendar._session.sent[1] == _write("POST", PERSONAL_EVENTS, {
        "summary": "Gym class", "recurrence": rules,
        "start": {"dateTime": "2026-11-10T18:00:00-05:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-11-10T19:00:00-05:00", "timeZone": "America/New_York"}})

    # A series at 8 PM in New York is the next day's 1 AM in London: it's still the 10th's, in its own zone.
    late = {**series, "start": {"dateTime": "2026-10-27T20:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-27T21:00:00-04:00", "timeZone": "America/New_York"}}
    tenth = _occurrence("Gym", "made-up-gym", "2026-11-11T01:00:00Z", "2026-11-11T02:00:00Z",
                        meant="2026-11-11T01:00:00Z")
    calendar = for_changing(personal=[tenth], answers={
        GYM_URL: [late], GYM_INSTANCES: [_events_page()], f"PATCH {GYM_URL}": [late],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert calendar.change("gym", summary="Gym class", following=True, today=date(2026, 11, 5))["success"]
    assert calendar._session.sent[1]["json"]["start"] == {"dateTime": "2026-11-10T20:00:00-05:00",
                                                          "timeZone": "America/New_York"}


def test_a_time_said_for_a_series_in_another_zone_is_the_time_on_the_users_clock(for_changing):
    # "Move my gym to 7 PM from now on" is 7 PM where the user is - London - whatever zone the series repeats in,
    # and on the day they said, which is London's.
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
    series = {**_series("Gym", "made-up-gym", "2026-10-27T22:00:00Z", "2026-10-27T23:00:00Z", *rules),
              "start": {"dateTime": "2026-10-27T18:00:00-04:00", "timeZone": "America/New_York"},
              "end": {"dateTime": "2026-10-27T19:00:00-04:00", "timeZone": "America/New_York"}}
    tenth = _occurrence("Gym", "made-up-gym", "2026-11-10T23:00:00Z", "2026-11-11T00:00:00Z")
    calendar = for_changing(personal=[tenth], answers={
        GYM_URL: [series], GYM_INSTANCES: [_events_page()], f"PATCH {GYM_URL}": [series],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert calendar.change("gym", start="19:00", following=True, today=date(2026, 11, 5))["success"]
    assert calendar._session.sent[1]["json"]["start"] == {"dateTime": "2026-11-10T19:00:00+00:00",
                                                          "timeZone": "America/New_York"}
    assert calendar._session.sent[1]["json"]["end"] == {"dateTime": "2026-11-10T20:00:00+00:00",
                                                        "timeZone": "America/New_York"}


def test_on_day_puts_a_time_on_a_day_by_the_clock_of_the_zone_given():
    on_day = google_calendar._on_day
    new_york = ({"dateTime": "2026-10-27T18:00:00-04:00", "timeZone": "America/New_York"},
                {"dateTime": "2026-10-27T19:30:00-04:00", "timeZone": "America/New_York"})
    # By New York's clock: 6 PM on EST, an hour and a half long - and with no zone given, by the brain's.
    assert on_day(new_york, date(2026, 11, 10), "America/New_York") == (
        {"dateTime": "2026-11-10T18:00:00-05:00"}, {"dateTime": "2026-11-10T19:30:00-05:00"})
    assert on_day(new_york, date(2026, 11, 10)) == (
        {"dateTime": "2026-11-10T22:00:00+00:00"}, {"dateTime": "2026-11-10T23:30:00+00:00"})
    # A zone this can't load is the brain's, as none is.
    assert on_day(new_york, date(2026, 11, 10), "Nowhere/Made_Up") == on_day(new_york, date(2026, 11, 10))
    # An event over the clocks going back keeps its length - 1 AM to 3 AM on the 1st of November in New York
    # is three hours, as the hour between is repeated.
    over = ({"dateTime": "2026-10-25T01:00:00-04:00"}, {"dateTime": "2026-10-25T03:00:00-04:00"})
    assert on_day(over, date(2026, 11, 1), "America/New_York") == (
        {"dateTime": "2026-11-01T01:00:00-04:00"}, {"dateTime": "2026-11-01T02:00:00-05:00"})
    # One all day is only moved.
    assert on_day(({"date": "2026-11-03"}, {"date": "2026-11-04"}), date(2026, 11, 10), "America/New_York") == (
        {"date": "2026-11-10"}, {"date": "2026-11-11"})


def test_from_this_one_on_with_a_time_alone_splits_at_the_next_time(for_changing):
    # "Move my gym to 7 from now on": no day is said, so none is guessed - it's the next gym's own, the 24th.
    calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM]}))
    reply = calendar.change("gym", start="19:00", following=True, today=date(2026, 11, 23))
    assert _said(reply) == {"success": True, "message": "Moved Gym to 7 PM from tomorrow on."}
    assert [each["method"] for each in calendar._session.sent] == ["PATCH", "POST"]
    assert calendar._session.sent[0]["json"] == CUT_AT_THE_24TH
    assert calendar._session.sent[1]["json"]["start"] == {"dateTime": "2026-11-24T19:00:00+00:00",
                                                          "timeZone": "Europe/London"}
    # Its end too, as a time alone.
    calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM]}))
    calendar.change("gym", start="19:00", end="20:30", following=True, today=NOVEMBER_21)
    assert calendar._session.sent[1]["json"]["end"] == {"dateTime": "2026-11-24T20:30:00+00:00",
                                                        "timeZone": "Europe/London"}


def test_an_all_day_series_is_split_from_this_one_on_with_the_times_left(for_changing):
    # Bins all day every Tuesday from the 3rd of November, eight times, the 10th deleted on its own: three
    # were meant to be before the 24th, so five are left - and the new series, made two days long, is from the
    # 24th. All day, the old one ends the day before, as a date.
    bins = _event_url("calendar-personal", "made-up-bins")
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=8", "EXDATE;VALUE=DATE:20261110"]
    series = _series("Bins", "made-up-bins", "2026-11-03", "2026-11-04", *rules)
    before = [_occurrence("Bins", "made-up-bins", "2026-11-03", "2026-11-04"),
              {"kind": "calendar#event", "id": "made-up-bins_20261110", "status": "cancelled",
               "recurringEventId": "made-up-bins", "originalStartTime": {"date": "2026-11-10"}},
              _occurrence("Bins", "made-up-bins", "2026-11-17", "2026-11-18")]
    calendar = for_changing(personal=[_occurrence("Bins", "made-up-bins", "2026-11-24", "2026-11-25"),
                                      _occurrence("Bins", "made-up-bins", "2026-12-01", "2026-12-02")],
                            answers={bins: [series], f"{bins}/instances": [_events_page(), _events_page(*before)],
                                     f"PATCH {bins}": [series, series], f"POST {PERSONAL_EVENTS}": [_inserted()],
                                     f"DELETE {_event_url('calendar-personal', 'made-up-event')}": [DELETED]})
    reply = calendar.change("bins", start="2026-11-24", end="2026-11-25", summary="Bins and recycling",
                            following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved Bins to all day from Tuesday on and renamed it "
                                                        "Bins and recycling."}
    assert [params for url, params in calendar._session.asked if url == f"{bins}/instances"] == [
        {"timeMin": "2026-11-24T00:00:00+00:00", "timeMax": "2028-11-23T00:00:00+00:00", "showDeleted": "true",
         "maxResults": 250},
        {"timeMax": "2027-01-23T00:00:00+00:00", "showDeleted": "true", "maxResults": 250}]
    assert calendar._session.sent == [
        _write("PATCH", bins, {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261123", rules[1]]}),
        _write("POST", PERSONAL_EVENTS, {
            "summary": "Bins and recycling", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-26"},
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=5", rules[1]]})]

    # Undone, the new series goes and the old one has its rules back.
    assert reply["undo"]() == {"success": True, "message": "Put Bins back as it was."}
    assert calendar._session.sent[2:] == [_write("DELETE", _event_url("calendar-personal", "made-up-event")),
                                          _write("PATCH", bins, {"recurrence": rules})]


def _bins_series_all_day(first_day="2026-11-03", last_day="2026-11-04"):
    """Bins all day every Tuesday from a day, eight times, the 10th of November left out."""
    return _series("Bins", "made-up-bins", first_day, last_day, "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=8",
                   "EXDATE;VALUE=DATE:20261110")


def test_an_all_day_series_given_a_time_from_this_one_on_is_given_the_calendars_zone(for_changing):
    # Google needs a zone for a repeating event at a time, and one all day has none: the calendar's, Europe/London
    # for Personal, is given to the new start and end.
    bins = _event_url("calendar-personal", "made-up-bins")
    series = _bins_series_all_day()
    before = [_occurrence("Bins", "made-up-bins", "2026-11-03", "2026-11-04"),
              _occurrence("Bins", "made-up-bins", "2026-11-17", "2026-11-18")]
    occurrences = [_occurrence("Bins", "made-up-bins", "2026-11-24", "2026-11-25"),
                   _occurrence("Bins", "made-up-bins", "2026-12-01", "2026-12-02")]
    calendar = for_changing(personal=occurrences, answers={
        bins: [series], f"{bins}/instances": [_events_page(), _events_page(*before)],
        f"PATCH {bins}": [series], f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert _said(calendar.change("bins", start="2026-11-24 19:00", following=True, today=NOVEMBER_21))["success"]
    sent = calendar._session.sent[1]["json"]
    assert sent["start"] == {"dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"}
    assert sent["end"] == {"dateTime": "2026-11-24T20:00:00+00:00", "timeZone": "Europe/London"}

    # From its first time on, the series itself is changed, with the date cleared.
    first = _bins_series_all_day("2026-11-24", "2026-11-25")
    calendar = for_changing(personal=occurrences, answers={bins: [first], f"PATCH {bins}": [first]})
    assert _said(calendar.change("bins", start="2026-11-24 19:00", following=True, today=NOVEMBER_21))["success"]
    assert calendar._session.sent == [_write("PATCH", bins, {
        "start": {"date": None, "dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"},
        "end": {"date": None, "dateTime": "2026-11-24T20:00:00+00:00", "timeZone": "Europe/London"}})]

    # A calendar Google gave no zone for leaves it out, as it was.
    unzoned = _entry("calendar-personal", "Personal", primary=True)
    del unzoned["timeZone"]
    calendar = for_changing(personal=occurrences, answers={
        LIST: [_calendar_list(unzoned)], bins: [first], f"PATCH {bins}": [first]})
    assert _said(calendar.change("bins", start="2026-11-24 19:00", following=True, today=NOVEMBER_21))["success"]
    assert calendar._session.sent[0]["json"]["start"] == {"date": None, "dateTime": "2026-11-24T19:00:00+00:00"}


def test_a_series_with_guests_isnt_split_from_this_one_on(for_changing):
    # A new series from this one on has no guests, so cutting the old one would take their future times
    # away. Nothing is cut, added or put back - only the series is read.
    me, friend = {"email": "me@example.com", "self": True}, {"email": "friend@example.com"}
    guests = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *GYM_RULES,
                     attendees=[me, friend])
    said = {"success": False, "message": "That has guests. Change it in Google Calendar."}
    for asked in ({"start": "2026-11-24 19:00"}, {"start": "19:00"}, {"summary": "Gym class"},
                  {"start": "19:00", "summary": "Gym class"}):
        calendar = for_changing(**_splitting(**{GYM_URL: [guests]}))
        assert calendar.change("gym", following=True, today=NOVEMBER_21, **asked) == said, asked
        assert calendar._session.sent == [], asked
        assert f"{GYM_URL}/instances" not in [url for url, _ in calendar._session.asked], asked

    # Only himself on it is no guest, and that day's alone, or from the first one on, is still changed:
    # neither makes a new series, so the guests stay on it.
    alone = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *GYM_RULES, attendees=[me])
    calendar = for_changing(**_splitting(**{GYM_URL: [alone], f"PATCH {GYM_URL}": [alone]}))
    assert _said(calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21))["success"]
    assert [each["method"] for each in calendar._session.sent] == ["PATCH", "POST"]

    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES,
                    attendees=[me, friend])
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={GYM_URL: [first], f"PATCH {GYM_URL}": [first]})
    assert _said(calendar.change("gym", summary="Gym class", following=True, today=NOVEMBER_21))["success"]
    assert [each["method"] for each in calendar._session.sent] == ["PATCH"]

    tuesday = _event_url("calendar-personal", GYM_24TH["id"])
    calendar = for_changing(personal=[GYM_24TH], answers={f"PATCH {tuesday}": [CHANGED]})
    assert _said(calendar.change("gym", summary="Gym class", today=NOVEMBER_21))["success"]
    assert GYM_URL not in [url for url, _ in calendar._session.asked]


def test_a_split_that_fails_puts_the_old_series_back(for_changing):
    couldnt = "I couldn't change them, so I've left Gym as it was."
    unsure = "I can't tell whether the new Gym was added, so check Google Calendar."
    neither = "I couldn't change them, and I can't be sure Gym is as it was, so check Google Calendar."
    for posted, put_back, said in (
            # Refused, or never sent: the old rules are put back once, so it's never left cut short.
            (_refused(400, "invalid"), CHANGED, couldnt),
            (requests.ConnectTimeout("Connection timed out."), CHANGED, couldnt),
            # Unanswered, the new one may be there - put back all the same, and said to be checked.
            (requests.ReadTimeout("Read timed out."), CHANGED, unsure),
            (FakeResponse(200, "not an event"), CHANGED, unsure),
            (requests.ReadTimeout("Read timed out."), _refused(403), unsure),
            # Not put back either - said to be checked, and nothing more sent.
            (_refused(400, "invalid"), _refused(403), neither),
            (_refused(400, "invalid"), requests.ReadTimeout("Read timed out."), neither)):
        calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM, put_back, GYM],
                                                f"POST {PERSONAL_EVENTS}": [posted, _inserted()]}))
        assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
            "success": False, "message": said}, said
        assert [each["method"] for each in calendar._session.sent] == ["PATCH", "POST", "PATCH"], said
        assert calendar._session.sent[0]["json"] == CUT_AT_THE_24TH
        assert calendar._session.sent[-1] == _write("PATCH", GYM_URL, {"recurrence": GYM_RULES}), said

    # The cut itself unanswered, nothing more is sent.
    calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [requests.ReadTimeout("Read timed out."), GYM]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I can't tell whether Gym was changed, so check Google Calendar."}
    assert [each["method"] for each in calendar._session.sent] == ["PATCH"]
    # As is the series' own change, from its first time on.
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [first], f"PATCH {GYM_URL}": [requests.ReadTimeout("Read timed out."), first]})
    assert calendar.change("gym", summary="Gym class", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I can't tell whether Gym was changed, so check Google Calendar."}
    assert [each["method"] for each in calendar._session.sent] == ["PATCH"]
    # Nor anything at all when the times before it can't be counted.
    calendar = for_changing(**_splitting(**{f"{GYM_URL}/instances": [requests.ConnectionError("Network down")]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._session.sent == []


def test_a_split_is_undone_whole(for_changing):
    new = _event_url("calendar-personal", "made-up-event")
    put_back = [_write("DELETE", new), _write("PATCH", GYM_URL, {"recurrence": GYM_RULES})]
    # The new series deleted, and the old one's rules put back - even when the new one has been
    # deleted on the phone since.
    for deleted in (DELETED, _refused(410, "deleted")):
        calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM, GYM], f"DELETE {new}": [deleted]}))
        reply = calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)
        assert _said(reply) == {"success": True, "message": "Moved Gym to 7 PM from Tuesday on."}
        # Seven times left from the 24th: the 3rd, the 10th and the 17th were before it.
        assert calendar._session.sent[1]["json"]["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=7"
        assert reply["undo"]() == {"success": True, "message": "Put Gym back as it was."}
        assert calendar._session.sent[2:] == put_back

    # The new one taken off but the old one not put back: said to be checked.
    for answer in (_refused(403), requests.ReadTimeout("Read timed out.")):
        calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM, answer], f"DELETE {new}": [DELETED]}))
        assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)["undo"]() == {
            "success": False, "message": "I can't be sure Gym is back as it was, so check Google Calendar."}
        assert calendar._session.sent[2:] == put_back
    # The new one not taken off: nothing more is sent.
    calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM, GYM], f"DELETE {new}": [_refused(403)]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)["undo"]() == {
        "success": False, "message": "Google wouldn't change that."}
    assert calendar._session.sent[2:] == put_back[:1]


def test_a_time_moved_from_a_slot_already_past_is_never_removed(for_changing):
    # At 8 on the Tuesday its own time is still to come, but the time it was meant to be has gone -
    # and from it on would end the series before that Tuesday's, which has been.
    calendar = for_changing(personal=[GYM_MOVED_ON])
    assert calendar.remove("gym", day=THURSDAY, now=TUESDAY_EVENING) == GONE
    assert calendar._session.sent == []

    calendar = for_changing(personal=[GYM_MOVED_ON, GYM_1ST], answers={
        GYM_URL: [GYM], f"{GYM_URL}/instances": [_events_page()], f"PATCH {GYM_URL}": [GYM]})
    assert _said(calendar.remove("gym", following=True, now=TUESDAY_EVENING)) == {
        "success": True, "message": "Removed Gym from Tuesday the 1st of December on."}
    assert calendar._session.sent[0]["json"]["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T175959Z"


def test_a_time_moved_from_a_slot_already_past_is_never_changed(for_changing):
    calendar = for_changing(personal=[GYM_MOVED_ON])
    assert calendar.change("gym", day=THURSDAY, start="2026-11-26 19:00", now=TUESDAY_EVENING) == GONE
    assert calendar._session.sent == []

    calendar = for_changing(personal=[GYM_MOVED_ON, GYM_1ST], answers={
        GYM_URL: [GYM], f"{GYM_URL}/instances": [_events_page(), _events_page(*BEFORE_THE_24TH, GYM_MOVED_ON)],
        f"PATCH {GYM_URL}": [GYM], f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert _said(calendar.change("gym", start="2026-12-01 19:00", following=True, now=TUESDAY_EVENING)) == {
        "success": True, "message": "Moved Gym to 7 PM from Tuesday the 1st of December on."}
    assert calendar._session.sent[0]["json"]["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T175959Z"
    assert calendar._session.sent[1]["json"]["recurrence"][0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=6"


# A day deleted on its own, and a series cut
#
# Google keeps a day deleted on its own as a cancelled instance of its series - and drops those after a
# cut once the series' rules end before them, so putting the rules back doesn't bring them back. They're
# read before a cut, and deleted again by its undo; a new series from a split has them left out.

GYM_8TH = _occurrence("Gym", "made-up-gym", "2026-12-08T18:00:00Z", "2026-12-08T19:00:00Z")


def _cancelled(series_id, meant):
    """A time of a repeating event deleted on its own, as instances gives it with showDeleted."""
    stamp = meant.replace("-", "") if len(meant) == 10 else \
        datetime.fromisoformat(meant).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {"kind": "calendar#event", "id": f"{series_id}_{stamp}", "status": "cancelled",
            "recurringEventId": series_id, "originalStartTime": _when(meant)}


# The gym on the 1st of December, deleted on its own: day four to the 24th's day three.
GYM_1ST_DELETED = _cancelled("made-up-gym", "2026-12-01T18:00:00Z")
GYM_1ST_DELETED_URL = _event_url("calendar-personal", "made-up-gym_20261201T180000Z")
# What reading the gym's times from the 24th on gives: those which stand, and the one deleted.
GYM_FROM_THE_24TH = _events_page(GYM_24TH, GYM_1ST_DELETED, GYM_8TH)
BACK_FROM_TUESDAY = {"success": True, "message": "Put Gym back from Tuesday on."}
DEC_1ST_CAME_BACK = {"success": False, "message": "Put Gym back from Tuesday on, but Tuesday the 1st of "
                                                  "December's time came back - remove it in Google Calendar."}


def test_a_day_deleted_on_its_own_stays_deleted_when_a_cut_is_undone(for_changing):
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [GYM], GYM_INSTANCES: [GYM_FROM_THE_24TH], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {GYM_1ST_DELETED_URL}": [DELETED]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    # Read before the cut, from the time it's cut at: once the rules end before them, Google drops them.
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [FROM_THE_24TH]
    assert [each["method"] for each in calendar._session.sent] == ["PATCH"]
    # Undone, the rules are put back, then the day deleted again - by its id, each write telling nobody.
    assert reply["undo"]() == BACK_FROM_TUESDAY
    assert calendar._session.sent[1:] == [_write("PATCH", GYM_URL, {"recurrence": GYM_RULES}),
                                          _write("DELETE", GYM_1ST_DELETED_URL)]


def test_a_day_deleted_on_its_own_stays_deleted_when_the_whole_series_is_put_back(for_changing):
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)
    # Every one is from the first on, so every one deleted on its own is read.
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [first], GYM_INSTANCES: [GYM_FROM_THE_24TH], f"DELETE {GYM_URL}": [DELETED],
        f"PATCH {GYM_URL}": [first], f"DELETE {GYM_1ST_DELETED_URL}": [DELETED]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [FROM_THE_24TH]
    assert calendar._session.sent == [_write("DELETE", GYM_URL)]
    # Confirmed again, then the day deleted again.
    assert reply["undo"]() == BACK_FROM_TUESDAY
    assert calendar._session.sent[1:] == [_write("PATCH", GYM_URL, {"status": "confirmed"}),
                                          _write("DELETE", GYM_1ST_DELETED_URL)]

    # Or, when Google won't confirm it, a copy is added with that day left out of it - a new series has
    # none of the old one's times to delete again.
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [first], GYM_INSTANCES: [GYM_FROM_THE_24TH], f"DELETE {GYM_URL}": [DELETED],
        f"PATCH {GYM_URL}": [_refused(404, "notFound")], f"POST {PERSONAL_EVENTS}": [_inserted()]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == BACK_FROM_TUESDAY
    assert calendar._session.sent[-1] == _write("POST", PERSONAL_EVENTS, {
        "summary": "Gym", "start": first["start"], "end": first["end"],
        "recurrence": [*GYM_RULES, "EXDATE:20261201T180000Z"]})
    assert [each["method"] for each in calendar._session.sent] == ["DELETE", "PATCH", "POST"]


def test_a_split_leaves_the_days_deleted_on_its_own_out_of_the_new_series(for_changing):
    new = _event_url("calendar-personal", "made-up-event")
    # Read from the 24th on, then - for the COUNT - up to it.
    calendar = for_changing(**_splitting(**{
        GYM_INSTANCES: [GYM_FROM_THE_24TH, _events_page(*BEFORE_THE_24TH)], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {new}": [DELETED], f"DELETE {GYM_1ST_DELETED_URL}": [DELETED]}))
    reply = calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved Gym to 7 PM from Tuesday on."}
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [
        FROM_THE_24TH, BEFORE_THE_24TH_ASKED]
    # The 1st's, left out at the new time - 7 on that day, which is 7 in UTC on GMT - and the 10th, which was
    # before the 24th, still left out as it was.
    assert calendar._session.sent[0]["json"] == CUT_AT_THE_24TH
    assert calendar._session.sent[1]["json"]["recurrence"] == [
        "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=7", "EXDATE;TZID=Europe/London:20261110T180000", "EXDATE:20261201T190000Z"]
    # Undone, the new series goes with them, the old one has its rules back, and the day is deleted again.
    assert reply["undo"]() == {"success": True, "message": "Put Gym back as it was."}
    assert calendar._session.sent[2:] == [_write("DELETE", new), _write("PATCH", GYM_URL, {"recurrence": GYM_RULES}),
                                          _write("DELETE", GYM_1ST_DELETED_URL)]


def test_a_split_leaves_a_day_deleted_on_its_own_out_of_an_all_day_series_as_a_date(for_changing):
    bins = _event_url("calendar-personal", "made-up-bins")
    new = _event_url("calendar-personal", "made-up-event")
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=8"]
    series = _series("Bins", "made-up-bins", "2026-11-03", "2026-11-04", *rules)
    deleted_url = _event_url("calendar-personal", "made-up-bins_20261201")
    calendar = for_changing(personal=[_occurrence("Bins", "made-up-bins", "2026-11-24", "2026-11-25"),
                                      _occurrence("Bins", "made-up-bins", "2026-12-01", "2026-12-02")],
                            answers={bins: [series], f"{bins}/instances": [
                                _events_page(_cancelled("made-up-bins", "2026-12-01")),
                                _events_page(_occurrence("Bins", "made-up-bins", "2026-11-03", "2026-11-04"),
                                             _occurrence("Bins", "made-up-bins", "2026-11-10", "2026-11-11"),
                                             _occurrence("Bins", "made-up-bins", "2026-11-17", "2026-11-18"))],
                                     f"PATCH {bins}": [series, series], f"POST {PERSONAL_EVENTS}": [_inserted()],
                                     f"DELETE {new}": [DELETED], f"DELETE {deleted_url}": [DELETED]})
    reply = calendar.change("bins", start="2026-11-24", end="2026-11-25", summary="Bins and recycling",
                            following=True, today=NOVEMBER_21)
    assert _said(reply)["success"]
    assert [params for url, params in calendar._session.asked if url == f"{bins}/instances"][0] == {
        "timeMin": "2026-11-24T00:00:00+00:00", "timeMax": "2028-11-23T00:00:00+00:00", "showDeleted": "true",
        "maxResults": 250}
    assert calendar._session.sent[1]["json"]["recurrence"] == [rules[0].replace("COUNT=8", "COUNT=5"),
                                                               "EXDATE;VALUE=DATE:20261201"]
    assert reply["undo"]() == {"success": True, "message": "Put Bins back as it was."}
    assert calendar._session.sent[2:] == [_write("DELETE", new), _write("PATCH", bins, {"recurrence": rules}),
                                          _write("DELETE", deleted_url)]


def test_a_time_left_out_of_a_new_series_is_at_its_time_of_day_on_that_day():
    # The new series' own time on the day deleted, in UTC as Google gives a series' own start: 7 PM
    # on GMT is 7 PM UTC, and on BST it's 6 PM. A time on a day deleted is by the day it was in London.
    exdates = google_calendar._exdates
    new = {"dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"}
    assert exdates([("a", datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)),
                    ("b", datetime(2027, 4, 6, 17, 0, tzinfo=timezone.utc))], new) == [
        "EXDATE:20261201T190000Z", "EXDATE:20270406T180000Z"]
    # Half past midnight on BST is the day before in UTC - but still its own day.
    assert exdates([("a", datetime(2027, 4, 5, 23, 30, tzinfo=timezone.utc))], new) == ["EXDATE:20270406T180000Z"]
    # One all day is left out by its date, whatever it was before.
    assert exdates([("a", date(2026, 12, 1)), ("b", datetime(2026, 12, 8, 18, 0, tzinfo=timezone.utc))],
                   {"date": "2026-11-24"}) == ["EXDATE;VALUE=DATE:20261201", "EXDATE;VALUE=DATE:20261208"]
    assert exdates([], new) == []


def test_a_count_counts_a_day_deleted_on_its_own_before_the_one_it_splits_at(for_changing):
    # Five from the 3rd of November, the 10th - day two - deleted on its own, split from the 17th, day three:
    # 3 are left, the 17th and the two after it, not 4.
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=5"]
    series = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *rules)
    tenth = _cancelled("made-up-gym", "2026-11-10T18:00:00Z")
    third = _occurrence("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z")
    seventeenth = _occurrence("Gym", "made-up-gym", "2026-11-17T18:00:00Z", "2026-11-17T19:00:00Z")
    both = _events_page(third, tenth)
    calendar = for_changing(personal=[seventeenth, GYM_24TH], answers={
        GYM_URL: [series], GYM_INSTANCES: [both, both], f"PATCH {GYM_URL}": [series],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    reply = calendar.change("gym", day=date(2026, 11, 17), start="2026-11-17 19:00", following=True,
                            today=date(2026, 11, 12))
    assert _said(reply)["success"]
    # The 10th is before the 17th, so it isn't one to leave out of the new series - it's one to count.
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [
        {"timeMin": "2026-11-17T18:00:00+00:00", "timeMax": "2028-11-16T18:00:00+00:00", "showDeleted": "true",
         "maxResults": 250},
        {"timeMax": "2027-01-16T18:00:00+00:00", "showDeleted": "true", "maxResults": 250}]
    assert calendar._session.sent[1]["json"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=3"]


def test_a_time_moved_from_before_the_split_to_after_it_is_still_counted_before_it(for_changing):
    # Five from the 3rd of November, split from the 24th: the 3rd, 10th and 17th were meant to be before it, so
    # two are left. The 17th was moved to the 25th, and Google's timeMax bounds that start, not the 17th - so it
    # is only read if the read goes on past the split, and counting it is what keeps the COUNT at 2, not 3.
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=5"]
    series = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *rules)
    seventeenth_moved = _occurrence("Gym", "made-up-gym", "2026-11-25T18:00:00Z", "2026-11-25T19:00:00Z",
                                    meant="2026-11-17T18:00:00Z")
    tenth_deleted = _cancelled("made-up-gym", "2026-11-10T18:00:00Z")
    first = _occurrence("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z")
    # What Google gives for timeMax: the times which start before it, as the fake below picks them - a deleted
    # one has no start, so by when it was meant to be.
    page = [first, tenth_deleted, seventeenth_moved, GYM_24TH, GYM_1ST]
    calendar = for_changing(personal=[seventeenth_moved, GYM_24TH, GYM_1ST], answers={
        GYM_URL: [series], GYM_INSTANCES: [_events_page(), _events_page()], f"PATCH {GYM_URL}": [series],
        f"POST {PERSONAL_EVENTS}": [_inserted()]})
    session = calendar._session
    asked = session.get

    def get(url, params=None, **kwargs):
        answer = asked(url, params=params, **kwargs)
        if "timeMax" in (params or {}) and "timeMin" not in params:
            limit = datetime.fromisoformat(params["timeMax"])
            answer.body["items"] = [each for each in page if google_calendar._sort_key(
                google_calendar._google_when(each.get("start") or each["originalStartTime"])) < limit]
        return answer

    session.get = get
    assert _said(calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21))["success"]
    assert calendar._session.sent[1]["json"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=2"]


@pytest.mark.parametrize("answer", [_refused(404, "notFound"), _refused(410, "deleted")])
def test_a_day_already_gone_when_it_is_deleted_again_is_still_all_back(for_changing, answer):
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [GYM], GYM_INSTANCES: [GYM_FROM_THE_24TH], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {GYM_1ST_DELETED_URL}": [answer]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == BACK_FROM_TUESDAY
    assert calendar._session.sent[-1] == _write("DELETE", GYM_1ST_DELETED_URL)


@pytest.mark.parametrize("answer", [
    # Sent, but whether it was deleted can't be told - so it isn't sent again.
    _refused(500, "backendError"), requests.ReadTimeout("Read timed out."), FakeResponse(200, "not an answer"),
    # Refused, or out of reach.
    _refused(403), _refused(429, "rateLimitExceeded"), requests.ConnectTimeout("Connection timed out.")])
def test_a_day_which_cant_be_deleted_again_is_said_plainly(for_changing, caplog, answer):
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [GYM], GYM_INSTANCES: [GYM_FROM_THE_24TH], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {GYM_1ST_DELETED_URL}": [answer, answer]})
    with caplog.at_level(logging.INFO):
        assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == DEC_1ST_CAME_BACK
    # Once only, and neither the event nor its calendar's id is logged.
    assert [each["method"] for each in calendar._session.sent] == ["PATCH", "PATCH", "DELETE"]
    assert caplog.records and not any("calendar-" in record.getMessage() or "Gym" in record.getMessage()
                                      for record in caplog.records)


def test_every_day_deleted_on_its_own_is_tried_again_and_those_which_came_back_are_said(for_changing):
    eighth = _cancelled("made-up-gym", "2026-12-08T18:00:00Z")
    eighth_url = _event_url("calendar-personal", "made-up-gym_20261208T180000Z")
    first = _event_url("calendar-personal", "made-up-gym_20261201T180000Z")
    page = _events_page(GYM_24TH, GYM_1ST_DELETED, eighth)
    # The first can't be, but that doesn't stop the second.
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [GYM], GYM_INSTANCES: [page], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {first}": [_refused(403)], f"DELETE {eighth_url}": [DELETED]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == DEC_1ST_CAME_BACK
    assert [each["url"] for each in calendar._session.sent[2:]] == [first, eighth_url]
    # Both, said together.
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [GYM], GYM_INSTANCES: [page], f"PATCH {GYM_URL}": [GYM, GYM],
        f"DELETE {first}": [_refused(403)], f"DELETE {eighth_url}": [requests.ReadTimeout("Read timed out.")]})
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]() == {
        "success": False, "message": "Put Gym back from Tuesday on, but these times came back: Tuesday the 1st of "
                                     "December and Tuesday the 8th of December - remove them in Google Calendar."}

    # Not as many as are named at once.
    said = google_calendar._still_back([date(2026, 12, day) for day in (1, 8, 15, 22, 29)], NOVEMBER_21)
    assert said == ("these times came back: Tuesday the 1st of December, Tuesday the 8th of December, Tuesday the "
                    "15th of December and 2 more - remove them in Google Calendar")
    # Today and tomorrow are said as they are anywhere.
    assert google_calendar._still_back([date(2026, 11, 22)], NOVEMBER_21) == \
        "tomorrow's time came back - remove it in Google Calendar"


def test_when_the_days_deleted_on_their_own_cant_be_read_nothing_is_changed(for_changing):
    unreachable = requests.ConnectionError("Network down")
    first = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)

    def remove(calendar):
        return calendar.remove("gym", following=True, today=NOVEMBER_21)

    def split(calendar):
        return calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)

    # The cut, the whole series from the first time, and a split - which reads them before it counts.
    for series, ask in ((GYM, remove), (first, remove), (GYM, split)):
        calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
            GYM_URL: [series], GYM_INSTANCES: [unreachable], f"PATCH {GYM_URL}": [series],
            f"DELETE {GYM_URL}": [DELETED], f"POST {PERSONAL_EVENTS}": [_inserted()]})
        assert ask(calendar) == {"success": False, "message": CANT_REACH_GOOGLE}
        assert calendar._session.sent == []

    # Answers of the wrong shape are no better.
    for page in (_events_page({"kind": "calendar#event", "status": "cancelled", "id": "made-up-gym_20261201T180000Z"}),
                 {"items": "not a list"}):
        calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
            GYM_URL: [GYM], GYM_INSTANCES: [page], f"PATCH {GYM_URL}": [GYM]})
        assert calendar.remove("gym", following=True, today=NOVEMBER_21) == {
            "success": False, "message": CANT_REACH_GOOGLE}
        assert calendar._session.sent == []

    # Nor is a split which can read those deleted, but not count the times before.
    calendar = for_changing(**_splitting(**{GYM_INSTANCES: [GYM_FROM_THE_24TH, unreachable]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._session.sent == []


def test_a_series_whose_rules_cant_be_used_is_left_as_it_was_and_said_so(for_changing):
    said = {"success": False, "message": "Google's answer for that repeating event didn't make sense, so I've left "
                                         "it as it was."}
    # No rules at all, or not a list of them - which Google's answer for a repeating event always has.
    for series in ({key: value for key, value in GYM.items() if key != "recurrence"}, {**GYM, "recurrence": "RRULE"},
                   {**GYM, "recurrence": [7]}):
        for ask in (lambda calendar: calendar.remove("gym", following=True, today=NOVEMBER_21),
                    lambda calendar: calendar.change("gym", start="2026-11-24 19:00", following=True,
                                                     today=NOVEMBER_21)):
            calendar = for_changing(**_splitting(**{GYM_URL: [series]}))
            assert ask(calendar) == said, series
            assert calendar._session.sent == []

    # A COUNT with no times left once those before the split are taken off - or which isn't a number: the
    # split is refused before anything is sent.
    for rule in ("RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=3", "RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=many"):
        counted = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", rule)
        calendar = for_changing(**_splitting(**{GYM_URL: [counted]}))
        assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == said, rule
        assert calendar._session.sent == []
        # Just a cut, which has no times to count, is made all the same.
        calendar = for_changing(**_splitting(**{GYM_URL: [counted], f"PATCH {GYM_URL}": [counted]}))
        assert calendar.remove("gym", following=True, today=NOVEMBER_21)["success"] is True


def test_a_split_that_fails_puts_the_days_deleted_on_their_own_back_to_deleted(for_changing):
    new_failed = _refused(400, "invalid")
    answers = {GYM_INSTANCES: [GYM_FROM_THE_24TH, _events_page(*BEFORE_THE_24TH)], f"PATCH {GYM_URL}": [GYM, GYM],
               f"POST {PERSONAL_EVENTS}": [new_failed], f"DELETE {GYM_1ST_DELETED_URL}": [DELETED]}
    calendar = for_changing(**_splitting(**answers))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I couldn't change them, so I've left Gym as it was."}
    assert [each["method"] for each in calendar._session.sent] == ["PATCH", "POST", "PATCH", "DELETE"]
    assert calendar._session.sent[-1] == _write("DELETE", GYM_1ST_DELETED_URL)

    # One which can't be said, rather than left as it was.
    calendar = for_changing(**_splitting(**{**answers, f"DELETE {GYM_1ST_DELETED_URL}": [_refused(403)]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I couldn't change them, and I've put Gym back, but Tuesday the 1st of "
                                     "December's time came back - remove it in Google Calendar."}

    # The new series unanswered: the old one is put back all the same, with the day deleted again, and
    # the user is told to check, as before.
    unanswered = requests.ReadTimeout("Read timed out.")
    calendar = for_changing(**_splitting(**{**answers, f"POST {PERSONAL_EVENTS}": [unanswered]}))
    assert calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I can't tell whether the new Gym was added, so check Google Calendar."}
    assert calendar._session.sent[-1] == _write("DELETE", GYM_1ST_DELETED_URL)


def test_a_series_with_no_end_is_cut_and_split_with_the_days_deleted_inside_the_window(for_changing):
    # Every Tuesday with no UNTIL or COUNT: its times go on for ever, so only two years from the cut are
    # asked for - and a day deleted inside that is still deleted again by the undo, or left out of a split.
    rules = ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
    endless = _series("Gym", "made-up-gym", "2026-11-03T18:00:00Z", "2026-11-03T19:00:00Z", *rules)
    # A day a year on, in BST: the 6th of April 2027 at 6 PM London is 5 PM in UTC.
    deleted = _cancelled("made-up-gym", "2027-04-06T17:00:00Z")
    deleted_url = _event_url("calendar-personal", "made-up-gym_20270406T170000Z")
    page = _events_page(GYM_24TH, GYM_1ST, deleted)

    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [endless], GYM_INSTANCES: [page], f"PATCH {GYM_URL}": [endless, endless],
        f"DELETE {deleted_url}": [DELETED]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed Gym from Tuesday on."}
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [FROM_THE_24TH]
    assert calendar._session.sent == [_write("PATCH", GYM_URL, {
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z"]})]
    assert reply["undo"]() == BACK_FROM_TUESDAY
    assert calendar._session.sent[1:] == [_write("PATCH", GYM_URL, {"recurrence": rules}),
                                          _write("DELETE", deleted_url)]

    # Split: no COUNT, so nothing is counted - the one read. The new series has the same rule, with the day
    # left out at the new time, 7 PM London - 6 PM in UTC on BST.
    new = _event_url("calendar-personal", "made-up-event")
    calendar = for_changing(personal=[GYM_24TH, GYM_1ST], answers={
        GYM_URL: [endless], GYM_INSTANCES: [page], f"PATCH {GYM_URL}": [endless, endless],
        f"POST {PERSONAL_EVENTS}": [_inserted()], f"DELETE {new}": [DELETED], f"DELETE {deleted_url}": [DELETED]})
    reply = calendar.change("gym", start="2026-11-24 19:00", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved Gym to 7 PM from Tuesday on."}
    assert [params for url, params in calendar._session.asked if url == GYM_INSTANCES] == [FROM_THE_24TH]
    assert calendar._session.sent[1]["json"]["recurrence"] == [*rules, "EXDATE:20270406T180000Z"]
    assert reply["undo"]() == {"success": True, "message": "Put Gym back as it was."}
    assert calendar._session.sent[2:] == [_write("DELETE", new), _write("PATCH", GYM_URL, {"recurrence": rules}),
                                          _write("DELETE", deleted_url)]


# "All of them" for events which aren't one series

def _session(day, hour=8, summary="Gym", calendar_id="calendar-personal", **more):
    """One gym session made on its own - a one-off an hour long on a day of November 2026, not a series."""
    return _own(summary, f"2026-11-{day:02d}T{hour:02d}:00:00Z", f"2026-11-{day:02d}T{hour + 1:02d}:00:00Z",
                f"made-up-session-{day}", calendar_id=calendar_id, **more)


# Five made one by one, Monday the 23rd to Friday the 27th at 8 AM.
SESSIONS = [_session(day) for day in range(23, 28)]


def _answers(events, method, *answers, calendar_id="calendar-personal"):
    """What Google answers, in turn, to the writes to each of the events."""
    return {f"{method} {_event_url(calendar_id, event['id'])}": list(answers) for event in events}


def _moved(day, start="19:00", end="20:00"):
    """What is patched onto a session to move it to a time on its own day."""
    return {"start": {"dateTime": f"2026-11-{day}T{start}:00+00:00"}, "end": {"dateTime": f"2026-11-{day}T{end}:00+00:00"}}


def test_all_of_them_moves_separate_events_each_on_its_own_day(for_changing):
    # Five gym sessions made one by one are five events, not a series, so "all of them" has no series
    # to move: each is moved to the time, on its own day.
    calendar = for_changing(personal=SESSIONS, answers=_answers(SESSIONS, "PATCH", CHANGED, CHANGED))
    reply = calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved 5 Gym events to 7 PM."}
    assert calendar._session.sent == [_write("PATCH", _event_url("calendar-personal", event["id"]), _moved(day))
                                      for day, event in zip(range(23, 28), SESSIONS)]

    # One undo puts each back as it was, the last first.
    calendar._session.sent.clear()
    assert reply["undo"]() == {"success": True, "message": "Put the 5 Gym events back as they were."}
    assert calendar._session.sent == [
        _write("PATCH", _event_url("calendar-personal", event["id"]),
               {"start": event["start"], "end": event["end"]}) for event in reversed(SESSIONS)]


def test_all_of_them_removes_separate_events_from_the_day_said_on(for_changing):
    # Google is asked from that day on - not for that day alone - so the 23rd and 24th aren't read.
    from_wednesday = SESSIONS[2:]
    calendar = for_changing(personal=from_wednesday, answers={**_answers(from_wednesday, "DELETE", DELETED),
                                                              **_answers(from_wednesday, "PATCH", CHANGED)})
    reply = calendar.remove("gym", day=date(2026, 11, 25), following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed 3 Gym events from Wednesday on."}
    [(_, window)] = [read for read in _read(calendar) if read[0] == "calendar-personal"]
    assert (window["timeMin"], window["timeMax"]) == ("2026-11-25T00:00:00+00:00", "2027-02-23T00:00:00+00:00")
    assert calendar._session.sent == [_write("DELETE", _event_url("calendar-personal", event["id"]))
                                      for event in from_wednesday]

    calendar._session.sent.clear()
    assert reply["undo"]() == {"success": True, "message": "Put the 3 Gym events back."}
    assert calendar._session.sent == [
        _write("PATCH", _event_url("calendar-personal", event["id"]), {"status": "confirmed"})
        for event in reversed(from_wednesday)]

    # With no day it's from now - and says no day.
    calendar = for_changing(personal=SESSIONS, answers=_answers(SESSIONS, "DELETE", DELETED))
    assert _said(calendar.remove("gym", following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Removed 5 Gym events."}
    [(_, window)] = [read for read in _read(calendar) if read[0] == "calendar-personal"]
    assert window["timeMin"] == "2026-11-21T00:00:00+00:00"


def test_all_of_them_renames_separate_events(for_changing):
    calendar = for_changing(personal=SESSIONS, answers=_answers(SESSIONS, "PATCH", CHANGED, CHANGED))
    reply = calendar.change("gym", summary="Gym class", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Renamed 5 Gym events to Gym class."}
    assert calendar._session.sent == [_write("PATCH", _event_url("calendar-personal", event["id"]),
                                             {"summary": "Gym class"}) for event in SESSIONS]
    calendar._session.sent.clear()
    assert reply["undo"]()["message"] == "Put the 5 Gym events back as they were."
    assert calendar._session.sent == [_write("PATCH", _event_url("calendar-personal", event["id"]),
                                             {"summary": "Gym"}) for event in reversed(SESSIONS)]

    # Moved and renamed in one sentence.
    calendar = for_changing(personal=SESSIONS, answers=_answers(SESSIONS, "PATCH", CHANGED))
    assert calendar.change("gym", start="19:00", summary="Gym class", following=True, today=NOVEMBER_21)[
        "message"] == "Moved 5 Gym events to 7 PM and renamed them Gym class."


def test_all_of_them_gives_each_series_one_unit_beside_the_one_offs(for_changing):
    # The series from the 24th is cut once, however many times it's on, and the one-offs each go.
    first, last = _session(23), _session(27)
    cut = {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261124T175959Z",
                          "EXDATE;TZID=Europe/London:20261110T180000"]}
    calendar = for_changing(personal=[first, GYM_24TH, GYM_1ST, last], answers={
        **_answers([first, last], "DELETE", DELETED), **_answers([first, last], "PATCH", CHANGED),
        GYM_URL: [GYM], f"{GYM_URL}/instances": [_events_page()], f"PATCH {GYM_URL}": [GYM, GYM]})
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Removed 3 Gym events."}
    assert calendar._session.sent == [_write("DELETE", _event_url("calendar-personal", first["id"])),
                                      _write("PATCH", GYM_URL, cut),
                                      _write("DELETE", _event_url("calendar-personal", last["id"]))]
    calendar._session.sent.clear()
    assert reply["undo"]() == {"success": True, "message": "Put the 3 Gym events back."}
    assert calendar._session.sent == [_write("PATCH", _event_url("calendar-personal", last["id"]),
                                             {"status": "confirmed"}),
                                      _write("PATCH", GYM_URL, {"recurrence": GYM_RULES}),
                                      _write("PATCH", _event_url("calendar-personal", first["id"]),
                                             {"status": "confirmed"})]


def test_all_of_them_moves_a_series_and_the_one_offs_beside_it(for_changing):
    # The series from its first time on is changed itself, with its own zone.
    series = _series("Gym", "made-up-gym", "2026-11-24T18:00:00+00:00", "2026-11-24T19:00:00+00:00", *GYM_RULES)
    one_off = _session(23)
    calendar = for_changing(personal=[one_off, GYM_24TH, GYM_1ST], answers={
        **_answers([one_off], "PATCH", CHANGED), GYM_URL: [series], f"PATCH {GYM_URL}": [series]})
    assert _said(calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Moved 2 Gym events to 7 PM."}
    assert calendar._session.sent == [
        _write("PATCH", _event_url("calendar-personal", one_off["id"]), _moved(23)),
        _write("PATCH", GYM_URL, {"start": {"dateTime": "2026-11-24T19:00:00+00:00", "timeZone": "Europe/London"},
                                  "end": {"dateTime": "2026-11-24T20:00:00+00:00", "timeZone": "Europe/London"}})]


def test_all_of_them_leaves_alone_an_event_which_has_started(for_changing):
    # Even a one-off: said at noon on the 23rd, the 8 AM session that day is over, so it's from the 24th.
    now = datetime(2026, 11, 23, 12, 0, tzinfo=LONDON)
    to_come = SESSIONS[1:]
    calendar = for_changing(personal=SESSIONS, answers=_answers(to_come, "DELETE", DELETED))
    assert _said(calendar.remove("gym", following=True, now=now)) == {
        "success": True, "message": "Removed 4 Gym events."}
    assert calendar._session.sent == [_write("DELETE", _event_url("calendar-personal", event["id"]))
                                      for event in to_come]
    # Not following, a one-off already past is still removed: only "all of them" keeps the record.
    calendar = for_changing(personal=SESSIONS[:1], answers=_answers(SESSIONS[:1], "DELETE", DELETED))
    assert calendar.remove("gym", now=now)["success"]

    # With every one started there's nothing to change.
    calendar = for_changing(personal=SESSIONS, reads=2)
    later = datetime(2026, 11, 28, 12, 0, tzinfo=LONDON)
    assert calendar.remove("gym", following=True, now=later) == GONE
    assert calendar.change("gym", start="19:00", following=True, now=later) == GONE
    assert calendar._session.sent == []


def test_all_of_them_leaves_out_what_isnt_nova_to_change_and_says_so(for_changing):
    dinner = _google_event("Gym dinner", "2026-11-26T19:00:00Z", "2026-11-26T21:00:00Z", id="made-up-dinner",
                           organizer={"email": "someone@example.com", "self": False})
    uni = _session(26, calendar_id="calendar-uni")
    calendar = for_changing(personal=[*SESSIONS[:3], dinner], uni=[uni], answers=_answers(SESSIONS[:3], "DELETE", DELETED))
    assert _said(calendar.remove("gym", following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Removed 3 Gym events, and left 2 I can't change."}
    assert calendar._session.sent == [_write("DELETE", _event_url("calendar-personal", event["id"]))
                                      for event in SESSIONS[:3]]

    # One left beside one: said the same, after what one says.
    calendar = for_changing(personal=SESSIONS[:1], uni=[uni], answers=_answers(SESSIONS[:1], "DELETE", DELETED))
    assert _said(calendar.remove("gym", following=True, today=NOVEMBER_21)) == {
        "success": True, "message": "Removed Gym, Monday at 8 AM, and left 1 I can't change."}

    # None of them Nova's to change: said as it is for one.
    calendar = for_changing(uni=[uni, _session(27, calendar_id="calendar-uni")])
    assert calendar.remove("gym", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "I can't change that, Google only lets me change your own calendars."}
    calendar = for_changing(personal=[dinner])
    assert calendar.remove("gym", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "That's an invite from someone else. Change it in Google Calendar."}
    assert calendar._session.sent == []


def test_all_of_them_is_for_twenty_events_at_most(for_changing):
    # A mishearing mustn't empty the calendar: 21 events is refused, with the count, and nothing is read of them.
    many = [_session(day) for day in range(22, 31)] + [_own("Gym", f"2026-12-{day:02d}T08:00:00Z",
                                                            f"2026-12-{day:02d}T09:00:00Z", f"made-up-december-{day}")
                                                       for day in range(1, 13)]
    calendar = for_changing(personal=many, reads=2)
    refused = {"success": False, "message": "That's 21 events - say a day to start from, or fewer."}
    assert calendar.remove("gym", following=True, today=NOVEMBER_21) == refused
    assert calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21) == refused
    assert calendar._session.sent == []

    # Twenty is done.
    calendar = for_changing(personal=many[:20], answers=_answers(many[:20], "DELETE", DELETED))
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["message"] == "Removed 20 Gym events."
    assert len(calendar._session.sent) == 20


def test_all_of_them_still_only_changes_the_time(for_changing):
    # Another day for each would be a new day for all of them, as for a series - and nothing is sent.
    calendar = for_changing(personal=SESSIONS, reads=2)
    for start in ("2026-11-25 19:00", "2026-11-25"):
        assert calendar.change("gym", start=start, following=True, today=NOVEMBER_21) == {
            "success": False, "message": "I can only change the time for all of them. Move the days in Google "
                                         "Calendar."}, start
    assert calendar._session.sent == []


def test_a_write_which_fails_part_way_leaves_the_rest_and_keeps_the_undo(for_changing):
    # The third of five is refused: the first two stay moved, the last two are never sent, and what was done
    # and what wasn't is said - with one undo for the two.
    answers = _answers(SESSIONS, "PATCH", CHANGED, CHANGED)
    answers[f"PATCH {_event_url('calendar-personal', SESSIONS[2]['id'])}"] = [_refused(403)]
    calendar = for_changing(personal=SESSIONS, answers=answers)
    reply = calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21)
    assert _said(reply) == {"success": True, "message": "Moved 2 of the 5 Gym events to 7 PM. Google wouldn't change "
                                                        "that. I haven't touched the other 2."}
    assert [sent["url"] for sent in calendar._session.sent] == [_event_url("calendar-personal", event["id"])
                                                                for event in SESSIONS[:3]]
    calendar._session.sent.clear()
    assert reply["undo"]() == {"success": True, "message": "Put the 2 Gym events back as they were."}
    assert [sent["url"] for sent in calendar._session.sent] == [_event_url("calendar-personal", event["id"])
                                                                for event in (SESSIONS[1], SESSIONS[0])]

    # Not sure whether one went: said as that, and never sent again.
    answers = _answers(SESSIONS, "DELETE", DELETED)
    answers[f"DELETE {_event_url('calendar-personal', SESSIONS[1]['id'])}"] = [requests.ReadTimeout("Read timed out.")]
    calendar = for_changing(personal=SESSIONS, answers=answers)
    assert calendar.remove("gym", following=True, today=NOVEMBER_21)["message"] == (
        "Removed 1 of the 5 Gym events. I can't tell whether Gym was removed. I haven't touched the other 3.")

    # The first failing is just a failure: nothing was done, and there's nothing to undo.
    answers = _answers(SESSIONS, "DELETE", _refused(403))
    calendar = for_changing(personal=SESSIONS, answers=answers)
    assert calendar.remove("gym", following=True, today=NOVEMBER_21) == {
        "success": False, "message": "Google wouldn't change that."}
    assert len(calendar._session.sent) == 1

    # A series with guests, as for one alone, is the one that isn't done.
    guests = {**GYM, "attendees": [{"email": "someone@example.com"}]}
    one_off = _session(23)
    calendar = for_changing(personal=[one_off, GYM_24TH, GYM_1ST], answers={
        **_answers([one_off], "PATCH", CHANGED), GYM_URL: [guests]})
    assert calendar.change("gym", start="19:00", following=True, today=NOVEMBER_21)["message"] == (
        "Moved 1 of the 2 Gym events to 7 PM. That has guests. Change it in Google Calendar.")


def test_an_undo_of_all_of_them_which_fails_for_some_says_how_many_came_back(for_changing):
    answers = _answers(SESSIONS, "DELETE", DELETED)
    answers.update(_answers(SESSIONS, "PATCH", CHANGED))
    answers[f"PATCH {_event_url('calendar-personal', SESSIONS[2]['id'])}"] = [requests.ReadTimeout("Read timed out.")]
    calendar = for_changing(personal=SESSIONS, answers=answers)
    reply = calendar.remove("gym", following=True, today=NOVEMBER_21)
    calendar._session.sent.clear()
    # Every one is tried - one failing doesn't stop the rest.
    assert reply["undo"]() == {"success": False, "message": "Put 4 of the 5 Gym events back. Check the rest in "
                                                            "Google Calendar."}
    assert len(calendar._session.sent) == 5

    # None of them.
    answers = _answers(SESSIONS, "DELETE", DELETED)
    answers.update(_answers(SESSIONS, "PATCH", requests.ReadTimeout("Read timed out.")))
    calendar = for_changing(personal=SESSIONS, answers=answers)
    undo = calendar.remove("gym", following=True, today=NOVEMBER_21)["undo"]
    assert undo() == {"success": False, "message": "I couldn't put the 5 Gym events back. Check them in Google "
                                                   "Calendar."}


# Telling Home Assistant a change was made
#
# GoogleCalendar.after_change is called, on a thread of its own, after each write Google says it
# did - which is what has Home Assistant read its calendars again.

@pytest.fixture
def told(monkeypatch):
    """
    Gives a calendar an after_change which notes each call - as the writes sent by then,
    each as (method, address) - run at once, where the calendar would have a thread run it.
    """
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)

    def watch(calendar):
        calls = []
        calendar.after_change = lambda: calls.append([(sent["method"], sent["url"])
                                                      for sent in calendar._session.sent])
        return calls

    return watch


def test_a_calendar_has_nobody_to_tell_until_it_is_given_one(for_adding, for_changing, make_calendar):
    assert for_adding().after_change is None
    assert for_changing().after_change is None
    assert make_calendar().after_change is None


def test_an_event_added_is_told_of_once_google_has_added_it(for_adding, told):
    calendar = for_adding()
    calls = told(calendar)
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)["success"] is True
    # Told after the write was sent, not before.
    assert calls == [[("POST", _events_url("calendar-personal"))]]


def test_an_event_removed_is_told_of_once_google_has_removed_it(for_changing, told):
    dentist = _event_url("calendar-personal", "made-up-dentist")
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"DELETE {dentist}": [DELETED]})
    calls = told(calendar)
    assert calendar.remove("dentist", today=NOVEMBER_21)["success"] is True
    assert calls == [[("DELETE", dentist)]]


def test_an_event_changed_is_told_of_once_google_has_changed_it(for_changing, told):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [CHANGED]})
    calls = told(calendar)
    assert calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21)["success"] is True
    assert calls == [[("PATCH", DENTIST_URL)]]


def test_a_series_split_is_told_of_after_its_writes(for_changing, told):
    # A split is two writes, so it may tell of each - at least once, after the first was sent.
    calendar = for_changing(**_splitting(**{f"PATCH {GYM_URL}": [GYM]}))
    calls = told(calendar)
    assert calendar.change("gym", start="19:00", following=True, today=date(2026, 11, 23))["success"] is True
    assert calls and calls[0][0] == ("PATCH", GYM_URL) and calls[-1][-1] == ("POST", PERSONAL_EVENTS)


def test_an_undo_is_told_of_too(for_changing, told):
    calendar = for_changing(personal=[DENTIST_THURSDAY],
                            answers={f"PATCH {DENTIST_URL}": [CHANGED, CHANGED]})
    calls = told(calendar)
    undo = calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21)["undo"]
    assert len(calls) == 1
    assert undo()["success"] is True
    assert calls[1:] == [[("PATCH", DENTIST_URL), ("PATCH", DENTIST_URL)]]


def test_a_write_sent_again_after_a_401_is_told_of_only_once_it_landed(for_changing, told):
    # Google turned the first down unread, so there was nothing to read again - the one that landed is told of.
    calendar = for_changing(personal=[DENTIST_THURSDAY],
                            answers={f"PATCH {DENTIST_URL}": [_refused(401, "authError"), CHANGED]})
    calls = told(calendar)
    assert calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21)["success"] is True
    assert len(calendar._session.sent) == 2 and len(calls) == 1 and len(calls[0]) == 2


@pytest.mark.parametrize("answer", [
    # Refused, or the event gone.
    _refused(400, "invalid"), _refused(403), _refused(404, "notFound"), _refused(410, "deleted"),
    # Google's own trouble, and sent but never answered - which may have been done, but isn't known to be.
    _refused(500, "backendError"), FakeResponse(504, "Gateway Timeout"), requests.ReadTimeout("Read timed out."),
    FakeResponse(200, "not an answer"),
    # Never sent, or never accepted.
    requests.ConnectTimeout("Connection timed out."), _refused(401, "authError")])
def test_a_write_which_didnt_land_is_told_of_to_nobody(for_changing, told, answer):
    calendar = for_changing(personal=[DENTIST_THURSDAY], answers={f"PATCH {DENTIST_URL}": [answer, answer]})
    calls = told(calendar)
    assert calendar.change("dentist", start="2026-11-27 16:00", today=NOVEMBER_21)["success"] is False
    assert calls == []


def test_an_add_which_didnt_land_is_told_of_to_nobody(for_adding, told):
    calendar = for_adding(answers=[_refused(403), _refused(403)])
    calls = told(calendar)
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)["success"] is False
    assert calls == []


def test_a_read_is_told_of_to_nobody(make_calendar, told):
    calendar = make_calendar([_google_event("Dentist", "2026-10-22T09:30:00+01:00")])
    calls = told(calendar)
    assert [event["summary"] for event in calendar.events(date(2026, 10, 22), 1)] == ["Dentist"]
    assert calls == [] and calendar._session.sent == []


def test_a_failing_after_change_leaves_the_reply_as_it_was(for_adding, monkeypatch, caplog):
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)
    calendar = for_adding()
    calendar.after_change = lambda: (_ for _ in ()).throw(RuntimeError("made-up trouble with Dentist"))
    with caplog.at_level(logging.INFO):
        assert _said(calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)) == {
            "success": True, "message": "Added Dentist, Thursday the 29th at 3 PM."}
    # Logged, as only the kind of error - never what it said, which could hold the event.
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert "RuntimeError" in warning.getMessage() and "Dentist" not in warning.getMessage()


def test_the_calendar_never_waits_for_after_change(for_adding, monkeypatch):
    started = []

    class Waits:
        """Stands in for threading.Thread, starting nothing - work for the thread alone to do."""

        def __init__(self, target, **kwargs):
            started.append((target, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(google_calendar.threading, "Thread", Waits)
    calendar = for_adding()
    called = []
    calendar.after_change = lambda: called.append(True)
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)["success"] is True
    # Left to a daemon thread, so Home Assistant being slow, or the brain stopping, waits for nothing.
    assert called == [] and len(started) == 1 and started[0][1]["daemon"] is True
    started[0][0]()
    assert called == [True]
