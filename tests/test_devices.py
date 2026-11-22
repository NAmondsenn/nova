"""Tests for saying where the user's phone, tablet or Mac is, and ringing it - see devices.py."""

import json
from types import SimpleNamespace

import pytest

from voice_assistant.batteries import find
from voice_assistant.devices import AppDevice, FindMyDevice, RangLately, find_my_for, ring_call, where
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.home_assistant import HomeAssistant, NotAllowed

NOW = 1_000_000_000.0
TEN_MINUTES_AGO = NOW - 600


def _found(name="s25-ultra", maker="samsung", model="SM-S938B", notify="mobile_app_s25_ultra", tracker="not_home",
           reported=TEN_MINUTES_AGO, place=None):
    """One of HomeAssistant.app_devices()'s dicts - the S25's, unless a test says otherwise."""
    return {"device": f"dev_{name}", "name": name, "maker": maker, "model": model, "notify": notify,
            "tracker": None if tracker is None else {"state": tracker, "reported": reported}, "place": place}


PHONE = dict(name="s25-ultra", maker="samsung", model="SM-S938B")
TABLET = dict(name="tab-s11-ultra", maker="samsung", model="SM-X930", notify="mobile_app_tab_s11_ultra")
MAC = dict(name="macbook-pro", maker="Apple", model="Mac16,8", notify="mobile_app_macbook_pro")
IPHONE = dict(name="iphone", maker="Apple", model="iPhone17,1", notify="mobile_app_iphone")


def test_an_app_device_is_built_from_what_home_assistant_says():
    phone = AppDevice.from_home(_found(place={"state": "1 Example Road, Sometown, EX1 1AA, UK",
                                              "thoroughfare": "Example Road", "locality": "Sometown"}))
    assert phone == AppDevice(id="dev_s25-ultra", name="S25 Ultra", kind="mobile", phone=True, computer=False,
                              apple=False, notify="mobile_app_s25_ultra", tracker="not_home",
                              reported=TEN_MINUTES_AGO, place="Example Road, Sometown")
    mac = AppDevice.from_home(_found(**MAC, tracker=None))
    assert (mac.kind, mac.phone, mac.computer, mac.apple, mac.tracker, mac.reported) == \
        ("other", False, True, True, None, None)


def test_a_device_is_found_by_what_was_said_as_a_battery_is():
    devices = [AppDevice.from_home(_found(**found)) for found in (PHONE, TABLET, MAC)]
    assert [d.name for d in find(devices, "phone")] == ["S25 Ultra"]
    assert [d.name for d in find(devices, "tablet")] == ["Tab S11 Ultra"]
    assert [d.name for d in find(devices, "mac")] == ["MacBook Pro"]
    assert find(devices, "kettle") == []


def test_where_a_device_is_is_said_from_its_tracker():
    def said(**found):
        return where(AppDevice.from_home(_found(**found)), NOW)

    assert said(tracker="home") == "Your phone's at home."
    assert said(tracker="Work") == "Your phone's at Work."
    assert said(place={"state": "1 Example Road, Sometown, EX1 1AA, UK", "thoroughfare": "Example Road",
                       "locality": "Sometown"}) == "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    # Without the parts, the first two of the sensor's state.
    assert said(place={"state": "1 Example Road, Sometown, EX1 1AA, UK", "thoroughfare": None, "locality": None}) == \
        "Your phone's near 1 Example Road, Sometown, as of 10 minutes ago."
    # The Android app gives a town it hasn't got as the word "null" (live, 25 Oct), so it's only the street.
    for missing in ("null", "None", "unknown", "Unavailable", " ", None):
        assert said(place={"state": "12 Example Road, null", "thoroughfare": "Example Road",
                           "locality": missing}) == "Your phone's near Example Road, as of 10 minutes ago."
    assert said(place={"state": "null", "thoroughfare": "NULL", "locality": "null"}) == \
        "Your phone's away from home, as of 10 minutes ago."
    # A part that isn't text is still said as text, without its spaces.
    assert said(place={"state": "", "thoroughfare": " Example Road ", "locality": 12}) == \
        "Your phone's near Example Road, 12, as of 10 minutes ago."
    assert said() == "Your phone's away from home, as of 10 minutes ago."
    assert said(place={"state": "unknown", "thoroughfare": None, "locality": None}) == \
        "Your phone's away from home, as of 10 minutes ago."
    assert said(tracker=None) == "I can't tell where your phone is."
    assert said(tracker="unavailable") == "I can't tell where your phone is."
    # A tablet or a Mac is said as that word.
    assert said(**TABLET, tracker="home") == "Your tablet's at home."
    assert said(**MAC, tracker="home") == "Your Mac's at home."


def test_each_kind_of_device_is_rung_its_own_way():
    assert ring_call(AppDevice.from_home(_found(**PHONE))) == {
        "message": "TTS", "data": {"tts_text": "Nova's looking for your phone.", "media_stream": "alarm_stream_max",
                                   "priority": "high", "ttl": 0}}
    assert ring_call(AppDevice.from_home(_found(**IPHONE))) == {
        "message": "Nova's looking for your iPhone.",
        "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}
    assert ring_call(AppDevice.from_home(_found(**MAC))) == {
        "message": "Nova's looking for your Mac.", "data": {"push": {"sound": "default"}}}


# Google's Find My Device, through Home Assistant's googlefindmy integration

def _find_my_entry(entity, device, translation_key, original_name=None, platform="googlefindmy"):
    """One of the integration's entities, as the registry has it."""
    return Entry(entity, platform, device, None, f"uid_{entity}", original_name, (), False, "", translation_key)


def _find_my_home(trackers, buttons=(("button.thing_one", "2026-11-20T10:00:00+00:00"), ("button.tag_ring", "unknown"))):
    """
    A Home Assistant whose registry has a phone and a tracker tag of Find My's, and its own
    service device - with entity ids which say nothing of what each is, as a user may rename
    them - rendering the trackers' template with these, and keeping what it was asked.
    """
    entries = [
        _find_my_entry("button.thing_one", "dev_phone", "play_sound", "Play sound"),
        _find_my_entry("button.thing_two", "dev_phone", "stop_sound", "Stop sound"),
        _find_my_entry("button.thing_three", "dev_phone", "locate_device", "Locate now"),
        _find_my_entry("device_tracker.thing_four", "dev_phone", "device"),
        _find_my_entry("device_tracker.thing_five", "dev_phone", "last_location", "Last Location"),
        # A version without translation keys is still known by the names.
        _find_my_entry("button.tag_ring", "dev_tag", "", "Play sound"),
        _find_my_entry("device_tracker.tag", "dev_tag", ""),
        _find_my_entry("device_tracker.tag_last", "dev_tag", "", "Last Location"),
        # The service's own buttons ring nothing, and the HASS.Agent-free rest are never Find My's.
        _find_my_entry("button.service_regenerate_fcm_token", "dev_service", "regenerate_fcm_token",
                       "Regenerate FCM Token"),
        _find_my_entry("button.other_play_sound", "dev_other", "play_sound", "Play sound", platform="demo"),
    ]
    snapshot = Snapshot(entries={entry.entity_id: entry for entry in entries}, areas={}, read_at=0.0,
                        devices={"dev_phone": {"name": "Galaxy Phone 9"}, "dev_tag": {"name": "Sam's Wallet"},
                                 "dev_service": {"name": "Find Hub Service"}, "dev_other": {"name": "Demo"}})
    home = HomeAssistant("http://homeassistant.local:8123/", "token", registry=SimpleNamespace(snapshot=lambda: snapshot))
    home.asked = []

    def template(text, timeout=None):
        home.asked.append(text)
        return json.dumps({"trackers": trackers, "buttons": [list(button) for button in buttons]})
    home._template = template

    def request(method, path, timeout=None, **kwargs):
        home.asked.append((method, path, kwargs.get("json")))
        return SimpleNamespace(json=lambda: [])
    home._request = request
    return home


def test_find_my_devices_discovered_from_the_integration():
    home = _find_my_home([
        {"id": "device_tracker.thing_four", "state": "home", "seen": NOW - 60, "reported": NOW - 30},
        {"id": "device_tracker.thing_five", "state": "home", "seen": None, "reported": NOW - 30},
        # The tag's own tracker can't tell, as a tag's often can't, so where it was last seen is said.
        {"id": "device_tracker.tag", "state": "unknown", "seen": None, "reported": NOW - 5},
        {"id": "device_tracker.tag_last", "state": "not_home", "seen": None, "reported": TEN_MINUTES_AGO},
    ])
    found = sorted(home.find_my_devices(), key=lambda device: device["device"])
    assert found == [
        {"device": "dev_phone", "name": "Galaxy Phone 9", "play": "button.thing_one", "stop": "button.thing_two",
         "tracker": {"state": "home", "reported": NOW - 60}, "ready": True},
        # A button never pressed is "unknown", and still rings.
        {"device": "dev_tag", "name": "Sam's Wallet", "play": "button.tag_ring", "stop": None,
         "tracker": {"state": "not_home", "reported": TEN_MINUTES_AGO}, "ready": True},
    ]
    # Every tracker and Play sound button is read in one template, by the ids the registry gave.
    assert len(home.asked) == 1 and '["device_tracker.tag", "device_tracker.tag_last", "device_tracker.thing_five", ' \
        '"device_tracker.thing_four"]' in home.asked[0] and '["button.tag_ring", "button.thing_one"]' in home.asked[0]
    assert sorted(home.find_my_names()) == ["Galaxy Phone 9", "Sam's Wallet"]
    wallet = FindMyDevice.from_home(found[1])
    assert (wallet.name, wallet.said, wallet.place) == ("Sam's Wallet", "Wallet", None)
    assert where(wallet, NOW) == "Your Wallet's away from home, as of 10 minutes ago."

    # Only its Play sound and Stop sound may be pressed - never Locate now, the service's own, or another's.
    home.press("button.thing_one")
    home.press("button.thing_two")
    assert [sent for sent in home.asked[1:]] == [
        ("POST", "/api/services/button/press", {"entity_id": "button.thing_one"}),
        ("POST", "/api/services/button/press", {"entity_id": "button.thing_two"})]
    for refused in ("button.thing_three", "button.service_regenerate_fcm_token", "button.other_play_sound"):
        with pytest.raises(NotAllowed):
            home.press(refused)

    # While Google can't be reached its buttons are unavailable, and pressed do nothing - so it isn't ready.
    unreachable = _find_my_home([], buttons=[("button.thing_one", "unavailable")])
    assert {found["device"]: found["ready"] for found in unreachable.find_my_devices()} == \
        {"dev_phone": False, "dev_tag": False}
    assert FindMyDevice.from_home(unreachable.find_my_devices()[0]).ready is False

    # Without the registry's snapshot there's nothing to go by, so nothing is found.
    home.registry = SimpleNamespace(snapshot=lambda: None)
    assert home.find_my_devices() == [] and home.find_my_names() == []


def test_the_apps_device_and_find_mys_are_matched_by_their_names():
    phone, tablet, mac = (AppDevice.from_home(_found(**found)) for found in (PHONE, TABLET, MAC))
    galaxy_phone = FindMyDevice.from_home({"device": "f1", "name": "Galaxy S25 Ultra", "play": "button.a"})
    galaxy_tab = FindMyDevice.from_home({"device": "f2", "name": "Galaxy Tab S11 Ultra", "play": "button.b"})
    wallet = FindMyDevice.from_home({"device": "f3", "name": "Keys tag", "play": "button.c"})
    find_mys = [galaxy_phone, galaxy_tab, wallet]
    assert find_my_for(phone, find_mys) is galaxy_phone
    assert find_my_for(tablet, find_mys) is galaxy_tab
    # Google doesn't find the Mac.
    assert find_my_for(mac, find_mys) is None
    assert find_my_for(phone, [wallet]) is None
    # A name of one plain word says too little: a tag called "Ultra" or "Tab" is never the phone or tablet.
    for short in ("Ultra", "Tab", "tab"):
        tag = FindMyDevice.from_home({"device": "f4", "name": short, "play": "button.d"})
        assert find_my_for(phone, [tag]) is None and find_my_for(tablet, [tag]) is None, short
    # One word with a digit in it does.
    s25 = FindMyDevice.from_home({"device": "f5", "name": "S25", "play": "button.e"})
    assert find_my_for(phone, [s25]) is s25


def test_a_find_my_entity_with_a_key_of_its_own_isnt_taken_by_its_name():
    from voice_assistant.ha_registry import find_my_kind
    assert find_my_kind(_find_my_entry("button.a", "d", "play_sound", "Ring it")) == "play"
    assert find_my_kind(_find_my_entry("button.b", "d", "", "Play sound")) == "play"
    # A key the integration added since, whatever its name says, is something else.
    assert find_my_kind(_find_my_entry("button.c", "d", "play_sound_twice", "Play sound")) is None
    assert find_my_kind(_find_my_entry("device_tracker.e", "d", "history")) is None


def test_what_was_rung_lately_is_kept_for_ten_minutes_until_stopped():
    now = [NOW]
    rang = RangLately(clock=lambda: now[0])
    phone = FindMyDevice.from_home({"device": "f1", "name": "Galaxy Phone", "play": "button.a", "stop": "button.b"})
    wallet = FindMyDevice.from_home({"device": "f2", "name": "Wallet", "play": "button.c", "stop": "button.d"})
    rang.rang(phone)
    now[0] += 60
    rang.rang(wallet)
    assert rang.lately() == [phone, wallet]
    rang.stopped(phone)
    assert rang.lately() == [wallet]
    now[0] += 601
    assert rang.lately() == []
