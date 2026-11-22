"""
Action Executor Test
Tests how actions are dispatched, using the real VolumeControl (saving into a
temporary folder) and a fake Spotify controller, so nothing plays or goes online.
"""

import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from tests.fake_approvals import FakeApprovals
from voice_assistant import satellite_names, volume as volume_module
from voice_assistant.actions import NOT_A_COMMAND, ON_A_COMPUTER, Action, ActionExecutor, ActionResult, confirm
from voice_assistant.batteries import Battery
from voice_assistant.computers import ComputerControl
from voice_assistant.approvals import NO_WAY, pending_said
from voice_assistant.downloads import ON_ITS_WAY, Downloads, Wanted
from voice_assistant.home_assistant import NotAllowed
from voice_assistant.google_calendar import Removal
from voice_assistant.open_conversation import OpenConversation
from voice_assistant.undo import (NOTHING_TO_UNDO, SOMETHING_ELSE_SINCE, TOO_LATE, UNDO_CHANGE_KEPT,
                                   LastChange)
from voice_assistant.volume import VolumeControl


class FakeSpotify:
    """Stands in for SpotifyController, remembering the music volume it's given."""

    name = "Spotify"

    def __init__(self, playing, level=50):
        self.playing = playing
        self.level = level
        self.calls = []
        self.resume_cancelled = False

    def pause(self):
        self.calls.append("pause")
        return {"success": True, "message": "Paused", "chime": True}

    def cancel_resume(self):
        self.resume_cancelled = True

    def was_playing(self):
        return self.playing

    def get_volume(self):
        return self.level

    def set_volume(self, percent):
        self.level = int(percent)
        return {"success": True, "message": f"Volume {self.level} percent."}


class FakePlayer(FakeSpotify):
    """A satellite's music player in the room, which may have music paused on it."""

    def __init__(self, playing=False, paused=False, level=50):
        super().__init__(playing, level)
        self.paused = paused

    def now_playing(self):
        if not (self.playing or self.paused):
            return None
        return {"track": "Gooba", "playing": self.playing}

    def play(self, query=None, search_type=None):
        self.calls.append("play")
        return {"success": True, "message": "Resumed playback", "silent": True}


@pytest.fixture
def make_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(volume_module, "VOLUMES_FILE", str(tmp_path / "volumes.json"))

    def factory(spotify=None, **others):
        return ActionExecutor(music=spotify, volume=VolumeControl(), **others)

    return factory


def test_bare_volume_change_with_music_on_changes_the_music(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    result = executor.execute(Action("set_volume", {"direction": "down"}))
    assert result.ok and spotify.level == 40
    # The assistant's own voice is left where it was.
    assert executor.volume.percent("general") == 70


def test_bare_volume_change_with_nothing_playing_changes_the_assistant(make_executor):
    spotify = FakeSpotify(playing=False, level=50)
    executor = make_executor(spotify)
    executor.execute(Action("set_volume", {"direction": "up"}))
    assert executor.volume.percent("general") == 80 and spotify.level == 50


def test_saying_which_volume_still_wins(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    executor.execute(Action("set_volume", {"category": "general", "level": 30}))
    assert executor.volume.percent("general") == 30 and spotify.level == 50


def test_volume_without_spotify_falls_back_to_the_assistant(make_executor):
    executor = make_executor()
    executor.execute(Action("set_volume", {"level": 55}))
    assert executor.volume.percent("general") == 55


def test_volume_changes_are_confirmed_with_a_beep(make_executor):
    executor = make_executor(FakeSpotify(playing=False))
    result = executor.execute(Action("set_volume", {"category": "alarm", "direction": "down"}))
    assert (result.confirm, result.sound, result.sound_category) == ("sound", "volume_beep.wav", "alarm")


def test_unknown_tool_is_handled(make_executor):
    assert make_executor().execute(Action("play_netflix")).ok is False


def test_a_parameter_the_model_made_up_doesnt_stop_it_running(make_executor):
    result = make_executor().execute(Action("set_volume", {"level": 30, "loudness": "very"}))
    assert result.ok


# Each tool declared once

def test_every_tool_has_a_description_and_only_takes_what_it_declares():
    # Adding a tool used to mean three edits in two files, which could drift apart.
    import inspect
    for name, method in ActionExecutor.tools().items():
        declared = set(method.tool["parameters"])
        taken = set(list(inspect.signature(method).parameters)[1:])
        assert method.tool["description"], name
        assert declared == taken, name
        assert set(method.tool["required"]) <= declared, name


def test_only_tools_which_can_work_are_offered(make_executor):
    # The lights tool used to be offered while it could only fail.
    offered = {schema["name"] for schema in ActionExecutor(clock=object()).tool_schemas()}
    assert "set_timer" in offered and "play_music" not in offered and "get_weather" not in offered


def test_a_tool_asked_for_without_what_it_needs_says_so(make_executor):
    result = ActionExecutor().execute(Action("play_music", {"query": "Drake"}))
    assert result.ok is False and result.say == "The music isn't available right now."


def test_an_explicit_pause_keeps_the_music_paused_afterwards(make_executor):
    spotify = FakeSpotify(playing=True)
    result = make_executor(spotify).execute(Action("control_playback", {"action": "pause"}))
    assert result.confirm == "chime" and spotify.resume_cancelled


# The room acts as one

def test_pause_at_the_pc_pauses_the_pis_music(make_executor):
    pc, pi = FakePlayer(), FakePlayer(playing=True)
    result = make_executor(pc, room_players=lambda: [pc, pi]).execute(Action("control_playback", {"action": "pause"}))
    assert result.confirm == "chime"
    assert pi.calls == ["pause"] and pi.resume_cancelled
    assert pc.calls == [] and not pc.resume_cancelled

    # With nothing playing in the room, its own is paused, as before rooms.
    pc, pi = FakePlayer(), FakePlayer()
    make_executor(pc, room_players=lambda: [pc, pi]).execute(Action("control_playback", {"action": "pause"}))
    assert pc.calls == ["pause"] and pi.calls == []


def test_volume_at_pi_changes_only_pi_music(make_executor):
    # The PC is playing in the same room too, and is left alone: volume is the device spoken to's (0034).
    pi, pc = FakePlayer(playing=True, level=30), FakePlayer(playing=True, level=50)
    result = make_executor(pi, room_players=lambda: [pi, pc]).execute(Action("set_volume", {"direction": "up"}))
    assert result.ok and (pi.level, pc.level) == (40, 50)

    # The music named is this one's own, playing or not.
    pi, pc = FakePlayer(level=30), FakePlayer(playing=True, level=50)
    executor = make_executor(pi, room_players=lambda: [pi, pc])
    executor.execute(Action("set_volume", {"category": "music", "direction": "down"}))
    assert (pi.level, pc.level) == (20, 50)
    # With only the PC's music playing, "turn it down" at the Pi is the Pi's own voice.
    executor.execute(Action("set_volume", {"direction": "down"}))
    assert (pi.level, pc.level) == (20, 50) and executor.volume.percent("general") == 60


def test_resume_prefers_its_own_paused_music(make_executor):
    pc, pi = FakePlayer(paused=True), FakePlayer(paused=True)
    make_executor(pc, room_players=lambda: [pc, pi]).execute(Action("play_music", {}))
    assert pc.calls == ["play"] and pi.calls == []

    # Else the room's, if its own has nothing paused.
    pc, pi = FakePlayer(), FakePlayer(paused=True)
    make_executor(pc, room_players=lambda: [pc, pi]).execute(Action("play_music", {}))
    assert pc.calls == [] and pi.calls == ["play"]

    # Else its own, as before rooms.
    pc, pi = FakePlayer(), FakePlayer(playing=True)
    make_executor(pc, room_players=lambda: [pc, pi]).execute(Action("play_music", {}))
    assert pc.calls == ["play"] and pi.calls == []


# Volume and mute on the device named, else the one spoken to (0034)

class FakeWindows:
    """The PC's satellite, with Windows' own volume, which answers each volume request as Windows' keys would."""

    name = "pc"
    system_volume = True

    def __init__(self, level=30):
        self.level, self.muted = level, False
        self.asked = []

    def ask_computer(self, action, **fields):
        self.asked.append((action, fields))
        if "mute" in fields:
            self.muted = fields["mute"]
        elif "level" in fields:
            self.level, self.muted = fields["level"], False
        else:
            self.level += fields["step"] if fields["direction"] == "up" else -fields["step"]
            self.muted = False
        return {"level": self.level, "muted": self.muted}


@pytest.fixture
def pc_and_pi(tmp_path):
    """
    The PC and the Pi, in one room, each a session with its own executor, volumes and music,
    which find each other by name as the brain does - and the volume beeps played on each
    other than by the conversation spoken in, by its level.
    """
    windows = FakeWindows()
    sessions, spoken = {}, {"pc": "the PC", "bedroom": "the Pi"}
    named = {"the pc": "pc", "my pc": "pc", "the pi": "bedroom"}
    pc_music, pi_music = FakePlayer(playing=True, level=50), FakePlayer(level=60)

    def session_for(name):
        return sessions.get(name)

    brain = SimpleNamespace(find_satellite=lambda said: (named[said], None) if said in named
                            else (None, f"There's no satellite called {said}."),
                            session_for=session_for, spoken_name=spoken.get,
                            not_connected=lambda name: "Your PC's off.", computers=SimpleNamespace(known=lambda: []))

    def make(name, satellite, music):
        def device(said):
            if not said:
                return sessions[name], None
            if said == "the mac":
                return None, "Your Mac's off."
            found, problem = brain.find_satellite(said)
            return (sessions[found], None) if found else (None, problem)

        volume = VolumeControl(path=str(tmp_path / f"{name}.json"))
        actions = ActionExecutor(music=music, volume=volume, satellite=name, device=device,
                                 room_players=lambda: [pc_music, pi_music],
                                 computer=ComputerControl(brain, name))
        boops = []
        sessions[name] = SimpleNamespace(satellite=satellite, actions=actions, volume=volume, boops=boops,
                                         boop=boops.append)
        return sessions[name]

    pc = make("pc", windows, pc_music)
    pi = make("bedroom", SimpleNamespace(name="bedroom"), pi_music)
    return SimpleNamespace(pc=pc, pi=pi, windows=windows, pc_music=pc_music, pi_music=pi_music)


def test_volume_up_at_pc_goes_to_windows(pc_and_pi):
    result = pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "up"}))
    assert pc_and_pi.windows.asked == [("volume", {"direction": "up", "step": 10})]
    assert (result.ok, result.say) == (True, "Volume 40 percent.")
    # Beeped at the PC, at Nova's own level there, which Windows' now scales.
    assert (result.confirm, result.sound, result.sound_category) == ("sound", "volume_beep.wav", "general")
    # Nova's own volume and the music playing at the PC are left alone.
    assert pc_and_pi.pc.volume.percent("general") == 70 and pc_and_pi.pc_music.level == 50
    # A level, and a step he names.
    pc_and_pi.pc.actions.execute(Action("set_volume", {"level": 25}))
    pc_and_pi.pc.actions.execute(Action("set_volume", {"category": "music", "direction": "down", "amount": 5}))
    assert pc_and_pi.windows.asked[1:] == [("volume", {"level": 25}),
                                           ("volume", {"direction": "down", "step": 5})]


def test_a_step_at_pc_is_one_windows_can_take(pc_and_pi):
    for amount in (500, 0.2):
        pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "up", "amount": amount}, from_model=True))
    assert pc_and_pi.windows.asked == [("volume", {"direction": "up", "step": 100}),
                                       ("volume", {"direction": "up", "step": 1})]


def test_volume_on_pc_from_pi(pc_and_pi):
    result = pc_and_pi.pi.actions.execute(Action("set_volume", {"direction": "down", "where": "the pc"}))
    assert pc_and_pi.windows.asked == [("volume", {"direction": "down", "step": 10})]
    # Said at the Pi, where he is, and beeped at the PC, whose volume it is.
    assert (result.ok, result.say, result.confirm) == (True, "Volume 20 percent.", "speak")
    assert pc_and_pi.pc.boops == [0.7] and pc_and_pi.pi.boops == []
    assert pc_and_pi.pi.volume.percent("general") == 70 and pc_and_pi.pi_music.level == 60
    # The Pi named at the Pi is the Pi.
    pc_and_pi.pi.actions.execute(Action("set_volume", {"level": 20, "where": "the pi"}))
    assert pc_and_pi.pi.volume.percent("general") == 20 and len(pc_and_pi.windows.asked) == 1
    # One which is off, or none at all, says so.
    off = pc_and_pi.pi.actions.execute(Action("set_volume", {"direction": "up", "where": "the mac"}))
    assert (off.ok, off.say) == (False, "Your Mac's off.")


def test_volume_on_pi_from_pc_beeps_on_pi(pc_and_pi):
    result = pc_and_pi.pc.actions.execute(Action("set_volume", {"level": 30, "where": "the pi"}))
    # Nothing plays on the Pi, so it's Nova's voice there - and Windows is left alone.
    assert pc_and_pi.pi.volume.percent("general") == 30 and pc_and_pi.windows.asked == []
    assert result.ok and result.confirm == "speak" and result.say
    assert pc_and_pi.pi.boops == [0.3] and pc_and_pi.pc.boops == []
    # With music on the Pi, its music - and the beep at its voice's level, as one at the Pi would be.
    pc_and_pi.pi_music.playing = True
    pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "up", "where": "the pi"}))
    assert pc_and_pi.pi_music.level == 70 and pc_and_pi.pc_music.level == 50
    assert pc_and_pi.pi.boops == [0.3, 0.3]


def test_volume_named_elsewhere_is_its_music_only_while_it_plays_there(pc_and_pi):
    # Playing at the Pi, which wasn't paused for him as he spoke at the PC: its music.
    pi_music = pc_and_pi.pi_music
    pi_music.is_playing, pi_music.was_playing = (lambda: True), (lambda: False)
    pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "down", "where": "the pi"}))
    assert pi_music.level == 50 and pc_and_pi.pi.volume.percent("general") == 70
    # Stopped there, though it was playing when last spoken to there: its voice.
    pi_music.is_playing, pi_music.was_playing = (lambda: False), (lambda: True)
    pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "down", "where": "the pi"}))
    assert pi_music.level == 50 and pc_and_pi.pi.volume.percent("general") == 60
    # A player which can't say whether it plays, but says what's playing.
    del pi_music.is_playing
    pi_music.playing = True
    pc_and_pi.pc.actions.execute(Action("set_volume", {"direction": "up", "where": "the pi"}))
    assert pi_music.level == 60


def test_alarm_volume_at_pc_stays_nova(pc_and_pi):
    result = pc_and_pi.pc.actions.execute(Action("set_volume", {"category": "alarm", "direction": "down"}))
    assert pc_and_pi.pc.volume.percent("alarm") == 90 and pc_and_pi.windows.asked == []
    assert (result.sound, result.sound_category) == ("volume_beep.wav", "alarm")
    # Named from the Pi too.
    pc_and_pi.pi.actions.execute(Action("set_volume", {"category": "alarm", "level": 50, "where": "the pc"}))
    assert pc_and_pi.pc.volume.percent("alarm") == 50 and pc_and_pi.windows.asked == []
    assert pc_and_pi.pc.boops == [0.5]


def test_claude_set_volume_at_pc_goes_to_windows(pc_and_pi):
    result = pc_and_pi.pc.actions.execute(Action("set_volume", {"category": "music", "level": 45}, from_model=True))
    assert result.ok and pc_and_pi.windows.asked == [("volume", {"level": 45})]
    pc_and_pi.pi.actions.execute(Action("set_volume", {"mute": True, "where": "my pc"}, from_model=True))
    assert pc_and_pi.windows.asked[-1] == ("volume", {"mute": True})
    # Claude is told of both, and what they're for.
    schema = next(tool for tool in pc_and_pi.pc.actions.tool_schemas() if tool["name"] == "set_volume")
    properties = schema["input_schema"]["properties"]
    assert properties["where"]["type"] == "string" and "PC" in properties["where"]["description"]
    assert properties["mute"]["type"] == "boolean" and "unmute" in properties["mute"]["description"]
    # Its mute is the sound's only when he says sound or audio; the microphone is another tool's.
    assert "sound or audio" in properties["mute"]["description"]
    assert "mute_microphone" in properties["mute"]["description"]


def test_mute_microphone_is_the_sessions_and_offered_to_claude():
    asked = []

    def microphone(muted, where):
        asked.append((muted, where))
        return {"success": True, "message": "Microphone off.", "sound": "mic_off.wav"}

    executor = ActionExecutor(microphone=microphone)
    result = executor.execute(Action("mute_microphone", {"muted": True}))
    assert (result.ok, result.say, result.confirm, result.sound) == (True, "Microphone off.", "sound", "mic_off.wav")
    # Confirmed by the sound alone, even when Claude asked and has its own "Okay." to say.
    assert confirm([result], fallback="Okay.") == ([("mic_off.wav", "general")], [])
    executor.execute(Action("mute_microphone", {"muted": False, "where": "the Pi"}, from_model=True))
    assert asked == [(True, None), (False, "the Pi")]
    schema = next(tool for tool in executor.tool_schemas() if tool["name"] == "mute_microphone")
    assert schema["input_schema"]["required"] == ["muted"]
    assert "set_volume" in schema["description"] and "where" in schema["input_schema"]["properties"]
    # Not offered, and refused, without a session to mute.
    bare = ActionExecutor()
    assert all(tool["name"] != "mute_microphone" for tool in bare.tool_schemas())
    assert not bare.execute(Action("mute_microphone", {"muted": True})).ok


def test_the_pcs_own_microphone_is_offered_only_at_a_pc_which_can_reach_it(pc_and_pi):
    offered = {tool["name"]: tool for tool in pc_and_pi.pc.actions.tool_schemas()}
    assert "mute_computer_mic" in offered and "mute_microphone" in offered["mute_computer_mic"]["description"]
    # The question is the local rules' alone.
    assert "mute_which" not in offered
    assert not pc_and_pi.pc.actions.execute(Action("mute_which", {"muted": True}, from_model=True)).ok
    # Not at the Pi, which has no Windows to mute.
    assert "mute_computer_mic" not in {tool["name"] for tool in pc_and_pi.pi.actions.tool_schemas()}
    refused = pc_and_pi.pi.actions.execute(Action("mute_computer_mic", {"muted": True}))
    assert not refused.ok and pc_and_pi.windows.asked == []


def test_mute_which_asks_the_mic_or_the_audio(pc_and_pi):
    result = pc_and_pi.pc.actions.execute(Action("mute_which", {"muted": False}))
    assert (result.ok, result.say) == (True, "The mic or the audio?")
    assert result.ask_first.chosen("the mic") == Action("mute_computer_mic", {"muted": False})
    assert result.ask_first.chosen("microphone") == Action("mute_computer_mic", {"muted": False})
    assert result.ask_first.chosen("unmute the audio") == Action("set_volume", {"mute": False})
    assert result.ask_first.chosen("sound") == Action("set_volume", {"mute": False})
    assert result.ask_first.chosen("the mic and the lights") is None
    # Both is an answer to an unmute only (his ruling, 20 Nov): there's no "mute everything".
    for said in ("both", "everything", "unmute all", "all of it"):
        assert result.ask_first.chosen(said) == Action("unmute_everything"), said
    assert pc_and_pi.pc.actions.execute(Action("mute_which", {"muted": True})).ask_first.chosen("both") is None
    # The other verb is no answer: "mute the audio" after "Unmute." is a fresh request.
    assert result.ask_first.chosen("mute the audio") is None and result.ask_first.chosen("mute the mic") is None
    muting = pc_and_pi.pc.actions.execute(Action("mute_which", {"muted": True})).ask_first
    assert muting.chosen("mute the audio") == Action("set_volume", {"mute": True})
    assert muting.chosen("unmute the mic") is None


def test_mute_at_pc(pc_and_pi):
    muted = pc_and_pi.pc.actions.execute(Action("set_volume", {"mute": True}))
    assert pc_and_pi.windows.asked == [("volume", {"mute": True})]
    # The satellite plays mic_off.wav itself, before Windows mutes: the brain adds nothing, no words.
    assert (muted.ok, muted.confirm, muted.sound) == (True, "music", None)
    assert confirm([muted]) == ([], [])
    unmuted = pc_and_pi.pc.actions.execute(Action("set_volume", {"mute": False}))
    assert pc_and_pi.windows.asked[-1] == ("volume", {"mute": False})
    assert (unmuted.ok, unmuted.confirm, unmuted.sound) == (True, "sound", "mic_on.wav")
    assert confirm([unmuted]) == ([("mic_on.wav", "general")], [])
    # Nova's own volumes and the PC's music are untouched.
    assert pc_and_pi.pc.volume.percent("general") == 70 and pc_and_pi.pc_music.level == 50


def test_mute_at_pi_mutes_music_only_and_unmute_restores(pc_and_pi):
    pi = pc_and_pi.pi
    pc_and_pi.pi_music.playing = True
    muted = pi.actions.execute(Action("set_volume", {"mute": True}))
    assert (muted.ok, muted.confirm, muted.sound) == (True, "sound", "mic_off.wav")
    assert pc_and_pi.pi_music.level == 0
    # Nova's voice, alarms and timers still sound, and the PC's music plays on.
    assert (pi.volume.percent("general"), pi.volume.percent("alarm")) == (70, 100)
    assert pc_and_pi.pc_music.level == 50 and pc_and_pi.windows.asked == []
    unmuted = pi.actions.execute(Action("set_volume", {"mute": False}))
    assert (unmuted.ok, unmuted.sound) == (True, "mic_on.wav") and pc_and_pi.pi_music.level == 60
    # Muted from the PC, confirmed by the sound at the PC, as the microphone's is; and an unmute with nothing muted changes nothing.
    pc_and_pi.pc.actions.execute(Action("set_volume", {"mute": True, "where": "the pi"}))
    assert pc_and_pi.pi_music.level == 0 and pi.boops == []
    named = pc_and_pi.pc.actions.execute(Action("set_volume", {"mute": False, "where": "the pi"}))
    assert pc_and_pi.pi_music.level == 60 and named.sound == "mic_on.wav"
    again = pi.actions.execute(Action("set_volume", {"mute": False}))
    assert again.ok and pc_and_pi.pi_music.level == 60


# Moving the music, and playing on another satellite

class QueryPlayer:
    """
    A satellite's Navidrome player, which notes what it's asked to play, and was playing before the
    assistant was spoken to if it's told so - as music held for a reply is, see MusicHold.
    """

    name = "Navidrome"
    needs_internet = False

    def __init__(self, was_playing=False, events=None):
        self.calls = []
        self.was_playing_before = was_playing
        self.resume_cancelled = False
        self.events = events if events is not None else []

    def was_playing(self):
        return self.was_playing_before

    def hand_over(self):
        # What only Navidrome's player has: one which stream of its own to pause, rather than the account's.
        return None

    def pause(self):
        self.calls.append("pause")
        return {"success": True, "message": "Paused", "chime": True}

    def cancel_resume(self):
        self.resume_cancelled = True

    def play(self, query=None, search_type=None):
        self.calls.append(("play", query, search_type))
        self.events.append("played")
        return {"success": True, "message": f"Playing {query}"}

    def play_playlist(self, name):
        self.calls.append(("play_playlist", name))
        self.events.append("played")
        return {"success": True, "message": f"Playing {name}"}


def _pair(make_executor, in_one_room):
    """
    The Pi's executor, which hears "the PC" as another satellite and "the Pi" as itself, and
    the PC's - with the moves the Pi was asked for, what happened to the PC's player, and each
    one's player. The Pi was playing before it was spoken to, and so was the PC. With
    in_one_room, the two are in the user's room along with a third which wasn't playing.
    """
    events, moves = [], []
    pi_player, pc_player, third_player = (QueryPlayer(was_playing=True, events=events),
                                          QueryPlayer(was_playing=True, events=events), QueryPlayer())
    pc = make_executor(pc_player, satellite="pc")
    pc.spoken_name = lambda name: "the PC"

    def elsewhere(said):
        if said == "the PC":
            return pc, None
        if said == "the Pi":
            return pi, None
        if said == "the Mac":
            return None, "The Mac isn't connected."
        return None, f"There's no satellite called {said}."

    def mover(said):
        moves.append(said)
        return {"success": True, "message": "Moved to the PC."}

    room = [pi_player, pc_player, third_player] if in_one_room else [pi_player]
    pi = make_executor(pi_player, satellite="bedroom", mover=mover, elsewhere=elsewhere, room_players=lambda: room)
    return SimpleNamespace(pi=pi, pc=pc, pi_player=pi_player, pc_player=pc_player, third_player=third_player,
                           moves=moves, events=events)


@pytest.fixture
def pair(make_executor):
    """The Pi and the PC, in rooms of their own."""
    return _pair(make_executor, in_one_room=False)


@pytest.fixture
def roomed(make_executor):
    """The Pi and the PC, in the same room."""
    return _pair(make_executor, in_one_room=True)


def test_move_music_tool_calls_the_mover(pair):
    result = pair.pi.execute(Action("move_music", {"to": "the PC"}, from_model=True))
    assert (result.ok, result.say) == (True, "Moved to the PC.")
    assert pair.moves == ["the PC"]

    # What the brain says of a move which couldn't be done is said as it is.
    pair.pi.mover = lambda said: {"success": False, "message": "It's already on the PC."}
    result = pair.pi.execute(Action("move_music", {"to": "the PC"}))
    assert (result.ok, result.say) == (False, "It's already on the PC.")

    # A mover which fails is said, not raised.
    def broken(said):
        raise OSError("the brain's lost it")

    pair.pi.mover = broken
    result = pair.pi.execute(Action("move_music", {"to": "the PC"}))
    assert result.ok is False and result.say


def test_move_music_is_offered_with_the_satellite_to_name(pair, make_executor):
    schemas = {schema["name"]: schema for schema in pair.pi.tool_schemas()}
    assert schemas["move_music"]["input_schema"]["required"] == ["to"]
    assert "play it on my Pi" in schemas["move_music"]["description"]
    # Only for naming somewhere other than here: never required.
    for name in ("play_music", "play_playlist"):
        schema = schemas[name]["input_schema"]
        assert "on" in schema["properties"] and "on" not in schema["required"]
        assert "other than here" in schema["properties"]["on"]["description"]
        # With no query it's the music playing now which goes there, not the other way about.
        assert "moves the music that's playing now to there" in schema["properties"]["on"]["description"]
    # Without a way to move it, and without music, it isn't offered.
    assert "move_music" not in {schema["name"] for schema in ActionExecutor(clock=object()).tool_schemas()}


def test_play_music_on_another_satellite_plays_there(pair):
    result = pair.pi.execute(Action("play_music", {"query": "Drake", "search_type": "artist", "on": "the PC"}))
    # Said where he is, as he can't hear the PC's room.
    assert (result.ok, result.say, result.confirm) == (True, "Playing Drake on the PC.", "speak")
    assert pair.pc_player.calls == [("play", "Drake", "artist")]
    # This one's player isn't touched - it's in another room - and nothing is moved.
    assert pair.pi_player.calls == [] and not pair.pi_player.resume_cancelled and pair.moves == []


def test_play_music_on_with_no_query_moves(pair):
    result = pair.pi.execute(Action("play_music", {"on": "the PC"}))
    assert (result.ok, result.say) == (True, "Moved to the PC.")
    assert pair.moves == ["the PC"] and pair.pi_player.calls == [] and pair.pc_player.calls == []


def _with_nothing_to_move(pair):
    """What the brain says when there's no music anywhere to move: the sentence, and the flag for it."""
    pair.pi.mover = lambda words: pair.moves.append(words) or {
        "success": False, "message": "There's no music to move.", "nothing_to_move": True}
    pair.pc_player.play = lambda query=None, search_type=None: pair.pc_player.calls.append(("play", query)) or {
        "success": True, "message": "Playing your liked songs"}


def test_play_music_on_with_no_query_and_nothing_to_move_plays_there(pair):
    # "Play something on the PC" with nothing on anywhere is the PC's own bare "play", said where he is.
    _with_nothing_to_move(pair)
    result = pair.pi.execute(Action("play_music", {"on": "the PC"}))
    assert (result.ok, result.say, result.confirm) == (True, "Playing your liked songs on the PC.", "speak")
    assert pair.pc_player.calls == [("play", None)]
    # The move was asked first, and this one's player is left alone.
    assert pair.moves == ["the PC"] and pair.pi_player.calls == []


def test_play_music_on_with_no_query_and_music_to_move_still_moves(pair):
    # Moving wins: the brain found music, so says "Moved to the PC." and nothing is started over it.
    _with_nothing_to_move(pair)
    pair.pi.mover = lambda words: pair.moves.append(words) or {"success": True, "message": "Moved to the PC."}
    result = pair.pi.execute(Action("play_music", {"on": "the PC"}))
    assert (result.ok, result.say) == (True, "Moved to the PC.")
    assert pair.pc_player.calls == [] and pair.pi_player.calls == []


def test_play_music_on_with_nothing_to_move_to_one_which_cant_be_played_on_says_why(pair):
    # There's nothing to start anything on, so it's why it can't be started which is said, not the move's.
    _with_nothing_to_move(pair)
    for on, said in (("the Mac", "The Mac isn't connected."), ("the phone", "There's no satellite called the phone.")):
        result = pair.pi.execute(Action("play_music", {"on": on}))
        assert (result.ok, result.say) == (False, said)
    assert pair.pc_player.calls == [] and pair.pi_player.calls == []


def test_play_music_on_this_satellite_plays_here(pair):
    pair.pi.execute(Action("play_music", {"query": "Drake", "on": "the Pi"}))
    assert pair.pi_player.calls == [("play", "Drake", None)] and pair.pc_player.calls == []
    # With no query it carries on what's paused here, as without "on", and isn't a move.
    pair.pi.execute(Action("play_music", {"on": "the Pi"}))
    assert pair.pi_player.calls[-1] == ("play", None, None) and pair.moves == []


def test_play_music_on_unknown_says_why(pair):
    for on, said in (("the Mac", "The Mac isn't connected."), ("the phone", "There's no satellite called the phone.")):
        result = pair.pi.execute(Action("play_music", {"query": "Drake", "on": on}))
        assert (result.ok, result.say) == (False, said)
    assert pair.pi_player.calls == [] and pair.pc_player.calls == [] and pair.moves == []


def test_play_music_on_with_no_query_is_a_move_even_to_one_which_cant_be_found(pair):
    # What the brain says of the move is said, as the brain has the copy for it.
    said = "The Mac isn't connected, so the music's staying on the Pi."
    pair.pi.mover = lambda words: pair.moves.append(words) or {"success": False, "message": said}
    for on in ("the Mac", "the phone"):
        result = pair.pi.execute(Action("play_music", {"on": on}))
        assert (result.ok, result.say) == (False, said)
    assert pair.moves == ["the Mac", "the phone"]
    assert pair.pi_player.calls == [] and pair.pc_player.calls == []


def test_play_playlist_on_another_satellite(pair):
    result = pair.pi.execute(Action("play_playlist", {"name": "Gym", "on": "the PC"}))
    assert (result.ok, result.say) == (True, "Playing Gym on the PC.")
    assert pair.pc_player.calls == [("play_playlist", "Gym")] and pair.pi_player.calls == []

    pair.pi.execute(Action("play_playlist", {"name": "Gym", "on": "the Pi"}))
    assert pair.pi_player.calls == [("play_playlist", "Gym")]
    result = pair.pi.execute(Action("play_playlist", {"name": "Gym", "on": "the Mac"}))
    assert (result.ok, result.say) == (False, "The Mac isn't connected.")


def test_a_failure_on_another_satellite_is_said_and_not_raised(pair):
    def lost(said):
        raise OSError("the brain's lost it")

    # The brain can't look it up, or the other satellite's player gives way.
    pair.pi.elsewhere = lost
    for tool, params in (("play_music", {"query": "Drake"}), ("play_playlist", {"name": "Gym"})):
        result = pair.pi.execute(Action(tool, {**params, "on": "the PC"}))
        assert result.ok is False and result.say
    assert pair.pi_player.calls == []
    # With nothing to play it's the brain's move, which looks it up for itself.
    pair.pi.execute(Action("play_music", {"on": "the PC"}))
    assert pair.moves == ["the PC"]

    pair.pi.elsewhere = lambda said: (pair.pc, None)
    pair.pc_player.play = lost
    result = pair.pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    assert result.ok is False and result.say

    # Or what it waits for first.
    del pair.pc_player.play
    pair.pc.settled = lambda: lost("the PC")
    result = pair.pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    assert result.ok is False and result.say and pair.pc_player.calls == []


def test_another_satellite_with_no_music_says_so(pair, make_executor):
    pair.pi.elsewhere = lambda said: (make_executor(None, satellite="pc"), None)
    result = pair.pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    assert (result.ok, result.say) == (False, "The music isn't available right now.")


def test_playing_elsewhere_waits_for_what_is_on_its_way_to_that_satellite_first(pair):
    # As a move does: a pause which is still on its way mustn't land after the music is asked for.
    pair.pc.settled = lambda: pair.events.append("settled")
    pair.pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    pair.pi.execute(Action("play_playlist", {"name": "Gym", "on": "the PC"}))
    assert pair.events == ["settled", "played", "settled", "played"]


def test_what_isnt_a_start_isnt_said_to_be_playing_elsewhere(pair, make_executor):
    # Navidrome hasn't got it: what's said is the offer, or that it's on its way, where he is.
    player = QueryPlayer()
    player.play = lambda query=None, search_type=None: {"success": False, "missing": True,
                                                        "message": "I couldn't find Drake on Navidrome."}
    offers = [{"success": True, "message": "I'm already getting Drake."},
              {"success": True, "message": "I haven't got Drake. Shall I download it?",
               "ask_first": Action("request_download", {"query": "Drake"})}]
    other = make_executor(player, satellite="pc", downloads=SimpleNamespace(offer=lambda query, kind: offers.pop(0)))
    other.spoken_name = lambda name: "the PC"
    pair.pi.elsewhere = lambda said: (other, None)

    result = pair.pi.execute(Action("play_music", {"query": "Drake", "search_type": "track", "on": "the PC"}))
    assert (result.ok, result.say) == (True, "I'm already getting Drake.")
    result = pair.pi.execute(Action("play_music", {"query": "Drake", "search_type": "track", "on": "the PC"}))
    assert (result.ok, result.say) == (True, "I haven't got Drake. Shall I download it?")
    assert result.ask_first == Action("request_download", {"query": "Drake"})


@pytest.mark.parametrize("tool, params", [("play_music", {"query": "Drake"}), ("play_playlist", {"name": "Gym"})])
def test_playing_on_one_in_the_room_pauses_the_rooms_other_music_and_keeps_it_paused(roomed, tool, params):
    result = roomed.pi.execute(Action(tool, {**params, "on": "the PC"}))
    assert result.ok and result.say.endswith(" on the PC.")
    # As a spoken "pause" does: paused, and not brought back once the reply is over.
    assert roomed.pi_player.calls == ["pause"] and roomed.pi_player.resume_cancelled
    # The PC's is what's playing now, though it was before too, and a third which wasn't playing is left alone.
    assert "pause" not in roomed.pc_player.calls and not roomed.pc_player.resume_cancelled
    assert roomed.third_player.calls == [] and not roomed.third_player.resume_cancelled


class OneStream:
    """Spotify's one stream for the whole account, which plays on one device at a time."""

    def __init__(self):
        self.device = "bedroom"
        self.paused = False


class OneStreamPlayer:
    """Spotify's controller on one satellite: its pause stops whichever device the account is playing on."""

    name = "Spotify"
    needs_internet = False

    def __init__(self, stream, device):
        self.stream, self.device = stream, device
        self.resume_cancelled = False

    def was_playing(self):
        return True

    def pause(self):
        self.stream.paused = True
        return {"success": True, "message": "Paused", "chime": True}

    def cancel_resume(self):
        self.resume_cancelled = True

    def play(self, query=None, search_type=None):
        self.stream.device, self.stream.paused = self.device, False
        return {"success": True, "message": f"Playing {query}"}


def test_playing_on_one_in_the_room_never_pauses_spotifys_one_stream(make_executor):
    # Starting it on the PC took the account's stream from the Pi already: pausing "the Pi's" music would
    # pause the PC's, and nothing would bring it back.
    stream = OneStream()
    pi_player, pc_player = OneStreamPlayer(stream, "bedroom"), OneStreamPlayer(stream, "pc")
    pc = make_executor(pc_player, satellite="pc")
    pc.spoken_name = lambda name: "the PC"
    pi = make_executor(pi_player, satellite="bedroom", elsewhere=lambda said: (pc, None),
                       room_players=lambda: [pi_player, pc_player])

    result = pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))

    assert (result.ok, result.say) == (True, "Playing Drake on the PC.")
    assert stream.device == "pc" and not stream.paused
    assert not pi_player.resume_cancelled and not pc_player.resume_cancelled


def test_a_play_elsewhere_which_didnt_start_leaves_the_rooms_music_playing(roomed):
    roomed.pc_player.play = lambda query=None, search_type=None: {"success": False, "message": "Navidrome is down."}
    result = roomed.pi.execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    assert (result.ok, result.say) == (False, "Navidrome is down.")
    assert roomed.pi_player.calls == [] and not roomed.pi_player.resume_cancelled


def test_on_means_nothing_to_an_executor_with_no_other_satellites(make_executor):
    player = QueryPlayer()
    make_executor(player).execute(Action("play_music", {"query": "Drake", "on": "the PC"}))
    assert player.calls == [("play", "Drake", None)]


@pytest.mark.parametrize("params, repeat", [
    ({"hour": 7, "minute": 0}, None),
    ({"hour": 7, "minute": 0, "repeat": "weekdays"}, "weekdays"),
    ({"hour": 7, "minute": 0, "repeat": "custom", "days": ["monday", "wednesday"]}, ["monday", "wednesday"]),
    # "custom" with no days is passed through as-is, so the clock asks which days.
    ({"hour": 7, "minute": 0, "repeat": "custom"}, "custom"),
])
def test_an_alarms_repeat_is_passed_to_the_clock(params, repeat):
    class FakeClock:
        def set_alarm(self, hour, minute, repeat, satellite=None):
            self.set = (hour, minute, repeat)
            return {"success": True, "message": "Alarm set."}

    clock = FakeClock()
    ActionExecutor(clock=clock).execute(Action("set_alarm", params))
    assert clock.set == (7, 0, repeat)


def test_timers_and_alarms_ring_on_the_satellite_they_were_set_from():
    class FakeClock:
        def __init__(self):
            self.set = []

        def set_timer(self, duration_seconds, label, is_reminder, at=None, satellite=None):
            self.set.append(("timer", satellite))
            return {"success": True, "message": "Timer set."}

        def set_alarm(self, hour, minute, repeat, satellite=None):
            self.set.append(("alarm", satellite))
            return {"success": True, "message": "Alarm set."}

    clock = FakeClock()
    actions = ActionExecutor(clock=clock, satellite="bedroom")
    actions.execute(Action("set_timer", {"duration_seconds": 600}))
    actions.execute(Action("set_alarm", {"hour": 7, "minute": 0}))
    assert clock.set == [("timer", "bedroom"), ("alarm", "bedroom")]


class RecordingClock:
    """A clock which says what it was asked to set and where."""

    def __init__(self):
        self.set = []

    def set_timer(self, duration_seconds, label, is_reminder, at=None, satellite=None):
        self.set.append(("timer", satellite))
        return {"success": True, "message": "Timer set for 10 minutes."}

    def set_alarm(self, hour, minute, repeat, satellite=None):
        self.set.append(("alarm", satellite))
        return {"success": True, "message": "7:00 AM."}


def test_a_timer_set_at_the_pc_rings_on_the_pi():
    clock = RecordingClock()
    actions = ActionExecutor(clock=clock, satellite="pc", ring_on=lambda name: "bedroom",
                             room_of=lambda name: [name, "bedroom"])
    result = actions.execute(Action("set_timer", {"duration_seconds": 600}))
    assert clock.set == [("timer", "bedroom")]
    # In the room the user is in, so there's nothing to tell them.
    assert result.say == "Timer set for 10 minutes."


def test_a_timer_handed_to_another_room_says_so():
    clock = RecordingClock()
    actions = ActionExecutor(clock=clock, satellite="pc", ring_on=lambda name: "kitchen",
                             room_of=lambda name: [name])
    result = actions.execute(Action("set_timer", {"duration_seconds": 600}))
    assert clock.set == [("timer", "kitchen")]
    assert result.say == "Timer set for 10 minutes. It'll ring on the kitchen."


def test_an_alarm_set_at_the_pc_rings_on_the_pi():
    clock = RecordingClock()
    actions = ActionExecutor(clock=clock, satellite="pc", ring_on=lambda name: "bedroom",
                             room_of=lambda name: [name, "bedroom"])
    result = actions.execute(Action("set_alarm", {"hour": 7, "minute": 0}))
    assert clock.set == [("alarm", "bedroom")]
    assert result.say == "7:00 AM."

    other = ActionExecutor(clock=clock, satellite="pc", ring_on=lambda name: "kitchen",
                           room_of=lambda name: [name])
    assert other.execute(Action("set_alarm", {"hour": 7, "minute": 0})).say == "7:00 AM. It'll ring on the kitchen."


def test_nothing_is_added_when_a_timer_wasnt_set_or_rings_where_asked():
    class AskingClock(RecordingClock):
        def set_timer(self, duration_seconds, label, is_reminder, at=None, satellite=None):
            return {"success": False, "message": "What would you like to name this reminder?"}

    actions = ActionExecutor(clock=AskingClock(), satellite="pc", ring_on=lambda name: "kitchen",
                             room_of=lambda name: [name])
    assert actions.execute(Action("set_timer", {"duration_seconds": 600, "is_reminder": True})
                           ).say == "What would you like to name this reminder?"
    # Ringing where it was asked, even if its room isn't known.
    same = ActionExecutor(clock=RecordingClock(), satellite="kitchen", ring_on=lambda name: name,
                          room_of=lambda name: [name])
    assert same.execute(Action("set_timer", {"duration_seconds": 600})).say == "Timer set for 10 minutes."


def test_only_what_rings_on_the_satellite_asked_is_dismissed():
    # "Okay, thanks" in the kitchen dismissed the bedroom's timer too, which nobody
    # there could hear.
    class FakeClock:
        def dismiss(self, satellite=None):
            self.dismissed = satellite
            return {"success": True, "message": "Okay.", "chime": True}

    clock = FakeClock()
    ActionExecutor(clock=clock, satellite="kitchen").execute(Action("dismiss_timer", {}))
    assert clock.dismissed == "kitchen"

    # Its room, though, is all heard - a timer set at the PC rings on the Pi beside it.
    ActionExecutor(clock=clock, satellite="pc",
                   room_of=lambda name: [name, "bedroom"]).execute(Action("dismiss_timer", {}))
    assert clock.dismissed == ["pc", "bedroom"]


# Confirming what was done

def test_a_controllers_flags_are_read_into_one_way_of_confirming():
    assert ActionResult.from_reply({"success": True, "silent": True, "message": "Next"}).confirm == "music"
    assert ActionResult.from_reply({"success": True, "chime": True, "message": "Paused"}).confirm == "chime"
    assert ActionResult.from_reply({"success": True, "message": "Timer set."}).confirm == "speak"
    # A failure is always said, whatever it was going to confirm with.
    assert ActionResult.from_reply({"success": False, "chime": True, "message": "No"}).confirm == "speak"


def test_a_request_assist_didnt_understand_is_told_apart_from_one_which_failed():
    assert ActionResult.from_reply({"success": False, "message": "x", "not_understood": True}).understood is False
    assert ActionResult.from_reply({"success": False, "message": "x"}).understood is True
    assert ActionResult.from_reply({"success": True}).understood is True


def test_an_action_which_confirms_itself_has_nothing_said_about_it():
    # Even when there's something else to say: a reminder offer after a skipped
    # song used to have the skip's own message read out in front of it.
    skipped = ActionResult(True, "Skipped", confirm="music")
    assert confirm([skipped], fallback="Okay.") == ([], [])


@pytest.mark.parametrize("result, sounds", [
    (ActionResult(True, confirm="sound", sound="volume_beep.wav", sound_category="alarm"),
     [("volume_beep.wav", "alarm")]),
    (ActionResult(True, "Paused", confirm="chime"), [("success_chime.wav", "general")]),
])
def test_an_action_with_a_sound_is_confirmed_by_it(result, sounds):
    assert confirm([result], chime_enabled=True) == (sounds, [])


def test_a_chimed_action_is_said_out_loud_when_the_chime_is_off():
    assert confirm([ActionResult(True, "Paused", confirm="chime")], chime_enabled=False) == ([], ["Paused"])


def test_a_failed_action_says_why_or_falls_back_to_the_models_reply():
    assert confirm([ActionResult(False, "The music isn't available right now.")],
                   fallback="Okay.") == ([], ["The music isn't available right now."])
    assert confirm([ActionResult(False)], fallback="Okay.") == ([], ["Okay."])


def test_several_actions_are_each_confirmed_their_own_way():
    paused = ActionResult(True, "Paused", confirm="chime")
    timer = ActionResult(True, "Timer set for 10 minutes.")
    assert confirm([paused, timer]) == ([("success_chime.wav", "general")], ["Timer set for 10 minutes."])


def test_weather_is_passed_to_the_forecast():
    class FakeWeather:
        def forecast(self, day):
            return {"success": True, "message": f"Forecast for {day}"}

    executor = ActionExecutor(weather=FakeWeather())
    assert executor.execute(Action("get_weather", {"day": "tomorrow"})).say == "Forecast for tomorrow"


def test_offline_spotify_says_so_straight_away(make_executor, monkeypatch):
    from voice_assistant import actions as actions_module
    monkeypatch.setattr(actions_module.network, "online", lambda: False)
    spotify = FakeSpotify(playing=True)
    executor = make_executor(spotify=spotify)
    offline = "Spotify needs the internet, and I can't reach it right now."
    assert executor.execute(Action("control_playback", {"action": "pause"})).say == offline
    assert executor.execute(Action("set_volume", {"category": "music", "direction": "down"})).say == offline
    assert spotify.calls == []


def test_the_calendar_needs_a_day_it_can_read():
    class FakeCalendar:
        def agenda(self, first, days):
            self.asked = (first.isoformat(), days)
            return {"success": True, "message": "You've got nothing on."}

    calendar = FakeCalendar()
    executor = ActionExecutor(calendar=calendar)
    assert executor.execute(Action("get_calendar", {"start": "Thursday"})).ok is False
    executor.execute(Action("get_calendar", {"start": "2026-10-22", "days": 90}))
    assert calendar.asked == ("2026-10-22", 14)


def test_one_thing_is_looked_for_rather_than_reading_whole_days():
    class FakeCalendar:
        def find(self, wanted, first):
            self.asked = (wanted, first.isoformat())
            return {"success": True, "message": "You've got Car MOT on Monday."}

    calendar = FakeCalendar()
    result = ActionExecutor(calendar=calendar).execute(
        Action("get_calendar", {"start": "2026-10-20", "days": 14, "search": "MOT"}))
    assert calendar.asked == ("MOT", "2026-10-20") and result.say == "You've got Car MOT on Monday."


def test_the_calendar_is_only_offered_once_google_is_set_up():
    # It's Google's now, not Home Assistant's, so Home Assistant alone doesn't bring it.
    class FakeHome:
        pass

    executor = ActionExecutor(home=FakeHome())
    offered = [schema["name"] for schema in executor.tool_schemas()]
    assert "get_calendar" not in offered and "get_activity" not in offered
    for action in (Action("get_calendar", {"start": "2026-10-22"}), Action("get_activity", {"start": "2026-10-22"})):
        result = executor.execute(action)
        assert (result.ok, result.say) == (False, "The calendar isn't set up yet."), action.tool

    class FakeCalendar:
        def activity_calendars(self):
            return {}

    offered = {schema["name"]: schema for schema in ActionExecutor(calendar=FakeCalendar()).tool_schemas()}
    assert "get_calendar" in offered and "get_activity" in offered
    # With no activities yet, none are named.
    assert "The user's activities" not in offered["get_activity"]["description"]


def test_claude_is_never_told_a_calendar_named_by_an_address():
    # A calendar shared by another person is named by their email address - made up here -
    # which would otherwise go to Anthropic with every request, switched on as an activity.
    class FakeCalendar:
        def __init__(self, *calendars):
            self.calendars = calendars

        def activity_calendars(self):
            return {word: {"id": f"calendar-{index}", "name": name, "primary": False, "writable": False}
                    for index, (word, name) in enumerate(self.calendars)}

    def told(*calendars):
        schemas = ActionExecutor(calendar=FakeCalendar(*calendars)).tool_schemas()
        return next(schema["description"] for schema in schemas if schema["name"] == "get_activity")

    described = told(("work", "Work"), ("a shared calendar", "someone@example.com"))
    assert described.endswith("The user's activities: Work.") and "example" not in described
    described = told(("a shared calendar", "someone@example.com"))
    assert "The user's activities" not in described and "example" not in described


def test_add_to_calendar_is_only_offered_once_google_is_set_up():
    executor = ActionExecutor()
    assert "add_to_calendar" not in [schema["name"] for schema in executor.tool_schemas()]
    result = executor.execute(Action("add_to_calendar", {"summary": "Dentist", "start": "2026-10-29 15:00"}))
    assert (result.ok, result.say) == (False, "The calendar isn't set up yet.")

    class FakeCalendar:
        def activity_calendars(self):
            return {}

        def add(self, summary, start, end=None, all_day=None, calendar=None, **repeats):
            self.asked = (summary, start, end, all_day, calendar, repeats)
            return {"success": True, "message": "Added Shift to Work, Saturday the 31st at 9 AM."}

    calendar = FakeCalendar()
    executor = ActionExecutor(calendar=calendar)
    offered = {schema["name"]: schema for schema in executor.tool_schemas()}
    assert offered["add_to_calendar"]["description"] == (
        "Add an event to the user's Google calendar, for 'put the dentist on Thursday at 3'. Never call this when "
        "the user says remove, cancel, delete, take off or clear - remove_from_calendar is for those, even when the "
        "rest of the sentence is garbled ('Remove, go to the dentist next Thursday' removes the dentist, it never "
        "adds one). start and end are local 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for all day; leave end out for an "
        "hour. Give calendar only when the user names one, e.g. 'work'. Give repeat only when the user says it "
        "repeats - 'every weekday', 'every Tuesday', 'each month', 'every year' - and never otherwise: start is "
        "then the first one. What was added is said for you, so just reply 'Okay.'")
    assert offered["add_to_calendar"]["input_schema"]["required"] == ["summary", "start"]
    assert set(offered["add_to_calendar"]["input_schema"]["properties"]) == {
        "summary", "start", "end", "all_day", "calendar", "repeat", "repeat_on", "repeat_every", "repeat_until",
        "repeat_times"}
    result = executor.execute(Action("add_to_calendar", {"summary": "Shift", "start": "2026-10-31 09:00",
                                                         "calendar": "work"}, from_model=True))
    assert calendar.asked == ("Shift", "2026-10-31 09:00", None, None, "work", {})
    assert (result.ok, result.say) == (True, "Added Shift to Work, Saturday the 31st at 9 AM.")


def test_adding_is_never_for_a_sentence_that_says_remove():
    # A garbled "Remove, go to the dentist next Thursday" once made Claude add a dentist event.
    class FakeCalendar:
        def activity_calendars(self):
            return {}

    description = {schema["name"]: schema for schema in ActionExecutor(calendar=FakeCalendar()).tool_schemas()}[
        "add_to_calendar"]["description"]
    for words in ("Never call this when the user says remove, cancel, delete, take off or clear",
                  "remove_from_calendar is for those", "even when the rest of the sentence is garbled",
                  "'Remove, go to the dentist next Thursday' removes the dentist, it never adds one"):
        assert words in description, words
    # The ban comes before what the tool is told to do, where it's read first.
    assert description.index("Never call this") < description.index("start and end are")


def test_add_to_calendar_passes_a_repeat_on_and_only_when_it_is_given():
    class FakeCalendar:
        def activity_calendars(self):
            return {}

        def add(self, summary, start, end=None, all_day=None, calendar=None, **repeats):
            self.asked = (summary, start, repeats)
            return {"success": True, "message": "Added."}

    calendar = FakeCalendar()
    executor = ActionExecutor(calendar=calendar)
    properties = {schema["name"]: schema for schema in executor.tool_schemas()}["add_to_calendar"][
        "input_schema"]["properties"]
    # The kinds Nova builds a rule for, the weekdays, and whole numbers - Claude never writes a rule.
    assert properties["repeat"]["enum"] == ["daily", "weekdays", "weekly", "monthly", "yearly"]
    assert properties["repeat_on"]["type"] == "array" and properties["repeat_on"]["items"]["enum"] == [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    assert properties["repeat_every"]["type"] == properties["repeat_times"]["type"] == "integer"
    assert properties["repeat_until"]["type"] == "string" and "YYYY-MM-DD" in properties["repeat_until"]["description"]

    executor.execute(Action("add_to_calendar", {
        "summary": "Gym", "start": "2026-11-23 08:00", "repeat": "weekly", "repeat_on": ["Monday", "Wednesday"],
        "repeat_every": 2, "repeat_until": "2026-12-18", "repeat_times": 10}, from_model=True))
    assert calendar.asked == ("Gym", "2026-11-23 08:00", {
        "repeat": "weekly", "repeat_on": ["Monday", "Wednesday"], "repeat_every": 2, "repeat_until": "2026-12-18",
        "repeat_times": 10})
    executor.execute(Action("add_to_calendar", {"summary": "Gym", "start": "2026-11-23 08:00"}, from_model=True))
    assert calendar.asked == ("Gym", "2026-11-23 08:00", {})


# Removing, moving and renaming calendar events

class RecordingChangeCalendar:
    """
    A calendar which records what remove and change were asked, and answers with an undo - a
    removal found first, as find_removal gives it, and only removed once it's run.
    """

    def __init__(self, found="Dentist"):
        self.asked = []
        self.removed = []
        self.undone = 0
        # The event find_removal finds, or what it says instead for none - "which one?" or no match.
        self.found = found

    def activity_calendars(self):
        return {}

    def find_removal(self, search, day=None, at=None, following=False, calendar=None, today=None, now=None):
        self.asked.append(("remove", search, day, at, following, calendar))
        if isinstance(self.found, dict):
            return self.found
        name = self.found

        def run():
            self.removed.append(name)
            return {"success": True, "message": f"Removed {name}.", "undo": self._undo}
        return Removal(f"Remove '{name}', Thu 26 Nov 15:00", run)

    def change(self, search, day=None, at=None, start=None, end=None, summary=None, following=False, calendar=None,
               today=None, now=None):
        self.asked.append(("change", search, day, at, start, end, summary, following, calendar))
        return {"success": True, "message": "Moved Dentist.", "undo": self._undo}

    def _undo(self):
        self.undone += 1
        return {"success": True, "message": "Undone."}


def test_remove_from_calendar_passes_the_day_time_and_from_this_one_on():
    calendar = RecordingChangeCalendar()
    executor = ActionExecutor(calendar=calendar, approvals=FakeApprovals("approve"))
    offered = {schema["name"]: schema for schema in executor.tool_schemas()}
    assert offered["remove_from_calendar"]["input_schema"]["required"] == ["search"]
    assert set(offered["remove_from_calendar"]["input_schema"]["properties"]) == {
        "search", "day", "time", "following", "calendar"}

    result = executor.execute(Action("remove_from_calendar", {
        "search": "yoga", "day": "2026-11-26", "time": "18:30", "following": True, "calendar": "work"},
        from_model=True))
    assert calendar.asked == [("remove", "yoga", date(2026, 11, 26), "18:30", True, "work")]
    assert (result.ok, result.say) == (True, "Removed Dentist.")

    # Only what the user said is passed, and "from this one on" isn't assumed.
    calendar.asked.clear()
    executor.execute(Action("remove_from_calendar", {"search": "dentist", "following": None}, from_model=True))
    assert calendar.asked == [("remove", "dentist", None, None, False, None)]


def test_change_calendar_event_passes_the_new_times_and_name():
    calendar = RecordingChangeCalendar()
    executor = ActionExecutor(calendar=calendar)
    offered = {schema["name"]: schema for schema in executor.tool_schemas()}
    assert offered["change_calendar_event"]["input_schema"]["required"] == ["search"]
    assert set(offered["change_calendar_event"]["input_schema"]["properties"]) == {
        "search", "day", "time", "new_start", "new_end", "new_name", "following", "calendar"}

    result = executor.execute(Action("change_calendar_event", {
        "search": "dentist", "day": "2026-11-26", "time": "15:00", "new_start": "2026-11-27 16:00",
        "new_end": "2026-11-27 17:00", "new_name": "Check-up", "following": True, "calendar": "personal"},
        from_model=True))
    assert calendar.asked == [("change", "dentist", date(2026, 11, 26), "15:00", "2026-11-27 16:00",
                               "2026-11-27 17:00", "Check-up", True, "personal")]
    assert (result.ok, result.say) == (True, "Moved Dentist.")

    calendar.asked.clear()
    executor.execute(Action("change_calendar_event", {"search": "dentist", "new_name": "Check-up"}, from_model=True))
    assert calendar.asked == [("change", "dentist", None, None, None, None, "Check-up", False, None)]


def test_the_change_tool_says_how_a_day_alone_or_a_time_alone_is_given():
    offered = {schema["name"]: schema for schema in ActionExecutor(calendar=RecordingChangeCalendar()).tool_schemas()}
    description = offered["change_calendar_event"]["description"]
    # A day alone keeps the time - it isn't "all day" for a timed event - and a time alone keeps the day,
    # and with following always the time alone unless the user named a new day.
    assert "for all day" not in description
    for words in ("'YYYY-MM-DD' for the day alone", "keeps its time", "'HH:MM' for the time alone", "keeps its day",
                  "with following, always the time alone unless the user named a new day"):
        assert words in description, words
    # Read as "always pass following", it would change a whole series when one day was meant.
    assert "always with following" not in description
    # Adding is another matter: there a date alone is all day.
    assert "for all day" in offered["add_to_calendar"]["description"]


def test_the_change_tools_say_following_covers_every_event_of_that_name_from_then_on():
    # Five gym sessions made one by one are five events, so "all my gym sessions" is following too.
    offered = {schema["name"]: schema for schema in ActionExecutor(calendar=RecordingChangeCalendar()).tool_schemas()}
    for tool in ("remove_from_calendar", "change_calendar_event"):
        description = offered[tool]["description"]
        assert "Following also means every event of that name from then on" in description, tool
        for words in ("'all my gym sessions'", "'all of them'", "'every one'", "even when each is an event of its own"):
            assert words in description, (tool, words)
        assert "'all my gym sessions'" in offered[tool]["input_schema"]["properties"]["following"]["description"], tool


def test_an_unreadable_day_is_said():
    calendar = RecordingChangeCalendar()
    executor = ActionExecutor(calendar=calendar)
    for tool in ("remove_from_calendar", "change_calendar_event"):
        result = executor.execute(Action(tool, {"search": "dentist", "day": "Thursday"}, from_model=True))
        assert (result.ok, result.say) == (False, "I didn't catch which day you meant."), tool
    assert calendar.asked == []


def test_a_calendar_change_is_kept_for_undo():
    calendar = RecordingChangeCalendar()
    executor = ActionExecutor(calendar=calendar, approvals=FakeApprovals("approve"))
    assert executor.execute(Action("remove_from_calendar", {"search": "dentist"}, from_model=True)).ok
    assert calendar.undone == 0
    assert executor.execute(Action("undo_last_change")).say == "Undone."
    assert calendar.undone == 1

    assert executor.execute(Action("change_calendar_event", {"search": "dentist", "new_name": "Check-up"},
                                   from_model=True)).ok
    assert executor.execute(Action("undo_last_change")).say == "Undone."
    assert calendar.undone == 2


# Removing needs his OK - see approvals.py

CANCEL_DENTIST = Action("remove_from_calendar", {"search": "dentist", "day": "2026-11-26"}, from_model=True)


def test_removing_an_event_asks_first_with_what_it_found():
    calendar = RecordingChangeCalendar()
    approvals = FakeApprovals("wait")
    executor = ActionExecutor(calendar=calendar, approvals=approvals, satellite="kitchen")
    result = executor.execute(CANCEL_DENTIST)
    # Asked after the lookup, naming the real event, and said where the outcome is to be told.
    assert calendar.asked == [("remove", "dentist", date(2026, 11, 26), None, False, None)]
    assert approvals.asked == [("Remove 'Dentist', Thu 26 Nov 15:00", "kitchen")]
    assert (result.ok, result.say) == (True, pending_said(["your phone"]))
    # Nothing is removed while it waits.
    assert calendar.removed == []
    assert [reply["message"] for reply in approvals.approve_waiting()] == ["Removed Dentist."]
    assert calendar.removed == ["Dentist"]


def test_no_match_is_said_without_asking():
    for said in ("I can't find the optician on your calendar.",
                 "I found 2: Dentist on Monday, at 9 AM; and Dentist on Thursday, at 3 PM. Which one?"):
        calendar = RecordingChangeCalendar(found={"success": False, "message": said})
        approvals = FakeApprovals("wait")
        result = ActionExecutor(calendar=calendar, approvals=approvals).execute(CANCEL_DENTIST)
        assert (result.ok, result.say) == (False, said)
        assert approvals.asked == [] and calendar.removed == []


def test_with_no_approvals_nothing_is_removed():
    # No Approvals at all, or none with a way to approve: refused, never done.
    for approvals in (None, FakeApprovals("none")):
        calendar = RecordingChangeCalendar()
        result = ActionExecutor(calendar=calendar, approvals=approvals).execute(CANCEL_DENTIST)
        assert (result.ok, result.say) == (False, NO_WAY)
        assert calendar.removed == []


def test_the_model_cant_skip_the_approval():
    # Claude's own call goes through the same gate as a local one - there's no parameter to skip it.
    calendar = RecordingChangeCalendar()
    approvals = FakeApprovals("wait")
    executor = ActionExecutor(calendar=calendar, approvals=approvals)
    executor.execute(Action("remove_from_calendar", {"search": "dentist", "approved": True, "skip": True},
                            from_model=True))
    assert len(approvals.asked) == 1 and calendar.removed == []
    assert "approv" not in str(ActionExecutor.tools()["remove_from_calendar"].tool["parameters"])


def test_two_removals_in_one_turn_each_ask():
    calendar = RecordingChangeCalendar()
    approvals = FakeApprovals("wait")
    executor = ActionExecutor(calendar=calendar, approvals=approvals)
    executor.execute(CANCEL_DENTIST)
    calendar.found = "Gym"
    executor.execute(Action("remove_from_calendar", {"search": "gym"}, from_model=True))
    assert [description for description, _ in approvals.asked] == [
        "Remove 'Dentist', Thu 26 Nov 15:00", "Remove 'Gym', Thu 26 Nov 15:00"]
    assert calendar.removed == []
    approvals.approve_waiting()
    assert calendar.removed == ["Dentist", "Gym"]


def test_a_change_made_while_a_removal_waits_leaves_the_approved_removal_as_that():
    # The removal is approved and kept on the approval's own thread, after a change made meanwhile:
    # it's the last thing done, so "undo that" takes it back, and only it.
    calendar = RecordingChangeCalendar()
    approvals = FakeApprovals("wait")
    executor = ActionExecutor(calendar=calendar, approvals=approvals)
    executor.execute(CANCEL_DENTIST)
    undone = []
    assert executor.execute(Action("change_calendar_event", {"search": "gym", "new_start": "2026-11-26 18:00"})).ok
    calendar._undo = lambda: undone.append("removal") or {"success": True, "message": "Put Dentist back."}
    approving = threading.Thread(target=approvals.approve_waiting)
    approving.start()
    approving.join(5)
    assert calendar.removed == ["Dentist"]
    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (True, "Put Dentist back.") and undone == ["removal"]
    assert executor.execute(UNDO_THAT).say == NOTHING_TO_UNDO


def test_a_routine_and_a_local_command_ask_too():
    # A local command - or a routine's step a local rule caught - comes without from_model, and a
    # routine's step Claude answered with it; both are asked about, as the gate is in the tool itself.
    # The routine itself, through a Conversation, is in test_conversation.
    calendar = RecordingChangeCalendar()
    approvals = FakeApprovals("wait")
    executor = ActionExecutor(calendar=calendar, approvals=approvals)
    for from_model in (False, True):
        result = executor.execute(Action("remove_from_calendar", {"search": "dentist"}, from_model=from_model))
        assert result.say == pending_said(["your phone"]), from_model
    assert len(approvals.asked) == 2 and calendar.removed == []


def test_the_calendar_change_tools_say_so_when_the_calendar_isnt_set_up():
    executor = ActionExecutor()
    offered = [schema["name"] for schema in executor.tool_schemas()]
    assert "remove_from_calendar" not in offered and "change_calendar_event" not in offered
    for tool in ("remove_from_calendar", "change_calendar_event"):
        result = executor.execute(Action(tool, {"search": "dentist"}))
        assert (result.ok, result.say) == (False, "The calendar isn't set up yet."), tool


# "Undo that"

class ChangingCalendar:
    """A calendar whose add works, leaving a way to undo it, until it's made to fail."""

    def __init__(self):
        self.works = True
        self.undone = 0

    def activity_calendars(self):
        return {}

    def agenda(self, first, days):
        return {"success": True, "message": "You've got nothing on."}

    def add(self, summary, start, end=None, all_day=None, calendar=None):
        if not self.works:
            return {"success": False, "message": "I couldn't add that."}
        return {"success": True, "message": "Added.", "undo": self._undo}

    def _undo(self):
        self.undone += 1
        return {"success": True, "message": "Undone."}


DENTIST = Action("add_to_calendar", {"summary": "Dentist", "start": "2026-11-26 15:00"})
UNDO_THAT = Action("undo_last_change")


def test_undo_that_runs_the_last_changes_undo_and_says_what_it_says():
    calendar = ChangingCalendar()
    executor = ActionExecutor(calendar=calendar)
    assert executor.execute(DENTIST).say == "Added."
    assert calendar.undone == 0

    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (True, "Undone.")
    assert calendar.undone == 1

    # Kept for one undo only.
    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (False, NOTHING_TO_UNDO)
    assert calendar.undone == 1


def test_an_undo_cant_itself_be_undone():
    class UndoingCalendar(ChangingCalendar):
        def _undo(self):
            self.undone += 1
            # An undo which, wrongly, leaves a way to take itself back.
            return {"success": True, "message": "Undone.", "undo": self._undo}

    calendar = UndoingCalendar()
    executor = ActionExecutor(calendar=calendar)
    executor.execute(DENTIST)
    assert executor.execute(UNDO_THAT).say == "Undone."
    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (False, NOTHING_TO_UNDO)
    assert calendar.undone == 1


def test_undo_that_is_always_offered_and_says_what_it_reverses():
    offered = {schema["name"]: schema for schema in ActionExecutor().tool_schemas()}
    description = offered["undo_last_change"]["description"]
    for words in ("playlist or calendar change", "undo that", "put it back", "take that back"):
        assert words in description
    assert ActionExecutor().execute(UNDO_THAT).say == NOTHING_TO_UNDO


def test_undo_that_after_ten_minutes_is_too_late():
    now = [1000.0]
    calendar = ChangingCalendar()
    executor = ActionExecutor(calendar=calendar)
    executor.last_change = LastChange(clock=lambda: now[0])
    executor.execute(DENTIST)
    now[0] += UNDO_CHANGE_KEPT + 1
    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (False, TOO_LATE)
    assert calendar.undone == 0


def test_another_action_since_means_undo_leaves_the_change():
    calendar = ChangingCalendar()
    executor = ActionExecutor(calendar=calendar, clock=RecordingClock())
    executor.execute(DENTIST)
    assert executor.execute(Action("set_timer", {"duration_seconds": 600})).ok

    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (False, SOMETHING_ELSE_SINCE)
    assert calendar.undone == 0


def test_one_slot_shared_by_two_satellites_is_taken_over_by_whatever_either_does():
    # The PC and the Pi in one room: "cancel the dentist" on one, a timer on the other, and "undo that" on the
    # first mustn't put the dentist back, as the timer was the last thing done in the house.
    shared = LastChange()
    calendar = ChangingCalendar()
    first = ActionExecutor(calendar=calendar, last_change=shared)
    second = ActionExecutor(clock=RecordingClock(), last_change=shared)
    assert first.last_change is second.last_change is shared
    first.execute(DENTIST)
    assert second.execute(Action("set_timer", {"duration_seconds": 600})).ok

    result = first.execute(UNDO_THAT)
    assert (result.ok, result.say) == (False, SOMETHING_ELSE_SINCE)
    assert calendar.undone == 0

    # And a change on the other satellite is the one "undo that" takes back, wherever it's said.
    first.execute(DENTIST)
    assert second.execute(UNDO_THAT).say == "Undone."
    assert calendar.undone == 1

    # With no slot given, each has one of its own, as every builder had before.
    assert ActionExecutor().last_change is not ActionExecutor().last_change


def test_an_action_which_answers_with_a_result_of_its_own_counts_as_another_action():
    class QuietClock:
        def dismiss(self, satellite=None):
            return ActionResult(True, "Okay.", confirm="chime")

    calendar = ChangingCalendar()
    executor = ActionExecutor(calendar=calendar, clock=QuietClock())
    executor.execute(DENTIST)
    assert executor.execute(Action("dismiss_timer")).ok
    assert executor.execute(UNDO_THAT).say == SOMETHING_ELSE_SINCE
    assert calendar.undone == 0


def test_a_calendar_read_or_the_timers_listed_since_leave_the_change_undoable():
    # Neither is read back to the model, but neither changes anything - "what's on Thursday?"
    # after a change isn't a reason to refuse "undo that".
    class ListingClock:
        def list_timers(self):
            return {"success": True, "message": "You don't have any timers running."}

    calendar = ChangingCalendar()
    executor = ActionExecutor(calendar=calendar, clock=ListingClock())
    executor.execute(DENTIST)
    assert executor.execute(Action("get_calendar", {"start": "2026-11-26"})).say == "You've got nothing on."
    assert executor.execute(Action("list_timers")).ok

    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (True, "Undone.")
    assert calendar.undone == 1


def test_the_tools_which_only_say_something_are_declared_so():
    # Declared one by one, as a tool which says something but also changes a little - which
    # clears what was missed, or marks notifications heard, or pauses the music - must count.
    tools = ActionExecutor.tools()
    for name in ("get_calendar", "get_activity", "get_weather", "list_timers", "list_alarms", "next_alarm",
                 "read_list", "list_memories", "get_batteries", "tell_me_more"):
        assert tools[name].tool["changes"] is False, name
    for name in ("control_playback", "check_missed", "read_notifications", "manage_list", "journey",
                 "find_device", "set_timer", "add_to_calendar", "undo_last_change"):
        assert tools[name].tool["changes"] is True, name


def test_a_failed_action_or_a_read_since_leaves_the_change_undoable():
    calendar = ChangingCalendar()
    home = SimpleNamespace(status=lambda about: {"success": True, "message": "The sun sets at 4."})
    executor = ActionExecutor(calendar=calendar, home=home)
    executor.execute(DENTIST)

    # A change which didn't work, whatever the tool.
    calendar.works = False
    assert executor.execute(DENTIST).ok is False
    assert executor.execute(Action("get_calendar", {"start": "Thursday"})).ok is False
    # Something Home Assistant is only read from, to answer from.
    assert executor.execute(Action("home_status", {"about": "sunset"})).ok

    result = executor.execute(UNDO_THAT)
    assert (result.ok, result.say) == (True, "Undone.")
    assert calendar.undone == 1


def test_a_change_which_failed_leaves_nothing_to_undo():
    calendar = ChangingCalendar()
    calendar.works = False
    executor = ActionExecutor(calendar=calendar)
    assert executor.execute(DENTIST).ok is False
    assert executor.execute(UNDO_THAT).say == NOTHING_TO_UNDO


def test_a_change_which_says_it_failed_is_never_kept():
    # Even with an undo in its reply, which a failure shouldn't have.
    class OddCalendar(ChangingCalendar):
        def add(self, summary, start, end=None, all_day=None, calendar=None):
            return {"success": False, "message": "No.", "undo": self._undo}

    calendar = OddCalendar()
    executor = ActionExecutor(calendar=calendar)
    executor.execute(DENTIST)
    assert executor.execute(UNDO_THAT).say == NOTHING_TO_UNDO
    assert calendar.undone == 0


def test_without_home_assistant_the_lights_say_so():
    for action in (Action("control_home", {"command": "lights on"}), Action("set_lights", {"on": True})):
        result = ActionExecutor().execute(action)
        assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet."), action.tool


def test_claude_isnt_offered_control_home_but_is_offered_set_lights():
    names = [schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()]
    assert "set_lights" in names and "control_home" not in names
    assert "set_lights" not in [schema["name"] for schema in ActionExecutor().tool_schemas()]


def test_dim_lights_isnt_offered_to_claude():
    names = [schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()]
    assert "dim_lights" not in names and "set_lights" in names


def test_dimming_is_passed_to_home_assistant():
    class FakeHome:
        def dim(self, direction, words):
            self.asked = (direction, words)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("dim_lights", {"direction": "up", "words": "the lamp"}))
    assert home.asked == ("up", "the lamp") and (result.ok, result.confirm) == (True, "chime")


def test_claude_cant_run_dim_lights_either():
    class FakeHome:
        def dim(self, direction, words):
            raise AssertionError("not for Claude")

    result = ActionExecutor(home=FakeHome()).execute(
        Action("dim_lights", {"direction": "down", "words": "the lights"}, from_model=True))
    assert result.ok is False


def test_without_home_assistant_dimming_says_so():
    result = ActionExecutor().execute(Action("dim_lights", {"direction": "down", "words": "the lights"}))
    assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet.")


def test_a_local_light_command_still_reaches_assist():
    # What Claude isn't offered can still be asked for: the local phrases ("lamp off") and the
    # offline keyword fallback both produce control_home, which goes to Assist as it always did.
    class FakeHome:
        def command(self, request, wider=False):
            self.asked = request
            return {"success": True, "message": "Turned off the lamp", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "lamp off"}))
    assert home.asked == "lamp off" and (result.ok, result.confirm) == (True, "chime")


def test_control_home_passes_the_wider_rule_on():
    class FakeHome:
        def command(self, request, wider=False):
            self.asked = (request, wider)
            return {"success": True, "message": "Done", "chime": True}

    home = FakeHome()
    ActionExecutor(home=home).execute(Action("control_home", {"command": "turn off the lights and the TV",
                                                              "wider": True}))
    assert home.asked == ("turn off the lights and the TV", True)
    ActionExecutor(home=home).execute(Action("control_home", {"command": "lamp off"}))
    assert home.asked == ("lamp off", False)


def test_a_refused_light_sentence_goes_to_novas_own_lights_and_nothing_else_does():
    class FakeHome:
        switched = None

        def command(self, request, wider=False):
            return {"success": False, "message": "I don't lock, unlock or open anything by voice.", "refused": True}

        def switch_lights(self, on, words, brightness=None):
            self.switched = (on, words, brightness)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "set the porch light to 40 percent", "on": True, "words": "the porch light", "brightness": 40}))
    assert home.switched == (True, "the porch light", 40) and (result.ok, result.confirm) == (True, "chime")

    # The offline fallback's sentence, which says nothing of switching lights, stays refused.
    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "turn off the front door"}))
    assert home.switched is None
    assert (result.ok, result.say) == (False, "I don't lock, unlock or open anything by voice.")


def test_a_refused_question_goes_to_claude_and_nothing_else_does():
    # "Is the garage door open?" is refused for its words, but Claude can answer it with
    # home_status, which only reads - never Assist, nor Nova's own lights.
    class FakeHome:
        switched = None
        asked = 0

        def command(self, request, wider=False):
            self.asked += 1
            return {"success": False, "message": "I don't lock, unlock or open anything by voice.", "refused": True}

        def switch_lights(self, on, words, brightness=None):
            self.switched = (on, words, brightness)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "is the garage door open", "wider": True, "question": True}))
    assert (result.ok, result.understood) == (False, False)
    assert home.asked == 1 and home.switched is None
    # A refusal that isn't a question is still said.
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "open the garage", "wider": True}))
    assert (result.ok, result.understood, result.say) == (False, True, "I don't lock, unlock or open anything by voice.")


def test_claude_cant_run_what_it_isnt_offered():
    # It can ask for any tool by name, offered or not - control_home passes free text to
    # Assist, so a made-up call to it mustn't run.
    class FakeHome:
        asked = None

        def command(self, request, wider=False):
            self.asked = request
            return {"success": True, "message": "Unlocked", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "unlock the door"}, from_model=True))
    assert home.asked is None and result.ok is False


def test_claude_runs_a_script_or_scene_by_name_and_hears_what_happened():
    class FakeHome:
        asked = None

        def run_script(self, name):
            self.asked = name
            return {"success": True, "message": "Started Goodnight."}

    home = FakeHome()
    assert "run_home_script" in [schema["name"] for schema in ActionExecutor(home=home).tool_schemas()]
    result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": "goodnight"}, from_model=True))
    assert home.asked == "goodnight" and (result.ok, result.say, result.confirm) == (True, "Started Goodnight.", "speak")
    # Without a name, Home Assistant isn't asked.
    home.asked = None
    result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": " "}, from_model=True))
    assert home.asked is None and (result.ok, result.say) == (False, "I didn't catch which script or scene to run.")


def test_a_script_name_that_isnt_words_is_answered_as_no_name():
    class FakeHome:
        asked = None

        def run_script(self, name):
            self.asked = name
            return {"success": True, "message": "Started Goodnight."}

    home = FakeHome()
    for name in (5, ["goodnight"], None):
        result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": name}, from_model=True))
        assert (result.ok, result.say) == (False, "I didn't catch which script or scene to run."), name
    assert home.asked is None


def test_without_home_assistant_no_script_is_offered_or_run():
    assert "run_home_script" not in [schema["name"] for schema in ActionExecutor().tool_schemas()]
    result = ActionExecutor().execute(Action("run_home_script", {"name": "goodnight"}, from_model=True))
    assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet.")


def test_claude_works_the_lights_by_light_room_brightness_colour_and_warmth():
    class FakeHome:
        def set_lights(self, on, light=None, area=None, brightness=None, colour=None, warmth=None):
            self.asked = (on, light, area, brightness, colour, warmth)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    executor = ActionExecutor(home=home)
    result = executor.execute(Action("set_lights", {"on": True, "area": "Kitchen", "brightness": 30, "colour": "blue",
                                                   "warmth": 2700, "volume": 11}))
    assert home.asked == (True, None, "Kitchen", 30, "blue", 2700)
    assert (result.ok, result.confirm) == (True, "chime")

    executor.execute(Action("set_lights", {"on": False, "light": "light.lamp"}))
    assert home.asked == (False, "light.lamp", None, None, None, None)


def test_the_lights_wont_be_switched_without_knowing_which_way():
    class FakeHome:
        def set_lights(self, on, **others):
            raise AssertionError("shouldn't be asked")

    result = ActionExecutor(home=FakeHome()).execute(Action("set_lights", {"area": "Kitchen"}))
    assert (result.ok, result.say) == (False, "I didn't catch whether to switch the lights on or off.")


def test_a_light_which_cant_be_worked_is_told_in_homes_words():
    class FakeHome:
        def set_lights(self, on, **others):
            return {"success": False, "message": "Which room?"}

    result = ActionExecutor(home=FakeHome()).execute(Action("set_lights", {"on": True}))
    assert (result.ok, result.say) == (False, "Which room?")


def test_an_activity_is_asked_of_the_calendar_for_the_days_given():
    class FakeCalendar:
        def activity(self, name, first, days):
            self.asked = (name, first.isoformat(), days)
            return {"success": True, "message": "You haven't got uni tomorrow."}

    calendar = FakeCalendar()
    result = ActionExecutor(calendar=calendar).execute(
        Action("get_activity", {"activity": "uni", "start": "2026-10-21"}))
    assert calendar.asked == ("uni", "2026-10-21", 1) and result.say == "You haven't got uni tomorrow."


def test_the_next_times_are_looked_for_across_a_fortnight():
    class FakeCalendar:
        def activity(self, name, first, days, count=None):
            self.asked = (name, first.isoformat(), days, count)
            return {"success": True, "message": "You're next at work tomorrow."}

    calendar = FakeCalendar()
    ActionExecutor(calendar=calendar).execute(
        Action("get_activity", {"activity": "work", "start": "2026-10-20", "count": 50}))
    assert calendar.asked == ("work", "2026-10-20", 14, 10)


# Notifications

class FakeNtfy:
    """Stands in for the user's ntfy server, keeping what was sent to it."""

    def __init__(self, phone="clipboard", works=True):
        self.phone = phone
        self.works = works
        self.sent = []

    def send(self, topic, message, title="", priority=0):
        self.sent.append({"topic": topic, "title": title, "message": message})
        return self.works

    def unheard(self):
        return {"success": True, "message": "No new notifications."}

    def tell_more(self):
        return {"success": True, "message": "Backup failed. Error 5."}


def test_send_that_to_my_phone_sends_what_was_last_said_under_what_was_asked():
    ntfy = FakeNtfy()
    executor = ActionExecutor(ntfy=ntfy)
    executor.last_answer = ("What's the recipe for pancakes?", "Mix flour, eggs and milk, then fry them.")
    result = executor.execute(Action("send_to_phone"))

    assert (result.ok, result.confirm) == (True, "chime")
    assert ntfy.sent == [{"topic": "clipboard", "title": "What's the recipe for pancakes?",
                          "message": "Mix flour, eggs and milk, then fry them."}]


class ClearedNtfy(FakeNtfy):
    """An ntfy whose phone topic is cleared on the settings page just after it's first read."""

    @property
    def phone(self):
        topics, self._topics = self._topics, [""]
        return topics[0]

    @phone.setter
    def phone(self, topic):
        self._topics = [topic]


def test_the_phone_topic_is_read_once_for_each_send():
    # So a topic cleared between checking it and sending to it is never sent to as "".
    ntfy = ClearedNtfy()
    ActionExecutor(ntfy=ntfy).execute(Action("send_to_phone", {"text": "Milk", "title": "Shopping"}))
    assert [sent["topic"] for sent in ntfy.sent] == ["clipboard"]


def test_claude_can_send_more_than_it_said():
    ntfy = FakeNtfy()
    ActionExecutor(ntfy=ntfy).execute(Action("send_to_phone", {"text": "100g flour, 2 eggs, 300ml milk...",
                                                              "title": "Pancake recipe"}))
    assert ntfy.sent == [{"topic": "clipboard", "title": "Pancake recipe", "message": "100g flour, 2 eggs, 300ml milk..."}]


def test_theres_nothing_to_send_before_anything_was_said():
    result = ActionExecutor(ntfy=FakeNtfy()).execute(Action("send_to_phone"))
    assert (result.ok, result.say) == (False, "There's nothing to send yet.")


def test_sending_says_when_it_couldnt():
    executor = ActionExecutor(ntfy=FakeNtfy(works=False))
    executor.last_answer = ("asked", "said")
    assert executor.execute(Action("send_to_phone")).say == "I couldn't reach your notifications."
    # Nor with nowhere set up to send it.
    assert ActionExecutor(ntfy=FakeNtfy(phone="")).execute(Action("send_to_phone")).ok is False


def test_notifications_are_only_offered_once_ntfy_is_set_up():
    def offered(executor):
        return {schema["name"] for schema in executor.tool_schemas()}

    tools = {"send_to_phone", "read_notifications", "tell_me_more"}
    assert tools <= offered(ActionExecutor(ntfy=FakeNtfy()))
    assert not tools & offered(ActionExecutor())
    assert ActionExecutor().execute(Action("read_notifications")).say == "Notifications aren't set up yet."


class FakeBatteries:
    """Stands in for the battery watcher, reading what it's given - or failing as Home Assistant can."""

    stale_after = 3600.0

    def __init__(self, batteries=(), fails=None):
        self.batteries, self.fails = list(batteries), fails

    def read(self):
        if self.fails is not None:
            raise self.fails
        return self.batteries


def _battery(name, level, kind="other", charging=False, age=0.0):
    return Battery(id=f"sensor.{name}", name=name, level=level, changed=time.time() - age, device=None,
                   kind=kind, phone=False, computer=False, charging=charging)


def test_get_batteries_says_each_one_found():
    executor = ActionExecutor(batteries=FakeBatteries([
        _battery("MacBook Pro", 72), _battery("S25 Ultra", 40, "mobile", False),
        _battery("Tab S11 Ultra", 88, "mobile", age=3 * 86400)]))
    everything = executor.execute(Action("get_batteries"))
    assert everything.ok
    assert everything.say == ("Your MacBook Pro's on 72%, and not charging. "
                              "Your S25 Ultra's on 40%, and not charging. "
                              "Your Tab S11 Ultra was on 88%, 3 days ago.")
    # The same sentence the local commands and the watcher use, for just the one asked about.
    assert executor.execute(Action("get_batteries", {"device": "mac"})).say == "Your MacBook Pro's on 72%, and not charging."
    assert executor.execute(Action("get_batteries", {"device": "tab"})).say == "Your Tab S11 Ultra was on 88%, 3 days ago."


def test_get_batteries_finds_nothing_called_that():
    executor = ActionExecutor(batteries=FakeBatteries([_battery("MacBook Pro", 72)]))
    result = executor.execute(Action("get_batteries", {"device": "kettle"}))
    assert (result.ok, result.say) == (False, "I can't find a battery called that.")
    # One asked for by name is still called that, though none is listed at all.
    result = ActionExecutor(batteries=FakeBatteries([])).execute(Action("get_batteries", {"device": "mac"}))
    assert (result.ok, result.say) == (False, "I can't find a battery called that.")


def test_get_batteries_with_home_assistant_down_says_so():
    result = ActionExecutor(batteries=FakeBatteries(fails=requests.ConnectionError("down"))).execute(
        Action("get_batteries"))
    assert (result.ok, result.say) == (False, "I can't reach Home Assistant right now.")


def test_get_batteries_with_a_token_home_assistant_refuses_says_so_not_that_its_down():
    refused = requests.HTTPError("401", response=SimpleNamespace(status_code=401))
    result = ActionExecutor(batteries=FakeBatteries(fails=refused)).execute(Action("get_batteries"))
    assert (result.ok, result.say) == (
        False, "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking.")


@pytest.mark.parametrize("params", [{}, {"device": ""}, {"device": "all"}])
def test_get_batteries_with_none_at_all_says_so_when_none_was_asked_about(params):
    # "Battery levels" with nothing to say isn't a battery "called that" which can't be found.
    result = ActionExecutor(batteries=FakeBatteries([])).execute(Action("get_batteries", params))
    assert (result.ok, result.say) == (False, "Home Assistant hasn't got any batteries to tell you about.")


@pytest.mark.parametrize("failure", [ValueError("not JSON"), KeyError("unit"), TypeError("not a list")])
def test_get_batteries_with_an_answer_home_assistant_got_wrong_says_so(failure):
    # A wrong-shaped answer raises KeyError or TypeError, not only ValueError. Home Assistant was reached.
    result = ActionExecutor(batteries=FakeBatteries(fails=failure)).execute(Action("get_batteries"))
    assert (result.ok, result.say) == (False, "Home Assistant had a problem with that.")


def test_a_bug_reading_batteries_isnt_taken_for_home_assistant_being_down():
    with pytest.raises(RuntimeError):
        ActionExecutor(batteries=FakeBatteries(fails=RuntimeError("bug"))).execute(Action("get_batteries"))


def test_batteries_are_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"] for schema in executor.tool_schemas()}

    assert "get_batteries" in offered(ActionExecutor(batteries=FakeBatteries()))
    assert "get_batteries" not in offered(ActionExecutor())
    assert ActionExecutor().execute(Action("get_batteries")).say == "Battery levels need Home Assistant."


# Where the phone is, and ringing it - see devices.py

class FakeAppHome:
    """
    Stands in for Home Assistant: the app's devices, Find My's - none unless a test gives
    some - and the notifications it was asked to send, and the buttons it was asked to press.
    """

    def __init__(self, devices, notify_fails=None, find_mys=(), press_fails=None):
        self.devices = devices
        self.notify_fails = notify_fails
        self.find_mys = list(find_mys)
        self.press_fails = press_fails
        self.sent = []
        self.titles = []
        self.pressed = []

    def app_devices(self):
        return self.devices

    def find_my_devices(self):
        return self.find_mys

    def find_my_names(self):
        return [found["name"] for found in self.find_mys]

    def press(self, entity_id):
        if self.press_fails is not None:
            raise self.press_fails
        self.pressed.append(entity_id)

    def notify(self, service, message, title=None, data=None):
        if self.notify_fails is not None:
            raise self.notify_fails
        self.sent.append((service, message, data))
        self.titles.append(title)


def _app_device(name="s25-ultra", maker="samsung", model="SM-S938B", notify="mobile_app_s25_ultra",
                tracker="not_home", age=600.0):
    return {"device": f"dev_{name}", "name": name, "maker": maker, "model": model, "notify": notify,
            "tracker": {"state": tracker, "reported": time.time() - age},
            "place": {"state": "Example Road, Sometown", "thoroughfare": "Example Road", "locality": "Sometown"}}


def test_find_device_says_where_without_ringing():
    home = FakeAppHome([_app_device(tracker="home")])
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": False}))
    assert (result.ok, result.say, result.private) == (True, "Your phone's at home.", True)
    assert home.sent == []


def test_ring_my_phone_rings_wherever_the_user_is():
    # Away from home it's still rung - it may be in the car, or a friend's.
    home = FakeAppHome([_app_device()])
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert result.say == "Your phone's near Example Road, Sometown, as of 10 minutes ago. Ringing your phone."
    assert result.private
    assert [(service, message) for service, message, _ in home.sent] == [("mobile_app_s25_ultra", "TTS")]


def test_ringing_is_only_said_once_home_assistant_took_it():
    failure = requests.HTTPError("500", response=SimpleNamespace(status_code=500))
    home = FakeAppHome([_app_device(tracker="home")], notify_fails=failure)
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert (result.ok, result.say) == (False, "Your phone's at home. I couldn't ring your phone.")
    assert result.private


def test_ringing_unanswered_may_have_rung_but_a_connection_never_made_didnt():
    ring = Action("find_device", {"device": "phone", "ring": True})
    # Sent, but unanswered - so it may well be ringing.
    home = FakeAppHome([_app_device(tracker="home")], notify_fails=requests.ReadTimeout("slow"))
    result = ActionExecutor(home=home).execute(ring)
    assert result.say == ("Your phone's at home. Home Assistant didn't answer in time, so I can't tell whether "
                          "your phone rang.")
    # A ConnectTimeout is also a Timeout, but nothing was sent - said as a refused connection is.
    home = FakeAppHome([_app_device(tracker="home")], notify_fails=requests.ConnectTimeout("no connection"))
    result = ActionExecutor(home=home).execute(ring)
    assert (result.ok, result.say) == (False, "Your phone's at home. I couldn't ring your phone.")
    assert result.private


def test_a_device_without_the_apps_notifications_cant_be_rung():
    mac = _app_device("macbook-pro", "Apple", "Mac16,8", notify=None, tracker="home")
    result = ActionExecutor(home=FakeAppHome([mac])).execute(Action("find_device", {"device": "mac", "ring": True}))
    assert result.say == ("Your Mac's at home. Your Mac can't be rung - it hasn't got the Home Assistant app's "
                          "notifications.")


def test_find_device_finds_nothing_called_that():
    result = ActionExecutor(home=FakeAppHome([_app_device()])).execute(Action("find_device", {"device": "kettle"}))
    assert (result.ok, result.say, result.private) == (False, "I haven't got a device called that.", True)


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), ValueError("not JSON"), KeyError("device")])
def test_find_device_with_home_assistant_down_says_so(failure):
    class Down(FakeAppHome):
        def app_devices(self):
            raise failure
    result = ActionExecutor(home=Down([])).execute(Action("find_device", {"device": "phone"}))
    assert (result.ok, result.say) == (False, "I can't reach Home Assistant right now.")


def test_find_device_is_offered_with_home_assistant():
    described = ("Say where one of the user's devices is - their phone, tablet or Mac, or anything in Home "
                 "Assistant's Find My, such as a tracker tag - and ring it if asked ('find my phone', 'make my "
                 "tablet ring', 'where's my wallet'). What's found is said for you, so just reply 'Okay.'")
    schemas = {s["name"]: s for s in ActionExecutor(home=FakeAppHome([])).tool_schemas()}
    assert schemas["find_device"]["description"] == described
    # Stopping it is offered with it.
    assert "stop_finding" in schemas
    # What Find My has is told, by the names said aloud, in order - and a Home Assistant which can't say isn't
    # the turn lost.
    home = FakeAppHome([], find_mys=[_find_my("Sam's Wallet", "w"), _find_my("Galaxy Phone 9", "p"),
                                     _find_my("Keys tag", "k")])
    schema = {s["name"]: s for s in ActionExecutor(home=home).tool_schemas()}["find_device"]
    assert schema["description"] == described + " Home Assistant's Find My has: Galaxy Phone 9, Keys tag and Wallet."
    schema = {s["name"]: s for s in ActionExecutor(home=object()).tool_schemas()}["find_device"]
    assert schema["description"] == described
    assert "find_device" not in {s["name"] for s in ActionExecutor().tool_schemas()}
    assert ActionExecutor().execute(Action("find_device", {"device": "phone"})).say == "Home Assistant isn't set up yet."
    # Its answers are the user's alone - see Conversation.handle.
    assert ActionExecutor.private_tools() == ["find_device"]
    # What it finds is said, never read back to Claude.
    assert "find_device" not in ActionExecutor().read_tools()


def _find_my(name="Galaxy Phone 9", key="phone", stop=True, tracker="home", ready=True):
    """One of HomeAssistant.find_my_devices()'s dicts."""
    return {"device": f"fm_{key}", "name": name, "play": f"button.{key}_play_sound",
            "stop": f"button.{key}_stop_sound" if stop else None,
            "tracker": {"state": tracker, "reported": time.time() - 600}, "ready": ready}


# The phone, as the app has it ("phone 9", from "phone-9"), and as Find My does ("Galaxy Phone 9") - the same phone.
def _phone_both_ways(**more):
    return FakeAppHome([_app_device("phone-9", tracker="home", notify="mobile_app_phone_9"),
                        _app_device("macbook-pro", "Apple", "Mac16,8", notify="mobile_app_macbook_pro",
                                    tracker="home")],
                       find_mys=[_find_my(), _find_my("Sam's Wallet", "wallet", tracker="not_home")], **more)


def test_phone_rings_through_find_my():
    home = _phone_both_ways()
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert (result.ok, result.say, result.private) == (True, "Your phone's at home. Ringing your phone.", True)
    assert home.pressed == ["button.phone_play_sound"] and home.sent == []


@pytest.mark.parametrize("failure", [requests.HTTPError("500", response=SimpleNamespace(status_code=500)),
                                     requests.ConnectionError("down"), NotAllowed("button.press")])
def test_falls_back_to_the_app_notification_when_find_my_fails(failure, caplog):
    # A 500 is Home Assistant failing to reach Google, too.
    home = _phone_both_ways(press_fails=failure)
    with caplog.at_level("INFO"):
        result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert (result.ok, result.say) == (True, "Your phone's at home. Ringing your phone.")
    assert [(service, message) for service, message, _ in home.sent] == [("mobile_app_phone_9", "TTS")]
    assert "Couldn't ring the Galaxy Phone 9 through Find My" in caplog.text
    assert "by the app's notification" in caplog.text
    # And with Find My having no such device, or unreadable, it's the notification as before.
    for found in ([], requests.ConnectionError("down")):
        home = _phone_both_ways()
        if isinstance(found, list):
            home.find_mys = found
        else:
            def unreadable():
                raise found
            home.find_my_devices = unreadable
        result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
        assert result.say == "Your phone's at home. Ringing your phone."
        assert home.pressed == [] and [service for service, _, _ in home.sent] == ["mobile_app_phone_9"]


def test_an_unavailable_play_sound_falls_back_to_the_app_notification(caplog):
    # While Google can't be reached the button is unavailable: pressed, it would do nothing, yet HA says it did.
    home = _phone_both_ways()
    home.find_mys[0]["ready"] = False
    executor = ActionExecutor(home=home)
    with caplog.at_level("INFO"):
        result = executor.execute(Action("find_device", {"device": "phone", "ring": True}))
    assert (result.ok, result.say) == (True, "Your phone's at home. Ringing your phone.")
    assert home.pressed == [] and [service for service, _, _ in home.sent] == ["mobile_app_phone_9"]
    assert "Play sound is unavailable - ringing it by the app's notification" in caplog.text
    # Nothing rang through Find My, so there's nothing for "found it" to stop.
    assert executor.rang.lately() == []
    # A tag has no other way to ring.
    home.find_mys[1]["ready"] = False
    result = executor.execute(Action("find_device", {"device": "wallet", "ring": True}))
    assert (result.ok, result.say) == (False, "Your Wallet's away from home, as of 10 minutes ago. "
                                              "I couldn't ring your Wallet.")
    assert home.pressed == [] and executor.rang.lately() == []


def test_mac_still_rings_by_notification():
    # Even with a Find My device whose name has every word of the Mac's.
    home = _phone_both_ways()
    home.find_mys.append(_find_my("MacBook Pro", "mac"))
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "mac", "ring": True}))
    assert result.say == "Your Mac's at home. Ringing your Mac."
    assert home.pressed == [] and [service for service, _, _ in home.sent] == ["mobile_app_macbook_pro"]


def test_wallet_found_and_rung_by_its_name():
    home = _phone_both_ways()
    executor = ActionExecutor(home=home)
    result = executor.execute(Action("find_device", {"device": "wallet", "ring": True}))
    assert (result.ok, result.private) == (True, True)
    assert result.say == "Your Wallet's away from home, as of 10 minutes ago. Ringing your Wallet."
    assert home.pressed == ["button.wallet_play_sound"] and home.sent == []
    # Its owner's name is a word of its name too, and a Find My device with no app's is never rung another way.
    home.press_fails = requests.ConnectionError("down")
    result = executor.execute(Action("find_device", {"device": "sam's wallet", "ring": True}))
    assert (result.ok, result.say) == (False, "Your Wallet's away from home, as of 10 minutes ago. "
                                              "I couldn't ring your Wallet.")
    # Several which fit are asked about.
    home.find_mys.append(_find_my("Wallet tag", "tag"))
    result = executor.execute(Action("find_device", {"device": "wallet", "ring": True}))
    assert (result.ok, result.say, result.private) == (False, "Which one: Wallet or Wallet tag?", True)


def test_where_is_my_wallet_is_private(caplog):
    home = _phone_both_ways()
    home.find_mys[1]["tracker"] = {"state": "Gym", "reported": time.time()}
    with caplog.at_level("DEBUG"):
        result = ActionExecutor(home=home).execute(Action("find_device", {"device": "wallet"}))
    assert (result.ok, result.say, result.private) == (True, "Your Wallet's at Gym.", True)
    assert home.pressed == []
    assert "Gym" not in caplog.text and "Said where the Sam's Wallet is" in caplog.text


def test_stop_presses_stop_sound_for_what_was_rung():
    home = _phone_both_ways()
    executor = ActionExecutor(home=home)
    executor.execute(Action("find_device", {"device": "phone", "ring": True}))
    executor.execute(Action("find_device", {"device": "wallet", "ring": True}))
    home.pressed.clear()
    # Rung at one satellite, stopped at another: the brain's record is shared.
    elsewhere = ActionExecutor(home=home, rang=executor.rang)
    result = elsewhere.execute(Action("stop_finding"))
    assert (result.ok, result.say) == (True, "Stopped.")
    assert home.pressed == ["button.phone_stop_sound", "button.wallet_stop_sound"]
    # Nothing's left ringing, so a second says so.
    assert elsewhere.execute(Action("stop_finding")).say == "I haven't rung anything lately."
    # One named is stopped whether or not Nova rang it.
    home.pressed.clear()
    assert elsewhere.execute(Action("stop_finding", {"device": "phone"})).say == "Stopped."
    assert home.pressed == ["button.phone_stop_sound"]
    # The Mac rings by notification, which can't be stopped.
    assert elsewhere.execute(Action("stop_finding", {"device": "mac"})).say == \
        "I can't stop your Mac ringing from here."
    home.press_fails = requests.ConnectionError("down")
    result = elsewhere.execute(Action("stop_finding", {"device": "wallet"}))
    assert (result.ok, result.say) == (False, "I couldn't stop your Wallet ringing.")


def test_what_was_rung_is_forgotten_after_ten_minutes():
    home = _phone_both_ways()
    executor = ActionExecutor(home=home)
    now = [1000.0]
    executor.rang._clock = lambda: now[0]
    executor.execute(Action("find_device", {"device": "phone", "ring": True}))
    now[0] += 601
    assert executor.execute(Action("stop_finding")).say == "I haven't rung anything lately."


# Sending to the Mac or the tablet - see devices.py

def _mac(notify="mobile_app_macbook_pro"):
    return _app_device("macbook-pro", "Apple", "Mac16,8", notify=notify, tracker="home")


def test_send_that_to_my_mac_goes_through_its_notify_service():
    home = FakeAppHome([_app_device(), _mac()])
    ntfy = FakeNtfy()
    executor = ActionExecutor(home=home, ntfy=ntfy)
    executor.last_answer = ("What's the recipe for pancakes?", "Mix flour, eggs and milk, then fry them.")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))

    assert (result.ok, result.confirm) == (True, "chime")
    assert home.sent == [("mobile_app_macbook_pro", "Mix flour, eggs and milk, then fry them.", None)]
    assert home.titles == ["What's the recipe for pancakes?"]
    assert ntfy.sent == []


def test_the_phone_still_goes_through_ntfy():
    home = FakeAppHome([_app_device(), _mac()])
    ntfy = FakeNtfy()
    executor = ActionExecutor(home=home, ntfy=ntfy)
    executor.last_answer = ("asked", "said")
    for params in ({}, {"device": "phone"}):
        assert executor.execute(Action("send_to_phone", params)).say == "Sent."
    assert len(ntfy.sent) == 2
    assert home.sent == []


def test_sent_is_only_said_once_home_assistant_took_it():
    failure = requests.HTTPError("500", response=SimpleNamespace(status_code=500))
    executor = ActionExecutor(home=FakeAppHome([_mac()], notify_fails=failure))
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))
    assert (result.ok, result.say) == (False, "I couldn't send that to your MacBook Pro.")

    # Unanswered, it may well have been sent.
    executor = ActionExecutor(home=FakeAppHome([_mac()], notify_fails=requests.Timeout("slow")))
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))
    assert (result.ok, result.say) == (False, "Home Assistant didn't answer in time, so I can't tell whether that "
                                              "was sent to your MacBook Pro.")


def test_a_connection_never_made_says_it_wasnt_sent():
    executor = ActionExecutor(home=FakeAppHome([_mac()], notify_fails=requests.ConnectTimeout("no connection")))
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))
    assert (result.ok, result.say) == (False, "I couldn't send that to your MacBook Pro.")


def test_a_device_with_no_notify_service_says_so():
    executor = ActionExecutor(home=FakeAppHome([_mac(notify=None)]))
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))
    assert (result.ok, result.say) == (False, "Your MacBook Pro hasn't got the Home Assistant app's notifications.")
    # Nor is there a device called that, or Home Assistant to ask.
    assert executor.execute(Action("send_to_phone", {"device": "kettle"})).say == "I haven't got a device called that."
    lonely = ActionExecutor(ntfy=FakeNtfy())
    lonely.last_answer = ("asked", "said")
    assert lonely.execute(Action("send_to_phone", {"device": "mac"})).say == "Sending to your mac needs Home Assistant."
    # Claude's "my mac" is still "your mac".
    assert lonely.execute(Action("send_to_phone", {"device": "my mac"})).say == "Sending to your mac needs Home Assistant."


@pytest.mark.parametrize("word", ["phone", "my phone", "Mobile", "the phone", "my mobile", "The Phone"])
def test_the_phone_is_ntfy_without_asking_home_assistant(word):
    class Down(FakeAppHome):
        def app_devices(self):
            raise requests.ConnectionError("down")

    # An ntfy-only brain, Home Assistant down, and a phone Home Assistant doesn't know.
    for home in (None, Down([]), FakeAppHome([_mac()])):
        ntfy = FakeNtfy()
        executor = ActionExecutor(home=home, ntfy=ntfy)
        executor.last_answer = ("asked", "said")
        assert executor.execute(Action("send_to_phone", {"device": word})).say == "Sent."
        assert len(ntfy.sent) == 1


def test_the_phones_among_those_found_go_by_ntfy_and_the_rest_by_notify():
    home = FakeAppHome([_app_device(), _mac()])
    ntfy = FakeNtfy()
    executor = ActionExecutor(home=home, ntfy=ntfy)
    executor.last_answer = ("asked", "said")
    # "s25" is the phone, "macbook" the Mac, and "pro" only the Mac.
    assert executor.execute(Action("send_to_phone", {"device": "s25"})).say == "Sent."
    assert (len(ntfy.sent), home.sent) == (1, [])
    assert executor.execute(Action("send_to_phone", {"device": "pro"})).say == "Sent."
    assert (len(ntfy.sent), len(home.sent)) == (1, 1)


@pytest.mark.parametrize("text", ["TTS", " TTS ", "request_location_update", "clear_notification", "remove_channel",
                                  "command_stop_tts", "command_screen_on", "command_launch_app",
                                  # The iPhone's, iPad's and Mac's own, the Mac being one Nova sends to.
                                  "clear_badge", "update_complications", "update_widgets", "kiosk_reload"])
def test_a_message_which_is_one_of_the_apps_commands_is_shown_not_obeyed(text):
    # The app acts on a message which is exactly one of its commands, whatever its title.
    home = FakeAppHome([_mac()])
    result = ActionExecutor(home=home).execute(Action("send_to_phone", {"device": "mac", "text": text}))
    assert (result.ok, result.say) == (True, "Sent.")
    # Shown by the character put in front, which Home Assistant's trimming leaves - and nothing else.
    assert home.sent == [("mobile_app_macbook_pro", NOT_A_COMMAND + text, None)]


def test_a_message_which_isnt_words_is_sent_as_words():
    # Claude can give a number for the text, which had no strip() to check it with.
    home = FakeAppHome([_mac()])
    result = ActionExecutor(home=home).execute(Action("send_to_phone", {"device": "mac", "text": 42}))
    assert (result.ok, result.say) == (True, "Sent.")
    assert home.sent == [("mobile_app_macbook_pro", "42", None)]


@pytest.mark.parametrize("text", ["tts", "TTS is text to speech", "Take the command_screen_on", "commands",
                                  "Mix flour, eggs and milk, then fry them."])
def test_any_other_message_is_sent_as_it_is(text):
    home = FakeAppHome([_mac()])
    ActionExecutor(home=home).execute(Action("send_to_phone", {"device": "mac", "text": text}))
    assert home.sent == [("mobile_app_macbook_pro", text, None)]


def test_a_partial_send_says_which_device_got_it():
    air = _app_device("macbook-air", "Apple", "Mac15,2", notify=None, tracker="home")
    executor = ActionExecutor(home=FakeAppHome([_mac(), air]))
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "mac"}))
    assert (result.ok, result.confirm, result.say) == (
        False, "speak", "Sent to your MacBook Pro. Your MacBook Air hasn't got the Home Assistant app's notifications.")


@pytest.mark.parametrize("ntfy, trouble", [
    (None, "Notifications aren't set up yet."),
    (FakeNtfy(phone=""), "There's nowhere set up to send things to your phone."),
])
def test_the_rest_are_sent_to_when_the_phone_among_them_cant_be(ntfy, trouble):
    # "All" finds the phone and the Mac, and the Mac doesn't need ntfy.
    home = FakeAppHome([_app_device(), _mac()])
    executor = ActionExecutor(home=home, ntfy=ntfy)
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action("send_to_phone", {"device": "all"}))
    assert home.sent == [("mobile_app_macbook_pro", "said", None)]
    assert (result.ok, result.confirm, result.say) == (
        False, "speak", f"Sent to your MacBook Pro. I couldn't send that to your phone. {trouble}")
    # Asked for the phone alone, or by its name, there is nothing else to send to.
    for asked in ("phone", "s25"):
        result = executor.execute(Action("send_to_phone", {"device": asked}))
        assert (result.ok, result.say) == (False, trouble)
    assert len(home.sent) == 1


def test_with_nothing_to_send_that_is_said_before_the_phone_cant_be_sent_to():
    home = FakeAppHome([_app_device(), _mac()])
    result = ActionExecutor(home=home).execute(Action("send_to_phone", {"device": "all"}))
    assert (result.ok, result.say, home.sent) == (False, "There's nothing to send yet.", [])


def test_send_to_phone_is_offered_with_ntfy_or_home_assistant():
    def offered(executor):
        return {s["name"]: s for s in executor.tool_schemas()}

    assert "send_to_phone" in offered(ActionExecutor(ntfy=FakeNtfy()))
    schema = offered(ActionExecutor(home=object()))["send_to_phone"]
    assert schema["description"].endswith(
        "Give device only when the user names one other than their phone, e.g. 'mac' or 'tablet'.")
    assert "send_to_phone" not in offered(ActionExecutor())
    assert ActionExecutor().execute(Action("send_to_phone")).say == "Notifications aren't set up yet."
    # With only Home Assistant, the phone has no ntfy to go by.
    assert ActionExecutor(home=FakeAppHome([])).execute(Action("send_to_phone")).say == (
        "Notifications aren't set up yet.")


def test_a_private_reply_is_kept_private():
    assert ActionResult.from_reply({"success": True, "message": "Your phone's at home.", "private": True}).private
    assert not ActionResult.from_reply({"success": True, "message": "Done."}).private


# What Claude reads of the house

def test_home_status_is_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"]: schema for schema in executor.tool_schemas()}

    schema = offered(ActionExecutor(home=object()))["home_status"]
    assert schema["description"].startswith("Read the state of things in Home Assistant")
    assert schema["input_schema"]["required"] == ["about"]
    assert "home_status" not in offered(ActionExecutor())
    assert ActionExecutor().execute(Action("home_status", {"about": "sunset"})).say == "Home Assistant isn't set up yet."
    # These are the tools whose answers go back to Claude rather than being said.
    assert ActionExecutor().read_tools() == ["downloads_waiting", "whats_new", "home_status", "get_football", "get_listening",
                                            "get_wrapped"]


def test_get_football_is_read_by_claude_and_names_the_followed_teams(monkeypatch):
    from voice_assistant import actions as actions_module

    # What it asks football is what the words are built from, so a fake stands in for that.
    monkeypatch.setattr(actions_module, "describe_football", lambda football, **asked: {
        "success": True, "message": f"asked {sorted(asked.items())}"})
    football = SimpleNamespace(names=lambda: ["Stevenage", "Liverpool", "England"])
    executor = ActionExecutor(football=football)
    schema = {tool["name"]: tool for tool in executor.tool_schemas()}["get_football"]
    assert schema["description"].endswith(" The user follows: England, Liverpool and Stevenage.")
    assert "never from memory" in schema["description"]
    result = executor.execute(Action("get_football", {"team": "Liverpool"}, from_model=True))
    assert result.ok and "('team', 'Liverpool')" in result.say
    assert "get_football" not in {tool["name"] for tool in ActionExecutor().tool_schemas()}
    assert ActionExecutor().execute(Action("get_football")).say == "Football needs Home Assistant."


def test_get_football_offers_lineups():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    description = schema["description"]
    about = schema["input_schema"]["properties"]["about"]
    assert about["enum"] == ["matches", "table", "left", "lineups", "match"]
    assert "starting eleven" in about["description"]
    assert "line-ups" in description and "starting 11" in description
    # With a team named, even one of the user's own; with none, the followed team playing now or today.
    assert ("'lineups' with the team named, even one of the user's own; with neither a team nor a competition, "
            "the followed team playing now or today") in about["description"]
    assert "ask about the user's own teams in general" in description
    assert "leave both out for the user's own teams" not in description
    # Claude is to reply with the side asked for, not to put it in the team.
    assert "Reply with the side asked for, or both" in about["description"]
    # Line-ups are only ever said from what Home Assistant gave, as the rest are.
    # So are who is left in a cup, the ground and the odds: odds from memory would be a made-up figure.
    assert ("fixtures, scores, results, what happened in a match (goals, scorers, red cards), tables, line-ups, "
            "who is left, the ground and the odds only from that, never from memory") in description
    # Neither scorers nor line-ups are what it hasn't got now: yellow cards are one thing it still hasn't.
    missing = description.split("hasn't got", 1)[1].split(".", 1)[0]
    assert "yellow cards" in missing and "scorers" not in missing and "line-ups" not in missing


def test_get_football_offers_what_happened_in_a_match():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    description = schema["description"]
    properties = schema["input_schema"]["properties"]
    assert "match" in properties["about"]["enum"]
    # "Who scored" goes to 'match': the 'matches' answer has no scorers, and the tool's words would then let Claude
    # say Home Assistant has none.
    assert ("'match' for who scored or what happened in a team's match - its goals, penalties, red cards and ESPN's notes"
            in properties["about"]["description"])
    # It is asked with the team named, even one of the user's own; with none, it is the followed team whose match is in
    # play, else over, on the day, which is the date (today when none is given), or with no date the one just played:
    # not the line-ups' way, which is the match about to start. A day asked about, last night's too, goes in date.
    assert ("'match' is asked with the team named too; with neither a team nor a competition it is the followed team "
            "whose match is in play, else the one whose match is over, on the day (today when no date is given), "
            "or with no date the one just played (last night's, until the next morning), "
            "with the day in date if one was asked about, last night's included.") in properties["about"]["description"]
    assert "asked the same way" not in properties["about"]["description"]
    assert "the day of a team's match" in properties["date"]["description"]
    # A round-up of a competition on a day is its matches with a date, not a team's match.
    assert "'what happened in the Champions League yesterday?' is a competition with a date" in description
    assert "what happened in a match" in description
    # The speech reads digits joined by a dash as "dash", whether a score or a formation.
    assert ("Say a score as 'Spain 2, England 3' or in words, and never write digits joined by a dash - a score or a "
            "formation - which is read out as 'dash'.") in description
    # What ESPN's notes don't say is not said, though a late winner is worked out from the minutes and the score.
    assert ("Say a mistake cost a game only where one of ESPN's notes says what happened; a late winner or equaliser "
            "may be worked out from the minutes and the score.") in description
    # 'Who scored' is a question of a match.
    assert "'who scored for Stevenage?'" in description
    # What round two added stays.
    assert "line-ups" in description and "never from memory" in description


def test_get_football_asks_for_a_competition_as_it_is_properly_called():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    description = schema["description"]
    # ESPN calls the EFL Cup the Carabao Cup and the European Championship "EURO": Claude is to name one as it is properly
    # called where it knows it, as Nova keeps no list of competitions to put it right.
    assert ("Name a competition as it is properly called where you know it, such as the Carabao Cup for the EFL Cup."
            in description)
    # It stays with what the tool says of the team or competition asked about, which is where a name is given.
    assert description.index("Name a competition as it is properly called") > description.index("Give the team or the competition asked about")
    assert "putting right a name that was clearly misheard" in description


def test_get_football_hands_everything_it_was_given_to_the_words(monkeypatch):
    from voice_assistant import actions as actions_module

    given = []
    monkeypatch.setattr(actions_module, "describe_football", lambda football, **asked: given.append((football, asked)) or {
        "success": False, "message": "I can't get football right now."})
    football = SimpleNamespace(names=list)
    result = ActionExecutor(football=football).execute(Action("get_football", {
        "team": "Arsenal", "competition": "Premier League", "about": "table", "date": "2026-11-24", "days": 2,
        "against": "Chelsea", "mood": "keen"}, from_model=True))
    assert (result.ok, result.say) == (False, "I can't get football right now.")
    assert given == [(football, {"team": "Arsenal", "competition": "Premier League", "about": "table",
                                 "date": "2026-11-24", "days": 2, "against": "Chelsea"})]


def test_get_football_offers_when_two_teams_last_played():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    description = schema["description"]
    against = schema["input_schema"]["properties"]["against"]
    assert against["type"] == "string"
    # The other team goes with 'match', for the last time the team played them, five seasons back.
    assert against["description"].startswith("The other team, for 'match': the last time the team played them, back five seasons.")
    assert "a date wins over it" in against["description"]
    # A competition given with it is left out, and only a match played or in play is found: one to come is the fixtures.
    assert "a competition given with it is left out" in against["description"]
    assert ("It finds only a match played or in play, never one to come: 'when do Liverpool play Newcastle next?' is the "
            "team's fixtures, not against.") in against["description"]
    # A day is looked for back through the seasons too, which is how the League Cup final of 2025 is found.
    assert "A team's match on a day is looked for back through five seasons." in schema["input_schema"]["properties"]["date"]["description"]
    # "When did Liverpool last play Newcastle?" is a team's match against another, never its matches or the team alone.
    assert "'when did Liverpool last play Newcastle?'" in description
    assert ("'When did Liverpool last play Newcastle?' is 'match' with the team Liverpool and against Newcastle, "
            "never 'matches'") in description
    # What isn't found is said as it is told, never as the two never having played.
    assert "never that they haven't played" in description
    # What rounds one to three said stays.
    assert "never from memory" in description and "'who scored for Stevenage?'" in description
    assert "against" not in schema["input_schema"]["required"]


def test_get_football_tells_claude_to_ask_which_team_was_meant_where_nearest_names_are_given():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    description = schema["description"]
    # Where the answer names the nearest names among the team's opponents, a name was probably misheard: ask, never guess.
    assert "nearest names" in description and "ask the user whether they meant" in description
    assert "'Did you mean Chorley?'" in description
    assert "never guess another team" in description
    # Never a guess where nothing is found either, and the question asked even where one name seems plainly meant.
    assert "never that they haven't played, and never guess another team yourself" in description
    assert "even where one seems plainly meant" in description and "on a yes, or the name they meant" in description
    # A yes is asked again with that name as the other team, so that the match is found; and none is not made up.
    assert "ask again with that name as against" in description
    assert "never that they haven't played" in description
    # Only the description says it: the parameters stand as they were.
    assert set(schema["input_schema"]["properties"]) == {"team", "competition", "about", "date", "days", "against"}


def test_get_football_hands_the_other_team_to_the_words_and_none_when_not_given(monkeypatch):
    from voice_assistant import actions as actions_module

    given = []
    monkeypatch.setattr(actions_module, "describe_football", lambda football, **asked: given.append(asked) or {
        "success": True, "message": "ok"})
    executor = ActionExecutor(football=SimpleNamespace(names=list))
    executor.execute(Action("get_football", {"team": "Liverpool", "about": "match", "against": "Newcastle"}, from_model=True))
    executor.execute(Action("get_football", {"team": "Liverpool", "about": "match"}, from_model=True))
    assert [asked["against"] for asked in given] == ["Newcastle", None]


def test_get_football_with_no_teams_followed_says_nothing_of_them():
    schema = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}["get_football"]
    assert "The user follows" not in schema["description"] and schema["description"].endswith("how fresh it is.")
    one = {tool["name"]: tool for tool in ActionExecutor(football=SimpleNamespace(names=lambda: ["Stevenage"])).tool_schemas()}
    assert one["get_football"]["description"].endswith(" The user follows: Stevenage.")


def test_follow_team_follows_and_unfollows():
    calls = []
    football = SimpleNamespace(
        names=list,
        follow=lambda team: calls.append(("follow", team)) or {"success": True, "message": "You're now following Arsenal."},
        unfollow=lambda team: calls.append(("unfollow", team)) or {"success": True, "message": "You've stopped following Norway."})
    executor = ActionExecutor(football=football)
    followed = executor.execute(Action("follow_team", {"team": "Arsenal"}, from_model=True))
    assert (followed.ok, followed.say) == (True, "You're now following Arsenal.")
    unfollowed = executor.execute(Action("follow_team", {"team": "Norway", "follow": False}, from_model=True))
    assert (unfollowed.ok, unfollowed.say) == (True, "You've stopped following Norway.")
    assert calls == [("follow", "Arsenal"), ("unfollow", "Norway")]
    # Leaving follow out follows; with no team, or one which is only blanks, nothing is asked of football.
    for params in ({}, {"follow": False}, {"team": "  "}, {"team": None}, {"team": 5}):
        missed = executor.execute(Action("follow_team", params, from_model=True))
        assert (missed.ok, missed.say) == (False, "I didn't catch which team.")
    assert len(calls) == 2


def test_follow_team_is_only_offered_with_football_and_isnt_read_by_claude():
    offered = {schema["name"]: schema for schema in ActionExecutor(football=SimpleNamespace(names=list)).tool_schemas()}
    assert offered["follow_team"]["description"].startswith("Follow or stop following a football team")
    assert offered["follow_team"]["input_schema"]["required"] == ["team"]
    assert set(offered["follow_team"]["input_schema"]["properties"]) == {"team", "follow"}
    assert "follow_team" not in {schema["name"] for schema in ActionExecutor().tool_schemas()}
    assert ActionExecutor().execute(Action("follow_team", {"team": "Arsenal"})).say == "Football needs Home Assistant."
    # What it did is said for Claude, so it isn't one of the tools whose answers go back to Claude.
    assert "follow_team" not in ActionExecutor().read_tools()


# Lists - Assist first, then Nova's own, through a real HomeAssistant and Lists with a fake
# in place of Home Assistant's API, so the guard and the intents asked for are the real ones.

class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeSession:
    """Stands in for Home Assistant's REST API, answering by path, or raising, and noting what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("8123", 1)[1]
        self.requests.append((path, kwargs.get("json")))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer(kwargs.get("json")) if callable(answer) else answer)


class FakeSocket:
    """Stands in for the WebSocket, answering Assist's sentence debugging with one match, or none."""

    def __init__(self, found):
        self.found = found

    def call(self, messages):
        return [{"results": [self.found]}]


def _debugged(intent, *targets):
    """Assist's sentence debugging's match, e.g. _debugged("HassListAddItem", "todo.shopping_list")."""
    return {"match": True, "intent": {"name": intent}, "targets": {target: {"matched": True} for target in targets},
            "source": "builtin"}


@pytest.fixture
def make_lists_executor(tmp_path, monkeypatch):
    from voice_assistant import network
    from voice_assistant.choices import ChoiceStore
    from voice_assistant.ha_registry import Entry, Snapshot
    from voice_assistant.home_assistant import HomeAssistant
    from voice_assistant.lists import Lists

    monkeypatch.setattr(network, "online", lambda: True)

    def factory(found, answers=None, items=("milk",), lists=None):
        # Each list's things, by its entity id - the shopping list's alone, unless a test says.
        lists = lists or {"todo.shopping_list": ("Shopping list", items)}
        snapshot = Snapshot(entries={entity: Entry(entity, "local_todo", None, None, entity, None, (), True)
                                     for entity in lists}, devices={}, areas={}, read_at=0.0)
        home = HomeAssistant("http://homeassistant.local:8123/", "token",
                             registry=type("Registry", (), {"snapshot": lambda self: snapshot})())
        home._session = FakeSession({
            "/api/states": [{"entity_id": entity, "state": str(len(things)), "attributes": {"friendly_name": name}}
                            for entity, (name, things) in lists.items()],
            "/api/conversation/process": {"response": {"response_type": "action_done",
                                                       "speech": {"plain": {"speech": "Added milk"}}}},
            "/api/services/todo/add_item": [],
            "/api/services/todo/update_item": [],
            "/api/services/todo/get_items?return_response": lambda asked: {"service_response": {asked["entity_id"]: {
                "items": [{"summary": item, "uid": f"uid-{item}", "status": "needs_action"}
                          for item in lists[asked["entity_id"]][1]]}}},
            **(answers or {})})
        home.socket = FakeSocket(found)
        return ActionExecutor(home=home, lists=Lists(home, ChoiceStore(str(tmp_path / "lists.json"), {"main": None}))), \
            home._session.requests
    return factory


def _sent(requests_made):
    """The paths sent to, bar the reads before Assist is asked."""
    return [path for path, _ in requests_made if path != "/api/states"]


def test_assist_adds_it_first_then_nova_does(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                     "command": "add milk to my shopping list"}))
    # Said, as adding to a list doesn't show itself the way a light does.
    assert (result.ok, result.say, result.confirm) == (True, "Added milk", "speak")
    assert _sent(sent) == ["/api/conversation/process"]

    # "Add milk to notes": Assist would add it with no list, which isn't sent.
    executor, sent = make_lists_executor(_debugged("HassListAddItem"))
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "shopping",
                                                     "command": "add milk to shopping"}))
    assert (result.ok, result.say) == (True, "Added milk to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]
    assert sent[-1][1] == {"entity_id": "todo.shopping_list", "item": "milk"}

    # The offline fallback gives no request for Assist, so Nova adds it straight away.
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "eggs milk and bread", "list": "shopping"}))
    assert (result.ok, result.say) == (True, "Added eggs milk and bread to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]


def test_a_list_sentence_the_guard_refuses_is_still_added_by_nova(make_lists_executor):
    # Its words say it would lock something, but Nova's lists only call todo services.
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "a door lock", "list": "my shopping list",
                                                     "command": "add a door lock to my shopping list"}))
    assert (result.ok, result.say) == (True, "Added a door lock to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]


def test_a_sentence_assist_would_read_as_removing_is_never_sent(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListRemoveItem", "todo.shopping_list"))
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "take milk off my shopping list"}))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    assert "/api/conversation/process" not in _sent(sent)
    assert sent[-1] == ("/api/services/todo/update_item",
                        {"entity_id": "todo.shopping_list", "item": "uid-milk", "status": "completed"})


def test_ticking_off_goes_to_assist_only_as_completing(make_lists_executor):
    completed = {"response": {"response_type": "action_done", "speech": {"plain": {"speech": "Completed milk"}}}}
    executor, sent = make_lists_executor(_debugged("HassListCompleteItem", "todo.shopping_list"),
                                         {"/api/conversation/process": completed})
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "complete milk on my shopping list"}))
    assert (result.ok, result.say) == (True, "Completed milk")
    assert _sent(sent) == ["/api/conversation/process"]
    # "Tick off milk on my shopping list" is HassTurnOn to Assist, with nothing to act on.
    executor, sent = make_lists_executor(_debugged("HassTurnOn"))
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "tick off milk on my shopping list"}))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    assert "/api/conversation/process" not in _sent(sent)


def test_a_list_is_read_by_nova_never_assist(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"), items=("milk", "eggs"))
    result = executor.execute(Action("read_list", {"list": "my shopping list"}))
    assert (result.ok, result.say) == (True, "Your shopping list has milk and eggs.")
    assert "/api/conversation/process" not in _sent(sent)


@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.Timeout("no answer")])
def test_what_names_no_list_goes_to_claude_with_home_assistant_down(make_lists_executor, failure):
    # "I've got a headache" is ticking something off to the local rule - but a sentence
    # about the world mustn't end at "I can't reach Home Assistant".
    executor, sent = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action("tick_off_list", {"item": "a headache", "list": None,
                                                       "command": "i've got a headache"}))
    assert (result.ok, result.understood) == (False, False)
    assert "/api/conversation/process" not in _sent(sent)
    # Nor does it with Home Assistant up and nothing on any list that fits.
    executor, _ = make_lists_executor(None)
    result = executor.execute(Action("tick_off_list", {"item": "a headache", "list": None,
                                                       "command": "i've got a headache"}))
    assert (result.ok, result.understood) == (False, False)
    # A list named is said to be out of reach.
    executor, _ = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "tick off milk on my shopping list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't reach Home Assistant right now.")


def test_claude_adds_several_things_one_by_one(make_lists_executor):
    executor, sent = make_lists_executor(None)
    result = executor.execute(Action("manage_list", {"action": "add", "items": ["eggs", "milk", "bread"],
                                                     "list": "shopping"}, from_model=True))
    assert (result.ok, result.say) == (True, "Added eggs, milk and bread to your shopping list.")
    assert [sent_json["item"] for path, sent_json in sent if path == "/api/services/todo/add_item"] == [
        "eggs", "milk", "bread"]
    assert "/api/conversation/process" not in _sent(sent)


def test_claude_reads_and_ticks_off_one_thing(make_lists_executor):
    executor, _ = make_lists_executor(None)
    result = executor.execute(Action("manage_list", {"action": "read"}, from_model=True))
    assert (result.ok, result.say) == (True, "Your shopping list has milk.")
    result = executor.execute(Action("manage_list", {"action": "tick_off", "items": ["milk"]}, from_model=True))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    for wrong in ({"action": "tick_off"}, {"action": "delete", "items": ["milk"]}, {"action": "add", "items": "milk"}):
        assert executor.execute(Action("manage_list", wrong, from_model=True)).ok is False


def test_lists_are_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"]: schema for schema in executor.tool_schemas()}

    schema = offered(ActionExecutor(lists=object()))["manage_list"]
    assert schema["description"].startswith("Add things to, read, or tick things off one of the user's to-do lists")
    assert schema["input_schema"]["required"] == ["action"]
    assert not {"add_to_list", "read_list", "tick_off_list"} & set(offered(ActionExecutor(lists=object())))
    assert "manage_list" not in offered(ActionExecutor())
    for tool_name in ("manage_list", "add_to_list", "read_list", "tick_off_list"):
        assert ActionExecutor().execute(Action(tool_name, {"action": "read"})).say == "Lists need Home Assistant."
    # Claude can't run the local ones, which pass free text to Assist.
    result = ActionExecutor(lists=object()).execute(Action("add_to_list", {"item": "milk"}, from_model=True))
    assert result.ok is False


def _ticked(sent):
    return [sent_json for path, sent_json in sent if path == "/api/services/todo/update_item"]


@pytest.mark.parametrize("said, on_list", [("flu", "Flu jab"), ("car", "Wash the car"), ("time", "Time sheet")])
def test_ive_got_ticks_off_only_an_exact_fit(make_lists_executor, said, on_list):
    # "I've got the flu" is a sentence about the world, never "Flu jab" ticked off - it's Claude's.
    executor, sent = make_lists_executor(None, items=(on_list,))
    result = executor.execute(Action("tick_off_list", {"item": said, "list": None, "exact": True}))
    assert (result.ok, result.understood) == (False, False)
    assert _ticked(sent) == [] and "/api/conversation/process" not in _sent(sent)
    # The thing itself is ticked off.
    executor, sent = make_lists_executor(None, items=(on_list, said))
    result = executor.execute(Action("tick_off_list", {"item": said, "list": None, "exact": True}))
    assert (result.ok, result.say) == (True, f"Ticked off {said} on your shopping list.")


@pytest.mark.parametrize("text, lists, ticked", [
    # "Take the day off work" is about the world, never "Day rota" ticked off by its one word.
    ("Take the day off work.", {"todo.work": ("Work", ("Day rota",))}, []),
    ("Cross the day off work.", {"todo.work": ("Work", ("Day rota",))}, []),
    # The word "list" says it's the list, so "milk" is still the oat milk.
    ("Take the milk off my shopping list.", {"todo.shopping_list": ("Shopping list", ("oat milk",))},
     [{"entity_id": "todo.shopping_list", "item": "uid-oat milk", "status": "completed"}]),
])
def test_taking_off_a_list_not_named_as_one_ticks_off_only_an_exact_fit(make_lists_executor, text, lists, ticked):
    from voice_assistant.intents import LocalIntents

    executor, sent = make_lists_executor(None, lists=lists)
    actions = LocalIntents(home=executor.home).match(text)["actions"]
    # Assist is still asked first.
    assert actions[0].params["command"] == text.lower().rstrip(".")
    result = executor.execute(actions[0])
    assert result.understood == bool(ticked) and _ticked(sent) == ticked


def test_a_list_named_without_the_word_list_and_nothing_fitting_goes_to_claude(make_lists_executor):
    executor, sent = make_lists_executor(None, lists={"todo.work": ("Work", ("Send the report",))})
    result = executor.execute(Action("tick_off_list", {"item": "friday", "list": "work",
                                                       "command": "take friday off work"}))
    assert (result.ok, result.understood) == (False, False) and _ticked(sent) == []
    # Said, when the words say it's a list.
    result = executor.execute(Action("tick_off_list", {"item": "friday", "list": "my work list",
                                                       "command": "take friday off my work list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't see friday on your work list.")


@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.Timeout("no answer")])
@pytest.mark.parametrize("tool, params", [
    ("add_to_list", {"item": "drake", "list": "spotify", "command": "put drake on spotify"}),
    ("read_list", {"list": "netflix"}),
    ("tick_off_list", {"item": "friday", "list": "work", "command": "take friday off work"}),
])
def test_words_naming_no_list_go_to_claude_with_home_assistant_down(make_lists_executor, failure, tool, params):
    executor, sent = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action(tool, params))
    assert (result.ok, result.understood) == (False, False)
    # Words saying it's a list are told it can't be reached.
    executor, _ = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action(tool, {**params, "list": "my shopping list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't reach Home Assistant right now.")


def test_offline_a_list_that_isnt_there_says_claude_cant_be_reached(make_lists_executor):
    # The fallback's "add a reminder to call mum" isn't a list, and the offline message
    # (the model's reply) is said rather than "I haven't got a list called that.".
    executor, _ = make_lists_executor(None)
    results = [executor.execute(Action("add_to_list", {"item": "a reminder", "list": "call mum"}))]
    assert confirm(results, fallback="Sorry, I can't reach the internet right now.")[1] == [
        "Sorry, I can't reach the internet right now."]


def test_an_assist_error_after_sending_is_said_not_done_again(make_lists_executor):
    failed = {"response": {"response_type": "error", "data": {"code": "failed_to_handle"},
                           "speech": {"plain": {"speech": "An unexpected error occurred"}}}}
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"),
                                         {"/api/conversation/process": failed})
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                     "command": "add milk to my shopping list"}))
    assert (result.ok, result.say) == (False, "An unexpected error occurred")
    assert _sent(sent) == ["/api/conversation/process"]


# The PC - see pc.py

class FakePC:
    """Stands in for PCPower, noting what it's asked and confirming with the chime."""

    def __init__(self):
        self.asked = []

    def act(self, action, name=None, defer=False):
        self.asked.append(("act", action, name))
        return {"success": True, "message": "Locking your PC.", "chime": True}


def test_the_pcs_power_words_are_deferred_only_at_a_pc():
    pc = FakePC()
    pc.act = lambda action, name=None, defer=False: {"success": True, "message": "Locking your PC.",
                                                    "after_said": (lambda: {"success": True}) if defer else None}
    assert ActionExecutor(pc=pc).execute(Action("pc_power", {"action": "lock"})).after_said is None
    result = ActionExecutor(pc=pc, at_pc=lambda: True).execute(Action("pc_power", {"action": "lock"}))
    assert (result.say, result.after_said()) == ("Locking your PC.", {"success": True})


def test_a_power_word_at_one_of_two_pcs_is_that_ones():
    pc = FakePC()
    at_desk = ActionExecutor(pc=pc, at_pc=lambda: True, pc_name=lambda: "Gaming PC")
    at_desk.execute(Action("pc_power", {"action": "lock"}))
    # One named is still the one named; and at the Pi none is chosen for it.
    at_desk.execute(Action("pc_power", {"action": "lock", "name": "Desk PC"}))
    ActionExecutor(pc=pc, pc_name=lambda: "Gaming PC").execute(Action("pc_power", {"action": "lock"}))
    assert pc.asked == [("act", "lock", "Gaming PC"), ("act", "lock", "Desk PC"), ("act", "lock", None)]
    # Shutting down is the same: that PC's, with no question.
    result = at_desk.execute(Action("pc_power", {"action": "off"}))
    assert (result.ask_first, result.confirm) == (None, "chime")
    assert pc.asked[-1] == ("act", "off", "Gaming PC")


def test_pc_power_never_asks_and_press_pc_button_is_gone():
    assert "press_pc_button" not in ActionExecutor.tools()
    pc = FakePC()
    executor = ActionExecutor(pc=pc)
    assert "press_pc_button" not in {schema["name"] for schema in executor.tool_schemas()}
    assert executor.execute(Action("press_pc_button", {"action": "off"})).ok is False
    result = executor.execute(Action("pc_power", {"action": "off"}, from_model=True))
    assert (result.ok, result.ask_first, result.confirm) == (True, None, "chime")
    assert pc.asked == [("act", "off", None)]


def test_the_pc_needs_home_assistant():
    executor = ActionExecutor()
    assert "pc_power" not in {schema["name"] for schema in executor.tool_schemas()}
    assert executor.execute(Action("pc_power", {"action": "on"})).say == "Your PC needs Home Assistant."


# Downloads - see downloads.py

class FakeNavidromePlayer:
    """Stands in for NavidromePlayer, answering every play with the reply given - by default, that it hasn't got it."""

    name = "Navidrome"
    needs_internet = False

    def __init__(self, missing=True, reply=None):
        self.missing = missing
        self.reply = reply
        self.played = []

    def play(self, query=None, search_type=None):
        self.played.append((query, search_type))
        if self.reply is not None:
            return dict(self.reply)
        return {"success": False, "message": f"I couldn't find {query} on Navidrome.", "missing": self.missing}


class EmptyLibrary:
    """Stands in for Navidrome's library, with nothing in it."""

    def find_songs(self, title, artist, also=None):
        return []

    def find_album_tracks(self, title, artist):
        return []


@pytest.fixture
def downloads(make_config, monkeypatch):
    """
    The real Downloads, set up with made-up settings and Navidrome empty, whose watches
    are only noted - as (Wanted, satellite) in its started - so SoulSync is never asked.
    """
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(music={"offer_downloads": True}, soulsync={"url": "http://soulsync.local:8008"},
                         navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    made = Downloads(config, lambda satellite, message: None, soulsync=lambda: None, library=EmptyLibrary)
    made.started = []
    made.start = lambda wanted, satellite, unless_large=False: made.started.append((wanted, satellite)) or True
    return made


GOOBA_PLAYED = Action("play_music", {"query": "Gooba by 6ix9ine", "search_type": "track"})


def test_play_offers_a_download_for_something_navidrome_hasnt_got(make_executor, downloads):
    player = FakeNavidromePlayer(missing=True)
    result = make_executor(player, downloads=downloads).execute(GOOBA_PLAYED)

    assert (result.ok, result.say) == (True, "I haven't got Gooba by 6ix9ine. Do you want me to download it?")
    assert result.ask_first == Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})
    assert player.played == [("Gooba by 6ix9ine", "track")] and downloads.started == []


def test_play_for_something_being_fetched_says_so(make_executor, downloads):
    # Waiting, as start() notes a request, without a watch to run.
    wanted = Wanted("Gooba", "6ix9ine", "track")
    downloads._waiting[wanted.key] = wanted
    player = FakeNavidromePlayer(missing=True)

    result = make_executor(player, downloads=downloads).execute(GOOBA_PLAYED)

    assert (result.ok, result.say, result.ask_first) == (True, "I'm already getting Gooba by 6ix9ine.", None)
    assert downloads.started == []

    # The same with the offer switched off on the page, as it's being fetched all the same.
    downloads.config.apply({("music", "offer_downloads"): False})
    result = make_executor(player, downloads=downloads).execute(GOOBA_PLAYED)
    assert (result.say, result.ask_first) == ("I'm already getting Gooba by 6ix9ine.", None)
    assert downloads.started == []


@pytest.mark.parametrize("player, said", [
    (FakeNavidromePlayer(missing=False), "I couldn't find Gooba by 6ix9ine on Navidrome."),
    # Navidrome couldn't be asked, which isn't the same as it not having it.
    (FakeNavidromePlayer(reply={"success": False, "message": "I can't reach Navidrome right now."}),
     "I can't reach Navidrome right now."),
])
def test_play_offers_nothing_unless_navidrome_hasnt_got_it(make_executor, downloads, player, said):
    result = make_executor(player, downloads=downloads).execute(GOOBA_PLAYED)
    assert (result.ok, result.say, result.ask_first) == (False, said, None)


def test_play_says_what_it_says_today_with_no_offer(make_executor, downloads):
    # Without downloads at all...
    result = make_executor(FakeNavidromePlayer()).execute(GOOBA_PLAYED)
    assert (result.say, result.ask_first) == ("I couldn't find Gooba by 6ix9ine on Navidrome.", None)

    # ...or with offer() giving none: here, the offer switched off on the page.
    downloads.config.apply({("music", "offer_downloads"): False})
    result = make_executor(FakeNavidromePlayer(), downloads=downloads).execute(GOOBA_PLAYED)
    assert (result.say, result.ask_first) == ("I couldn't find Gooba by 6ix9ine on Navidrome.", None)


def test_downloading_needs_downloads(make_executor):
    executor = make_executor()
    offered = {schema["name"] for schema in executor.tool_schemas()}
    assert "download_music" not in offered and "downloads_waiting" not in offered
    for tool_name in ("download_music", "request_download", "offer_download", "downloads_waiting"):
        result = executor.execute(Action(tool_name, {"query": "Gooba"}))
        assert (result.ok, result.say) == (False, "Downloading music isn't set up.")


def test_a_download_is_asked_for_from_the_satellite_and_said_later(make_executor, downloads):
    executor = make_executor(downloads=downloads, satellite="kitchen")

    result = executor.execute(Action("download_music", {"query": "Rumours by Fleetwood Mac", "search_type": "album"},
                                     from_model=True))

    assert (result.ok, result.say, result.confirm) == (
        True, f"Asked SoulSync for Rumours by Fleetwood Mac. {ON_ITS_WAY['album']}", "later")
    assert downloads.started == [(Wanted("Rumours", "Fleetwood Mac", "album"), "kitchen")]
    # Nothing said now, nor the model's "Okay." in its place: "Rumours by Fleetwood Mac requested." comes later.
    assert confirm([result], fallback="Okay.") == ([], [])


def test_only_the_users_yes_can_request_without_checking(make_executor, downloads):
    executor = make_executor(downloads=downloads, satellite="kitchen")
    names_offered = {schema["name"] for schema in executor.tool_schemas()}
    assert "download_music" in names_offered and "request_download" not in names_offered

    result = executor.execute(Action("request_download", {"query": "Gooba"}, from_model=True))
    assert (result.ok, result.say) == (False, "Sorry, I can't do that.")
    assert downloads.started == []

    # The yes, which only the conversation gives.
    result = executor.execute(Action("request_download", {"query": "Gooba", "search_type": "track"}))
    assert (result.ok, result.say, result.confirm) == (
        True, f"Asked SoulSync for Gooba. {ON_ITS_WAY['track']}", "later")
    assert downloads.started == [(Wanted("Gooba", None, "track"), "kitchen")]


def test_a_download_claude_wasnt_asked_for_in_words_is_only_offered(make_executor, downloads):
    executor = make_executor(downloads=downloads, satellite="kitchen")
    assert "offer_download" not in {schema["name"] for schema in executor.tool_schemas()}

    # Nova's own, in place of Claude's download_music - see LLMHandler.process_query.
    result = executor.execute(Action("offer_download", {"query": "Gooba by 6ix9ine", "search_type": "track"}))

    assert (result.ok, result.say) == (True, "I haven't got Gooba by 6ix9ine. Do you want me to download it?")
    assert result.ask_first == Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})
    assert downloads.started == []


def test_claude_is_told_how_to_ask_for_a_download(make_executor, downloads):
    schema = {schema["name"]: schema for schema in make_executor(downloads=downloads).tool_schemas()}["download_music"]
    assert schema["description"].startswith("Download a song or an album the user hasn't got yet")
    # Told it only asks, so it never says a download has finished which has only been asked for.
    assert "It only asks for it" in schema["description"] and "never say" in schema["description"]
    assert schema["input_schema"]["properties"]["search_type"]["enum"] == ["track", "album", "artist"]
    assert schema["input_schema"]["required"] == []


def test_claude_can_read_whats_downloading(make_executor, downloads):
    executor = make_executor(downloads=downloads)
    schema = {schema["name"]: schema for schema in executor.tool_schemas()}["downloads_waiting"]
    assert schema["description"].startswith("Read which downloads the user asked for are still on their way")
    # Read for Claude to answer from, never said as it is.
    assert "downloads_waiting" in executor.read_tools()

    result = executor.execute(Action("downloads_waiting", from_model=True))
    assert (result.ok, result.say) == (True, "Nothing is downloading.")

    # Waiting, as start() notes a request, without a watch to run.
    wanted = Wanted("Gooba", "6ix9ine", "track")
    downloads._waiting[wanted.key] = wanted
    downloads._asked_at[wanted.key] = time.monotonic() - 130
    result = executor.execute(Action("downloads_waiting", from_model=True))
    assert (result.ok, result.say) == (True, "Gooba by 6ix9ine, asked for 2 minutes ago.")


def test_a_result_said_later_is_neither_said_nor_chimed_now():
    later = ActionResult.from_reply({"success": True, "message": "Asked SoulSync for Gooba.", "later": True})
    assert later.confirm == "later"
    assert confirm([later]) == ([], [])
    assert confirm([later], chime_enabled=False, fallback="Okay.") == ([], [])
    # Beside something which is said, only that is.
    timer = ActionResult(True, "Timer set for 10 minutes.")
    assert confirm([later, timer], fallback="Okay.") == ([], ["Timer set for 10 minutes."])
    # A failure is still said.
    failed = ActionResult.from_reply({"success": False, "message": "SoulSync isn't answering.", "later": True})
    assert confirm([failed]) == ([], ["SoulSync isn't answering."])


def test_tool_listed_only_with_listening():
    offered = {tool["name"]: tool for tool in ActionExecutor(listening=object()).tool_schemas()}
    schema = offered["get_listening"]
    description = schema["description"]
    # It says the windows roll, and that today and a calendar month can't be answered.
    assert "rolling" in description and "last 7 days" in description
    assert "today" in description and "calendar month" in description
    assert schema["input_schema"]["properties"]["period"]["enum"] == ["week", "month", "year", "all"]
    assert schema["input_schema"]["properties"]["about"]["enum"] == ["summary", "artists", "songs", "albums", "genres", "when"]
    assert schema["input_schema"]["required"] == []
    assert "get_listening" not in {tool["name"] for tool in ActionExecutor().tool_schemas()}
    assert ActionExecutor().execute(Action("get_listening")).say == "Listening stats need SoulSync."


def test_get_listening_hands_what_it_was_given_to_the_words(monkeypatch):
    from voice_assistant import actions as actions_module

    given = []
    monkeypatch.setattr(actions_module, "describe_listening", lambda listening, period, about: given.append(
        (listening, period, about)) or {"success": True, "message": "A week of facts."})
    listening = object()
    executor = ActionExecutor(listening=listening)
    result = executor.execute(Action("get_listening", {"period": "month", "about": "songs", "stray": 1}, from_model=True))
    assert (result.ok, result.say) == (True, "A week of facts.")
    executor.execute(Action("get_listening", {}, from_model=True))
    assert given == [(listening, "month", "songs"), (listening, None, None)]


# New music - see navidrome.NavidromeLibrary.newest_albums

class FakePlaylistPlayer:
    """Stands in for NavidromePlayer's play_playlist, with the playlists it's given - or whatever is said of one."""

    name = "Navidrome"
    needs_internet = False

    def __init__(self, playlists=(), reply=None):
        self.playlists = list(playlists)
        self.reply = reply
        self.played = []

    def play_playlist(self, name, exact=False, playlist_id=None):
        assert exact is True
        self.played.append(name if playlist_id is None else (name, playlist_id))
        if self.reply is not None:
            return dict(self.reply)
        if name in self.playlists:
            return {"success": True, "message": f"Playing {name}"}
        return {"success": False, "missing": True, "message": f"I couldn't find a playlist called {name}."}


FIND_NEW = Action("find_new_music", from_model=True)


def test_find_new_music_plays_setting(make_executor, make_config):
    config = make_config(new_music={"playlist": "Discovery Shuffle"})
    player = FakePlaylistPlayer(["Discovery Shuffle", "Fresh Finds"])
    executor = make_executor(player, config=config)
    result = executor.execute(FIND_NEW)
    assert (result.ok, result.say) == (True, "Playing Discovery Shuffle")
    # The setting is read at each call, so a change on the settings page is used straight away.
    config.apply({("new_music", "playlist"): "Fresh Finds"})
    assert executor.execute(FIND_NEW).say == "Playing Fresh Finds"
    assert player.played == ["Discovery Shuffle", "Fresh Finds"]


class FakeNewMusic:
    """Stands in for NewMusicPlaylist's record of the playlist Nova made, by the name it was made under."""

    def __init__(self, made):
        self.made = made

    def playlist_id(self, name):
        return self.made.get(name)


def test_find_new_music_plays_the_playlist_nova_made_by_its_id(make_executor, make_config):
    config = make_config(new_music={"playlist": "New Music"})
    player = FakePlaylistPlayer(["New Music"])
    executor = make_executor(player, config=config, new_music=FakeNewMusic({"New Music": "pl7"}))
    assert executor.execute(FIND_NEW).ok is True
    # Under another name there is no record yet, so the exact name is asked for, as before.
    config.apply({("new_music", "playlist"): "Fresh Finds"})
    executor.execute(FIND_NEW)
    assert player.played == [("New Music", "pl7"), "Fresh Finds"]


def test_find_new_music_with_the_recorded_playlist_gone_says_it_isnt_made(make_executor, make_config):
    config = make_config(new_music={"playlist": "New Music"})
    # The player says it is missing as Navidrome's does, and an own playlist of the name isn't looked for.
    player = FakePlaylistPlayer(reply={"success": False, "missing": True, "message": "I couldn't find it."})
    result = make_executor(player, config=config, new_music=FakeNewMusic({"New Music": "pl7"})).execute(FIND_NEW)
    assert (result.ok, result.say) == (
        False, "Your new-music playlist isn't made yet; Nova makes it each morning from what's arrived.")
    assert player.played == [("New Music", "pl7")]


def test_find_new_music_is_offered_with_the_music_and_not_without(make_executor, make_config):
    config = make_config(new_music={"playlist": "Discovery Shuffle"})
    offered = {schema["name"]: schema for schema in make_executor(FakePlaylistPlayer(), config=config).tool_schemas()}
    assert offered["find_new_music"]["input_schema"]["required"] == []
    assert "something new" in offered["find_new_music"]["description"]
    assert "find_new_music" not in {schema["name"] for schema in make_executor(config=config).tool_schemas()}
    assert make_executor(config=config).execute(FIND_NEW).say == "The music isn't available right now."


def test_new_music_playlist_missing(make_executor, make_config):
    config = make_config(new_music={"playlist": "Discovery Shuffle"})
    result = make_executor(FakePlaylistPlayer(), config=config).execute(FIND_NEW)
    assert (result.ok, result.say) == (
        False, "Your new-music playlist isn't made yet; Nova makes it each morning from what's arrived.")


def test_new_music_playlist_not_found_is_only_said_when_navidrome_answered(make_executor, make_config):
    # A Navidrome which couldn't be asked says so itself, rather than that the playlist isn't there.
    config = make_config(new_music={"playlist": "Discovery Shuffle"})
    player = FakePlaylistPlayer(reply={"success": False, "message": "I can't reach Navidrome right now."})
    assert make_executor(player, config=config).execute(FIND_NEW).say == "I can't reach Navidrome right now."


def test_find_new_music_spotify_player(make_executor, make_config):
    config = make_config(new_music={"playlist": "Discovery Shuffle"})
    spotify = FakeSpotify(playing=False)
    spotify.play_playlist = lambda name: spotify.calls.append(("play_playlist", name))
    result = make_executor(spotify, config=config).execute(FIND_NEW)
    assert result.ok is False
    assert result.say == "New music plays from Navidrome, and Spotify is your music player right now."
    assert spotify.calls == []


def test_find_new_music_with_no_playlist_set(make_executor, make_config):
    player = FakePlaylistPlayer(["Discovery Shuffle"])
    for config in (make_config(new_music={"playlist": "  "}), make_config(), None):
        result = make_executor(player, config=config).execute(FIND_NEW)
        assert (result.ok, result.say) == (False, "No new-music playlist is set. It goes in Settings, under Music.")
    assert player.played == []


class FakeNewestLibrary:
    """Stands in for NavidromeLibrary's newest_albums, noting the time it was asked from."""

    def __init__(self, albums=(), error=None, whole=True):
        self.albums = list(albums)
        self.error = error
        self.whole = whole
        self.asked = []

    def newest_albums(self, since):
        self.asked.append(since)
        if self.error:
            raise self.error
        return list(self.albums), self.whole


def _whats_new(make_executor, library, days=None, now=1_800_000_000.0, monkeypatch=None):
    from voice_assistant import actions as actions_module
    monkeypatch.setattr(actions_module.time, "time", lambda: now)
    executor = make_executor(downloads=SimpleNamespace(library=lambda: library))
    return executor.execute(Action("whats_new", {} if days is None else {"days": days}, from_model=True))


def album(artist, songs, name="An Album"):
    return {"name": name, "artist": artist, "songCount": songs}


def test_whats_new_counts_and_names(make_executor, monkeypatch):
    library = FakeNewestLibrary([album("Band A", 10), album("Band B", 4), album("Band A", 8), album("Band C", 12),
                                 album("Band D", 1), album("Band E", 2), album("Band F", 3)])
    result = _whats_new(make_executor, library, monkeypatch=monkeypatch)
    assert result.ok
    # 7 albums, 40 songs, and the five artists with the most songs, most first.
    assert result.say == ("7 albums and 40 songs have come into your library in the last 7 days. "
                          "Most songs: Band A, Band C, Band B, Band F and Band E.")
    # The last seven days, as seconds before now.
    assert library.asked == [1_800_000_000.0 - 7 * 86400]


def test_whats_new_says_one_album_in_the_singular(make_executor, monkeypatch):
    result = _whats_new(make_executor, FakeNewestLibrary([album("Band A", 1)]), days=1, monkeypatch=monkeypatch)
    # Two things counted together have come, however few of each.
    assert result.say == "1 album and 1 song have come into your library in the last day. Most songs: Band A."


def test_whats_new_says_at_least_when_there_were_more_than_were_read(make_executor, monkeypatch):
    library = FakeNewestLibrary([album("Band A", 10), album("Band B", 4)], whole=False)
    result = _whats_new(make_executor, library, days=31, monkeypatch=monkeypatch)
    assert result.say == ("At least 2 albums and 14 songs have come into your library in the last 31 days. "
                          "Most songs: Band A and Band B.")


def test_whats_new_none(make_executor, monkeypatch):
    result = _whats_new(make_executor, FakeNewestLibrary(), monkeypatch=monkeypatch)
    assert (result.ok, result.say) == (True, "Nothing new has come into your library in the last 7 days.")


@pytest.mark.parametrize("asked, used", [(1, 1), (31, 31), (0, 1), (-3, 1), (45, 31), ("3", 3), ("lots", 7), (None, 7),
                                         (2.0, 2)])
def test_whats_new_days_bounds(make_executor, monkeypatch, asked, used):
    library = FakeNewestLibrary()
    executor_days = {} if asked is None else {"days": asked}
    from voice_assistant import actions as actions_module
    monkeypatch.setattr(actions_module.time, "time", lambda: 1_800_000_000.0)
    make_executor(downloads=SimpleNamespace(library=lambda: library)).execute(
        Action("whats_new", executor_days, from_model=True))
    assert library.asked == [1_800_000_000.0 - used * 86400]


def test_whats_new_is_read_by_claude_and_needs_downloads(make_executor):
    executor = make_executor(downloads=SimpleNamespace(library=lambda: None))
    schema = {schema["name"]: schema for schema in executor.tool_schemas()}["whats_new"]
    assert "whats_new" in executor.read_tools()
    assert "this week" in schema["description"]
    assert schema["input_schema"]["required"] == []
    assert "whats_new" not in {schema["name"] for schema in make_executor().tool_schemas()}
    assert make_executor().execute(Action("whats_new")).say == "Downloading music isn't set up."


def test_whats_new_when_navidrome_isnt_set_up_or_cant_be_asked(make_executor, monkeypatch):
    result = _whats_new(make_executor, None, monkeypatch=monkeypatch)
    assert (result.ok, result.say) == (False, "Navidrome isn't set up yet.")
    result = _whats_new(make_executor, FakeNewestLibrary(error=requests.ConnectionError("http://nd/rest/x?t=abc123token")),
                        monkeypatch=monkeypatch)
    assert (result.ok, result.say) == (False, "I can't reach Navidrome right now.")
    assert "abc123token" not in result.say


# Journeys

class FakeJourneys:
    """Journeys as far as the tools go: what each was asked, and an answer which offers a reminder."""

    def __init__(self):
        self.asked, self.reminded = [], []

    def ask(self, place, mode="driving", leave=False, arrive_at=None, satellite=None, now=None, day=None):
        self.asked.append((place, mode, leave, arrive_at, satellite, day))
        return {"success": True, "message": "The drive to Work is 24 minutes. Want a reminder?",
                "ask_first": Action("set_leave_reminder", {"place": place})}

    def remind(self, place, destination, origin, mode, arrive, leave_by, watch, satellite=None, now=None):
        self.reminded.append((place, destination, origin, mode, arrive, leave_by, watch, satellite))
        return {"success": True, "message": "I'll remind you at 8:15 AM."}


def test_the_journey_tool_asks_for_the_satellite_it_was_said_on():
    journeys = FakeJourneys()
    executor = ActionExecutor(journeys=journeys, satellite="kitchen")
    result = executor.execute(Action("journey", {"place": "work", "leave": True, "arrive_at": "09:00"},
                                     from_model=True))
    assert journeys.asked == [("work", "driving", True, "09:00", "kitchen", None)]
    assert result.ok and result.say.endswith("Want a reminder?")
    # Left out, a journey is a drive and only how long it takes.
    executor.execute(Action("journey", {"place": "the station", "mode": "walking"}, from_model=True))
    assert journeys.asked[1] == ("the station", "walking", False, None, "kitchen", None)
    # "What time do I leave for work tomorrow?" - the day, as Claude works it out from today's date.
    executor.execute(Action("journey", {"place": "work", "leave": True, "day": "2026-11-19"}, from_model=True))
    assert journeys.asked[2] == ("work", "driving", True, None, "kitchen", "2026-11-19")
    executor.execute(Action("journey", {"place": "work", "leave": True, "day": ""}, from_model=True))
    assert journeys.asked[3][5] is None


def test_the_journey_tool_is_described_for_claude():
    schema = {tool["name"]: tool for tool in ActionExecutor(journeys=FakeJourneys()).tool_schemas()}["journey"]
    assert schema["input_schema"]["required"] == ["place"]
    properties = schema["input_schema"]["properties"]
    assert properties["mode"]["enum"] == ["driving", "walking", "cycling", "transit"]
    assert set(properties) == {"place", "mode", "leave", "arrive_at", "day"}
    assert properties["day"]["description"] == ("The day asked about, as YYYY-MM-DD, worked out from today's date - "
                                                "only when the user names a day, e.g. tomorrow. Leave it out otherwise.")
    for words in ("how long's the drive to work?", "what time do I leave for uni?", "never a guess at an address",
                  "just reply 'Okay.'", "When do you need to be there?"):
        assert words in schema["description"]


def test_claude_is_never_offered_the_leave_reminder():
    journeys = FakeJourneys()
    executor = ActionExecutor(journeys=journeys)
    assert "set_leave_reminder" not in {tool["name"] for tool in executor.tool_schemas()}
    asked = Action("set_leave_reminder", {"place": "Work", "leave_by": 1.0}, from_model=True)
    assert executor.execute(asked).ok is False and journeys.reminded == []
    # Nova's own, on a yes, sets it - for the satellite it was said on.
    executor.satellite = "kitchen"
    params = {"place": "Work", "destination": "zone.work", "origin": "zone.home", "mode": "driving",
              "arrive": 1000.5, "leave_by": 900.5, "watch": True}
    result = executor.execute(Action("set_leave_reminder", params))
    assert result.ok and result.say == "I'll remind you at 8:15 AM."
    assert journeys.reminded == [("Work", "zone.work", "zone.home", "driving", 1000.5, 900.5, True, "kitchen")]


def test_a_leave_reminder_set_at_the_pc_rings_where_its_timers_do():
    journeys = FakeJourneys()
    executor = ActionExecutor(journeys=journeys, satellite="pc", ring_on=lambda name: "kitchen",
                              room_of=lambda name: [name])
    params = {"place": "Work", "destination": "zone.work", "origin": "zone.home", "mode": "driving",
              "arrive": 1000.5, "leave_by": 900.5, "watch": True}
    result = executor.execute(Action("set_leave_reminder", params))
    assert journeys.reminded[0][7] == "kitchen"
    assert result.say == "I'll remind you at 8:15 AM. It'll ring on the kitchen."
    # Handed to the Pi in the same room, there's nothing more to say.
    executor.ring_on, executor.room_of = (lambda name: "bedroom"), (lambda name: [name, "bedroom"])
    assert executor.execute(Action("set_leave_reminder", params)).say == "I'll remind you at 8:15 AM."
    assert journeys.reminded[1][7] == "bedroom"


def test_without_home_assistant_journeys_say_so():
    executor = ActionExecutor()
    assert not {"journey", "set_leave_reminder"} & {tool["name"] for tool in executor.tool_schemas()}
    for tool in ("journey", "set_leave_reminder"):
        result = executor.execute(Action(tool, {"place": "Work"}))
        assert (result.ok, result.say) == (False, "Journeys need Home Assistant.")


# Notes saved on a computer

def _notes(title, text, where=None):
    return {}


def test_save_note_offered_only_with_notes():
    # With no way to save one, none is offered.
    assert "save_note" not in {tool["name"] for tool in ActionExecutor(clock=object()).tool_schemas()}
    schemas = {tool["name"]: tool for tool in ActionExecutor(notes=_notes).tool_schemas()}
    assert schemas["save_note"]["input_schema"]["required"] == ["title", "text"]
    assert set(schemas["save_note"]["input_schema"]["properties"]) == {"title", "text", "where"}
    # Which computer is given as it is for the tools which open things.
    assert schemas["save_note"]["input_schema"]["properties"]["where"] == ON_A_COMPUTER


def test_save_note_is_offered_only_while_a_satellite_saves_notes():
    # The satellite the user is talking to need not be one: the Pi's notes go to the PC's Downloads.
    saving = []
    executor = ActionExecutor(notes=_notes, notes_offered=lambda: bool(saving))
    assert "save_note" not in {tool["name"] for tool in executor.tool_schemas()}
    saving.append("pc")
    assert "save_note" in {tool["name"] for tool in executor.tool_schemas()}
    # Asked for by name while none does, it's said, not tried.
    saving.clear()
    result = executor.execute(Action("save_note", {"title": "Bins", "text": "Green on Tuesday."}))
    assert (result.ok, result.say) == (False, "None of your computers can save notes.")


def test_save_note_passes_title_text_and_where():
    saved = []

    def notes(title, text, where):
        saved.append((title, text, where))
        return {"success": True, "message": "Saved to your Downloads as Bins."}

    executor = ActionExecutor(notes=notes)
    result = executor.execute(
        Action("save_note", {"title": "Bins", "text": "Green on Tuesday.", "extra": "ignored"}, from_model=True))
    assert (result.ok, result.say, result.confirm) == (True, "Saved to your Downloads as Bins.", "speak")
    # The computer the user names is passed as they said it, and none named as none.
    executor.execute(Action("save_note", {"title": "Bins", "text": "Green.", "where": " my PC "}, from_model=True))
    for where in ("", "  ", None, 5):
        executor.execute(Action("save_note", {"title": "Bins", "text": "Green.", "where": where}))
    assert saved == [("Bins", "Green on Tuesday.", None), ("Bins", "Green.", "my PC")] + [("Bins", "Green.", None)] * 4
    # What the session says of a note it couldn't save is said as it is.
    refused = ActionExecutor(notes=lambda title, text, where: {"success": False, "message": "The PC didn't answer."})
    result = refused.execute(Action("save_note", {"title": "Bins", "text": "Green on Tuesday."}))
    assert (result.ok, result.say) == (False, "The PC didn't answer.")


def test_save_note_description_keeps_the_list_apart():
    schemas = {tool["name"]: tool for tool in ActionExecutor(notes=_notes).tool_schemas()}
    description = schemas["save_note"]["description"]
    # "Add milk to my notes" is the notes list, which has a tool of its own.
    assert "'Add X to my notes' is the notes list" in description and "manage_list" in description
    assert "400 words" in description
    for words in ("make a note", "take a note", "write this down", "write me notes on", "Downloads"):
        assert words in description


def test_save_note_description_says_the_computer_is_only_for_when_the_user_names_it():
    # "Make a note on my PC" said at the Pi is the PC's; "make a note" alone isn't given a computer to guess at.
    schemas = {tool["name"]: tool for tool in ActionExecutor(notes=_notes).tool_schemas()}
    description = schemas["save_note"]["description"]
    assert "Give where only when the user names the computer ('make a note on my PC')" in description
    # No longer only the computer the user is talking to, which the Pi isn't.
    assert "the computer the user is talking to" not in description


def test_save_note_description_says_a_note_isnt_also_a_memory():
    # "Make a note that my bins go out on Tuesday" once had Claude save the note and remember it, so
    # Nova kept it in its memory as well and he had to say "don't remember that".
    schemas = {tool["name"]: tool for tool in ActionExecutor(notes=_notes).tool_schemas()}
    description = schemas["save_note"]["description"]
    assert ("A note is only saved as a file: don't also use remember for it, unless the user asks you to "
            "remember it too.") in description
    # Still the end of what's said, as the tool says what happened itself.
    assert description.endswith("What happened is said for you, so just reply 'Okay.'")


def test_save_note_asked_for_without_a_way_to_save_one_says_so():
    # It can be asked for by name, offered or not.
    result = ActionExecutor().execute(Action("save_note", {"title": "Bins", "text": "Green on Tuesday."}))
    assert (result.ok, result.say) == (False, "None of your computers can save notes.")


def test_save_note_with_nothing_to_write_or_a_notes_which_fails_says_so():
    saved = []
    executor = ActionExecutor(notes=lambda title, text, where: saved.append(title))
    # Nothing is saved for words which weren't given, whatever type the model gave.
    for text in ("", "  ", None, 5):
        result = executor.execute(Action("save_note", {"title": "Bins", "text": text}))
        assert (result.ok, result.say) == (False, "I didn't catch what to write.")
    assert saved == []

    # A title which isn't words is left for the satellite to name, as it does an empty one.
    ActionExecutor(notes=lambda title, text, where: saved.append(title) or {"success": True}).execute(
        Action("save_note", {"title": None, "text": "Green on Tuesday."}))
    assert saved == [""]

    # The session's end is another machine, never the end of this request.
    def broken(title, text, where):
        raise OSError("the satellite's lost it")

    result = ActionExecutor(notes=broken).execute(Action("save_note", {"title": "Bins", "text": "Green"}))
    assert (result.ok, result.say) == (False, "I couldn't save the note.")


def test_a_notes_text_is_only_logged_as_its_length():
    note = Action("save_note", {"title": "Bins", "text": "Green on Tuesday."}, from_model=True)
    shown = note.for_log()
    assert shown == Action("save_note", {"title": "Bins", "text": "<17 characters>"}, from_model=True)
    assert "Green" not in repr(shown) and "Bins" in repr(shown)
    # A copy: what's saved is the whole of it.
    assert note.params["text"] == "Green on Tuesday."
    # Whatever the model gave for it, none of that is shown; and other tools, and other parameters, are.
    assert Action("save_note", {"text": 5}).for_log().params == {"text": "<not logged>"}
    other = Action("set_timer", {"label": "Take the bins out", "text": "kept"})
    assert other.for_log() is other



def test_a_web_page_is_only_logged_by_its_host():
    page = Action("open_web_page", {"url": "https://www.bbc.co.uk/news/private?id=7", "where": "my PC"})
    assert page.for_log().params == {"url": "<www.bbc.co.uk>", "where": "my PC"}
    assert page.params["url"] == "https://www.bbc.co.uk/news/private?id=7"
    # Said bare, it's the same host; one which isn't a web address at all is only its length.
    assert Action("open_web_page", {"url": "bbc.co.uk/secret"}).for_log().params == {"url": "<bbc.co.uk>"}
    assert Action("open_web_page", {"url": "file:///c/secret"}).for_log().params == {"url": "<16 characters>"}
    assert Action("open_web_page", {"url": 5}).for_log().params == {"url": "<not logged>"}
    # What's searched for is never logged.
    assert Action("search_on_computer", {"words": "secret things"}).for_log().params == {"words": "<13 characters>"}

# Playlists and liked songs - see playlist_changes.py

ENDLESS = {"id": "s1", "title": "Endless", "artist": "Made Up Band"}
BEFORE = {"id": "s0", "title": "Before", "artist": "Made Up Band"}


class SongPlayer:
    """
    Stands in for NavidromePlayer with a song playing and the one before it, noting each time a song is read.
    A name other than Navidrome makes it another service's.
    """

    needs_internet = False

    def __init__(self, this=ENDLESS, last=BEFORE, name="Navidrome", playing=True):
        self.name = name
        self.songs = {"this": this, "last": last}
        self.playing = playing
        self.read = []

    def song_at(self, which):
        self.read.append(which)
        return self.songs[which]

    def was_playing(self):
        return self.playing

    def now_playing(self):
        song = self.songs["this"]
        return None if song is None else {"track": song["title"], "playing": self.playing}


class RecordingPlaylistChanges:
    """Stands in for PlaylistChanges, noting each call as (what, library, ...) and answering with the reply given."""

    def __init__(self, reply=None):
        self.reply = reply or {"success": True, "message": "Done."}
        self.calls = []

    def add(self, library, song, names):
        self.calls.append(("add", library, song, names))
        return dict(self.reply)

    def remove(self, library, song, names):
        self.calls.append(("remove", library, song, names))
        return dict(self.reply)

    def make(self, library, name, song):
        self.calls.append(("make", library, name, song))
        return dict(self.reply)

    def like(self, library, song, liked):
        self.calls.append(("like", library, song, liked))
        return dict(self.reply)


# What Claude calls for each of the four, bar which song.
PLAYLIST_ACTIONS = [
    Action("add_to_playlists", {"playlists": ["Gym", "Car"]}, from_model=True),
    Action("take_off_playlists", {"playlists": ["Gym"]}, from_model=True),
    Action("make_playlist", {"name": "Chill", "with_song": True}, from_model=True),
    Action("like_song", {}, from_model=True),
]


def test_add_this_to_playlists_gives_the_song_playing_and_the_names(make_executor):
    player = SongPlayer()
    changes = RecordingPlaylistChanges({"success": True, "message": "Added Endless to Gym and Car."})
    executor = make_executor(player, playlist_changes=changes)
    result = executor.execute(PLAYLIST_ACTIONS[0])
    assert (result.ok, result.say) == (True, "Added Endless to Gym and Car.")
    # The player itself is the library, and the song is the one at the queue's place unless "last" is said.
    assert changes.calls == [("add", player, ENDLESS, ["Gym", "Car"])]
    assert player.read == ["this"]


def test_each_tool_gives_what_it_changes_the_song_and_what_was_said(make_executor):
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    for action in PLAYLIST_ACTIONS:
        assert executor.execute(action).ok
    # Making one with the song on it, or not, and liking being what's wanted unless unliking is said.
    executor.execute(Action("make_playlist", {"name": "Empty"}, from_model=True))
    executor.execute(Action("like_song", {"like": False}, from_model=True))
    assert changes.calls == [("add", player, ENDLESS, ["Gym", "Car"]), ("remove", player, ENDLESS, ["Gym"]),
                             ("make", player, "Chill", ENDLESS), ("like", player, ENDLESS, True),
                             ("make", player, "Empty", None), ("like", player, ENDLESS, False)]


def test_the_last_song_is_the_one_before(make_executor):
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    executor.execute(Action("add_to_playlists", {"playlists": ["Gym"], "song": "last"}, from_model=True))
    executor.execute(Action("take_off_playlists", {"playlists": ["Gym"], "song": "last"}, from_model=True))
    executor.execute(Action("make_playlist", {"name": "Chill", "with_song": True, "song": "last"}, from_model=True))
    executor.execute(Action("like_song", {"song": "last"}, from_model=True))
    assert [call[2] if call[0] != "make" else call[3] for call in changes.calls] == [BEFORE] * 4
    assert player.read == ["last"] * 4
    # Anything else Claude says is this one.
    executor.execute(Action("like_song", {"song": "the one before"}, from_model=True))
    assert player.read[-1] == "this"


def test_nothing_playing_or_no_last_song_says_so(make_executor):
    player, changes = SongPlayer(this=None, last=None), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    for action in PLAYLIST_ACTIONS:
        result = executor.execute(action)
        assert (result.ok, result.say) == (False, "Nothing's playing."), action.tool
    for action in PLAYLIST_ACTIONS:
        last = Action(action.tool, {**action.params, "song": "last"}, from_model=True)
        result = executor.execute(last)
        assert (result.ok, result.say) == (False, "There isn't a last song."), action.tool
    assert changes.calls == []
    # A playlist made empty asks nothing of a song.
    assert executor.execute(Action("make_playlist", {"name": "Chill"}, from_model=True)).ok
    assert changes.calls == [("make", player, "Chill", None)]


def test_playlists_on_spotify_say_they_need_navidrome(make_executor):
    player, changes = SongPlayer(name="Spotify"), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    for action in PLAYLIST_ACTIONS + [Action("make_playlist", {"name": "Chill"}, from_model=True),
                                      Action("make_offered_playlist", {"name": "Jim", "song": ENDLESS})]:
        result = executor.execute(action)
        assert (result.ok, result.say) == (False, "Changing playlists only works with Navidrome."), action.tool
    assert changes.calls == [] and player.read == []


def test_navidrome_is_checked_on_the_player_whose_song_is_used(make_executor):
    # In a room, the music heard is the first player playing - which may not be this satellite's own.
    here, there = SongPlayer(name="Spotify", playing=False), SongPlayer(playing=True)
    changes = RecordingPlaylistChanges()
    executor = make_executor(here, playlist_changes=changes, room_players=lambda: [here, there])
    assert executor.execute(Action("like_song", {}, from_model=True)).ok
    assert changes.calls == [("like", there, ENDLESS, True)]
    assert here.read == [] and there.read == ["this"]

    # And the other way round: this satellite's Navidrome isn't what's playing.
    here, there = SongPlayer(playing=False), SongPlayer(name="Spotify", playing=True)
    changes = RecordingPlaylistChanges()
    executor = make_executor(here, playlist_changes=changes, room_players=lambda: [here, there])
    result = executor.execute(Action("like_song", {}, from_model=True))
    assert (result.ok, result.say) == (False, "Changing playlists only works with Navidrome.")
    assert changes.calls == []


def test_an_empty_playlist_is_made_on_a_navidrome_player_not_a_paused_spotify_one(make_executor):
    # Nothing is heard to follow for an empty playlist, so a Spotify player paused in the room, which is the
    # one a song would be taken from, mustn't refuse it while a Navidrome one is right there.
    make = Action("make_playlist", {"name": "Chill"}, from_model=True)
    offered = Action("make_offered_playlist", {"name": "Chill", "song": ENDLESS})
    for action in (make, offered):
        mine = SongPlayer(this=None, last=None, playing=False)
        there = SongPlayer(name="Spotify", playing=False)
        changes = RecordingPlaylistChanges()
        executor = make_executor(mine, playlist_changes=changes, room_players=lambda: [mine, there])
        assert executor.execute(action).ok, action.tool
        assert changes.calls[0][1] is mine, action.tool

    # This satellite's own Navidrome player is used even when the room's other one is the one playing.
    mine, there = SongPlayer(playing=False), SongPlayer(playing=True)
    changes = RecordingPlaylistChanges()
    executor = make_executor(mine, playlist_changes=changes, room_players=lambda: [mine, there])
    assert executor.execute(make).ok
    assert changes.calls == [("make", mine, "Chill", None)]

    # Else the room's first Navidrome player, skipping a Spotify one beside it.
    mine = SongPlayer(name="Spotify", playing=False)
    first, second = SongPlayer(name="Spotify", playing=True), SongPlayer(playing=False)
    third = SongPlayer(playing=True)
    changes = RecordingPlaylistChanges()
    executor = make_executor(mine, playlist_changes=changes, room_players=lambda: [mine, first, second, third])
    assert executor.execute(make).ok
    assert changes.calls == [("make", second, "Chill", None)]

    # And none of them Navidrome is still refused, as is a playlist made with a song on a Spotify player.
    only = SongPlayer(name="Spotify", playing=False)
    executor = make_executor(only, playlist_changes=RecordingPlaylistChanges(), room_players=lambda: [only])
    assert executor.execute(make).say == "Changing playlists only works with Navidrome."
    with_song = Action("make_playlist", {"name": "Chill", "with_song": True}, from_model=True)
    spotify = SongPlayer(name="Spotify", playing=True)
    mine = SongPlayer(playing=False)
    executor = make_executor(mine, playlist_changes=RecordingPlaylistChanges(), room_players=lambda: [mine, spotify])
    assert executor.execute(with_song).say == "Changing playlists only works with Navidrome."


def test_a_song_paused_on_another_satellite_in_the_room_is_the_one_changed(make_executor):
    # "Playing or paused, whoever paused it": paused on the other satellite, with nothing on this one.
    here, there = SongPlayer(this=None, last=None, playing=False), SongPlayer(playing=False)
    changes = RecordingPlaylistChanges()
    executor = make_executor(here, playlist_changes=changes, room_players=lambda: [here, there])
    assert executor.execute(Action("add_to_playlists", {"playlists": ["Gym"]}, from_model=True)).ok
    assert changes.calls == [("add", there, ENDLESS, ["Gym"])]
    assert here.read == [] and there.read == ["this"]

    # Music playing in the room is still the one heard before one paused, and this satellite's own
    # paused music is this one's, as a bare "play" carries it on.
    here, there = SongPlayer(playing=False), SongPlayer(this=BEFORE, last=ENDLESS, playing=True)
    changes = RecordingPlaylistChanges()
    executor = make_executor(here, playlist_changes=changes, room_players=lambda: [here, there])
    executor.execute(Action("like_song", {}, from_model=True))
    assert changes.calls == [("like", there, BEFORE, True)]
    here, there = SongPlayer(playing=False), SongPlayer(this=BEFORE, last=ENDLESS, playing=False)
    changes = RecordingPlaylistChanges()
    executor = make_executor(here, playlist_changes=changes, room_players=lambda: [here, there])
    executor.execute(Action("like_song", {}, from_model=True))
    assert changes.calls == [("like", here, ENDLESS, True)]


def test_without_playlist_changes_set_up_each_tool_says_so(make_executor):
    player = SongPlayer()
    executor = make_executor(player)
    for action in PLAYLIST_ACTIONS:
        result = executor.execute(action)
        assert (result.ok, result.say) == (False, "Changing playlists isn't set up."), action.tool
    result = executor.execute(Action("make_offered_playlist", {"name": "Jim", "song": ENDLESS}))
    assert (result.ok, result.say) == (False, "Changing playlists isn't set up.")
    assert player.read == []


def test_with_no_music_the_playlist_tools_say_so_and_are_not_offered(make_executor):
    executor = make_executor(playlist_changes=RecordingPlaylistChanges())
    assert not {"add_to_playlists", "take_off_playlists", "make_playlist", "like_song"} & {
        schema["name"] for schema in executor.tool_schemas()}
    assert executor.execute(Action("like_song", {}, from_model=True)).say == "The music isn't available right now."


def test_playlists_offline_say_so_without_asking(make_executor, monkeypatch):
    from voice_assistant import actions as actions_module
    monkeypatch.setattr(actions_module.network, "online", lambda: False)
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    player.needs_internet = True
    executor = make_executor(player, playlist_changes=changes)
    result = executor.execute(Action("like_song", {}, from_model=True))
    assert (result.ok, result.say) == (False, "Navidrome needs the internet, and I can't reach it right now.")
    assert changes.calls == [] and player.read == []


def test_the_offered_playlist_is_made_with_the_song_asked_about_not_the_one_playing_now(make_executor):
    # "Add this to Jim" offered to make Jim; the next song started before the yes. Review Focus 1.
    player, changes = SongPlayer(this=BEFORE, last=ENDLESS), RecordingPlaylistChanges(
        {"success": True, "message": "Made Jim with Endless on it."})
    executor = make_executor(player, playlist_changes=changes)
    result = executor.execute(Action("make_offered_playlist", {"name": "Jim", "song": ENDLESS}))
    assert (result.ok, result.say) == (True, "Made Jim with Endless on it.")
    assert changes.calls == [("make", player, "Jim", ENDLESS)]
    assert player.read == []


def test_the_offer_carried_by_a_reply_reaches_the_result_to_be_run_on_a_yes(make_executor):
    offer = Action("make_offered_playlist", {"name": "Jim", "song": ENDLESS})
    changes = RecordingPlaylistChanges({"success": True, "ask_first": offer,
                                        "message": "You haven't got a playlist called Jim. Want me to make it?"})
    executor = make_executor(SongPlayer(), playlist_changes=changes)
    result = executor.execute(Action("add_to_playlists", {"playlists": ["Jim"]}, from_model=True))
    assert result.ok and result.ask_first is offer


def test_the_offer_is_never_offered_to_claude(make_executor):
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    offered = {schema["name"] for schema in executor.tool_schemas()}
    assert "make_offered_playlist" not in offered
    assert {"add_to_playlists", "take_off_playlists", "make_playlist", "like_song"} <= offered
    # Nor can it run it by name.
    result = executor.execute(Action("make_offered_playlist", {"name": "Jim", "song": ENDLESS}, from_model=True))
    assert result.ok is False and changes.calls == []


def test_an_offer_without_a_song_to_put_on_it_makes_an_empty_playlist_rather_than_failing(make_executor):
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    assert executor.execute(Action("make_offered_playlist", {"name": "Jim"})).ok
    assert changes.calls == [("make", player, "Jim", None)]


def test_an_offer_whose_song_has_lost_its_id_is_refused_not_made_empty(make_executor):
    # The offer was for a playlist with a song on it, so an empty one is not what the yes was for.
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    for song in ({"title": "No id"}, {"id": None, "title": "None"}, {"id": "", "title": "Empty"}, "Endless", {}):
        result = executor.execute(Action("make_offered_playlist", {"name": "Jim", "song": song}))
        assert (result.ok, result.say) == (False, "I've lost which song that was."), song
    assert changes.calls == []


def test_a_playlist_given_on_its_own_is_the_playlist_named(make_executor):
    # Claude sometimes gives "Gym" where the tool takes ["Gym"].
    player, changes = SongPlayer(), RecordingPlaylistChanges()
    executor = make_executor(player, playlist_changes=changes)
    executor.execute(Action("add_to_playlists", {"playlists": "Gym"}, from_model=True))
    executor.execute(Action("take_off_playlists", {"playlists": " Gym "}, from_model=True))
    assert changes.calls == [("add", player, ENDLESS, ["Gym"]), ("remove", player, ENDLESS, ["Gym"])]
    # Nothing in it is still nothing, for the playlists to say.
    executor.execute(Action("add_to_playlists", {"playlists": "  "}, from_model=True))
    executor.execute(Action("add_to_playlists", {"playlists": 7}, from_model=True))
    assert changes.calls[2:] == [("add", player, ENDLESS, []), ("add", player, ENDLESS, [])]


def test_the_playlist_tools_are_described_for_claude(make_executor):
    executor = make_executor(SongPlayer(), playlist_changes=object())
    offered = {schema["name"]: schema for schema in executor.tool_schemas()}
    properties = {name: offered[name]["input_schema"]["properties"] for name in offered if name in (
        "add_to_playlists", "take_off_playlists", "make_playlist", "like_song")}
    assert set(properties["add_to_playlists"]) == set(properties["take_off_playlists"]) == {"playlists", "song"}
    assert properties["add_to_playlists"]["playlists"]["type"] == "array"
    assert set(properties["make_playlist"]) == {"name", "with_song", "song"}
    assert set(properties["like_song"]) == {"like", "song"}
    for each in properties.values():
        assert each["song"]["enum"] == ["this", "last"]
    for name in ("add_to_playlists", "take_off_playlists", "make_playlist", "like_song"):
        assert "just reply 'Okay.'" in offered[name]["description"], name
        assert offered[name]["input_schema"].get("required", []) == [], name
    assert "liked songs" in offered["add_to_playlists"]["description"]
    assert "Never make a playlist unless the user says new or make" in offered["add_to_playlists"]["description"]
    assert "make a playlist called" in offered["make_playlist"]["description"]
    assert "add this to a new playlist called" in offered["make_playlist"]["description"]


def test_the_playlist_tools_change_things_so_a_later_undo_reaches_only_the_last(make_executor):
    tools = ActionExecutor.tools()
    for name in ("add_to_playlists", "take_off_playlists", "make_playlist", "like_song", "make_offered_playlist"):
        assert tools[name].tool["changes"] is True and tools[name].tool["needs"] == "music", name
    assert tools["make_offered_playlist"].tool["for_model"] is False

    undone = []
    changes = RecordingPlaylistChanges({"success": True, "message": "Added.", "undo": lambda: (
        undone.append(1) or {"success": True, "message": "Took it back."})})
    executor = make_executor(SongPlayer(), playlist_changes=changes)
    executor.execute(Action("add_to_playlists", {"playlists": ["Gym"]}, from_model=True))
    assert executor.execute(UNDO_THAT).say == "Took it back." and undone == [1]


# Opening, closing and searching on a computer

class FakeComputerControl:
    """Stands in for ComputerControl: keeps what it was asked, and whether a computer opens things."""

    def __init__(self, offered=True):
        self.is_offered = offered
        self.asked = []

    def offered(self):
        return self.is_offered

    def has_microphone(self):
        # Not a computer whose own microphone can be muted - see the pc_and_pi tests for one.
        return False

    def _did(self, *asked):
        self.asked.append(asked)
        return {"success": True, "message": "Opening Fortnite."}

    def open(self, name, where=None):
        return self._did("open", name, where)

    def open_url(self, url, where=None):
        return self._did("open_url", url, where)

    def search(self, words, where=None):
        return self._did("search", words, where)

    def close(self, name, where=None):
        return self._did("close", name, where)


COMPUTER_TOOLS = {"open_on_computer", "open_web_page", "search_on_computer", "close_on_computer"}


def test_tools_offered_only_when_known():
    def offered(executor):
        return {tool["name"] for tool in executor.tool_schemas()}

    assert not COMPUTER_TOOLS & offered(ActionExecutor(clock=object()))
    computer = FakeComputerControl(offered=False)
    assert not COMPUTER_TOOLS & offered(ActionExecutor(computer=computer))
    # Once a computer which opens things has been seen - connected or not - all four are.
    computer.is_offered = True
    schemas = {tool["name"]: tool for tool in ActionExecutor(computer=computer).tool_schemas()}
    assert COMPUTER_TOOLS <= set(schemas)
    for name in COMPUTER_TOOLS:
        assert "What happened is said for you, so just reply 'Okay.'" in schemas[name]["description"]
        assert "where" in schemas[name]["input_schema"]["properties"]
    # Asked for anyway while none is, it's refused.
    computer.is_offered = False
    result = ActionExecutor(computer=computer).execute(Action("open_on_computer", {"name": "Fortnite"}, from_model=True))
    assert (result.ok, result.say) == (False, "There's no computer here to open things on.")
    assert computer.asked == []


def test_computer_tools_pass_what_was_said():
    computer = FakeComputerControl()
    executor = ActionExecutor(computer=computer)
    result = executor.execute(Action("open_on_computer", {"name": "Fortnite", "where": "my PC"}, from_model=True))
    assert (result.ok, result.say) == (True, "Opening Fortnite.")
    executor.execute(Action("open_web_page", {"url": "bbc.co.uk"}, from_model=True))
    executor.execute(Action("search_on_computer", {"words": "rocket league", "where": ""}, from_model=True))
    executor.execute(Action("close_on_computer", {"name": "this"}, from_model=True))
    assert computer.asked == [("open", "Fortnite", "my PC"), ("open_url", "bbc.co.uk", None),
                              ("search", "rocket league", None), ("close", "this", None)]
    # Nothing to open, or a name that isn't words, asks no computer.
    for tool, params in (("open_on_computer", {"name": " "}), ("open_web_page", {"url": 5}),
                         ("search_on_computer", {}), ("close_on_computer", {"name": None})):
        result = executor.execute(Action(tool, params, from_model=True))
        assert result.ok is False and result.say
    assert len(computer.asked) == 4


def test_a_failed_mute_of_the_sound_still_speaks(pc_and_pi):
    # Nothing to mute at the Pi without its music player.
    pc_and_pi.pi.actions.music = None
    failed = pc_and_pi.pi.actions.execute(Action("set_volume", {"mute": True}))
    assert not failed.ok and failed.confirm == "speak" and failed.say
    assert confirm([failed]) == ([], [failed.say])


# Starting and ending an open conversation

def _spoken(name):
    """A satellite as a reply calls it, the PC's name being "pc" and the Pi's "bedroom"."""
    return satellite_names.spoken(name, "pc" if name == "pc" else "pi", [])


def _talking(satellite="pc", on_at=None, settings=None):
    """An executor at a satellite with the brain's one OpenConversation, on at on_at or not."""
    conversation = OpenConversation(lambda: {"enabled": True} if settings is None else settings)
    if on_at:
        conversation.start(on_at)
    return ActionExecutor(satellite=satellite, conversation=conversation, spoken_name=_spoken), conversation


def _offered_conversation(executor):
    return {tool["name"] for tool in executor.tool_schemas()} & {"start_conversation", "end_conversation"}


def test_start_conversation_starts_one_and_says_so_with_no_chime():
    executor, conversation = _talking("bedroom")
    result = executor.execute(Action("start_conversation"))
    assert (result.ok, result.say, result.confirm) == (True, "Go ahead.", "speak")
    assert conversation.satellite == "bedroom"
    # Said, with no sound: the session plays the listening sound, and the chime would be a second.
    assert confirm([result], chime_enabled=True, fallback="Okay.") == ([], ["Go ahead."])


def test_start_conversation_where_one_is_on_already_says_go_ahead_again():
    executor, conversation = _talking("pc", on_at="pc")
    result = executor.execute(Action("start_conversation"))
    assert (result.ok, result.say) == (True, "Go ahead.")
    assert conversation.satellite == "pc"


def test_a_second_satellite_is_told_where_it_is():
    executor, conversation = _talking("pc", on_at="bedroom")
    result = executor.execute(Action("start_conversation"))
    # The name as a reply gives the Pi, "the Pi", and nothing is started at the PC.
    assert (result.ok, result.say) == (False, "We're already talking at the Pi.")
    assert conversation.satellite == "bedroom"
    # A name he gave it is its name.
    named = ActionExecutor(satellite="pc", conversation=conversation, spoken_name=lambda name: "Desk")
    assert named.execute(Action("start_conversation")).say == "We're already talking at Desk."


def test_a_muted_microphone_refuses_a_start():
    # Asked by typing at a wake-word satellite whose microphone is muted: nothing would hear him, so nothing starts.
    muted = [True]
    conversation = OpenConversation(lambda: {"enabled": True})
    executor = ActionExecutor(satellite="bedroom", conversation=conversation, spoken_name=_spoken,
                              muted=lambda: muted[0])
    result = executor.execute(Action("start_conversation"))
    assert (result.ok, result.say) == (False, "The microphone's muted.")
    assert conversation.satellite is None
    # Read as it is now.
    muted[0] = False
    assert executor.execute(Action("start_conversation")).say == "Go ahead."
    assert conversation.satellite == "bedroom"


def test_a_satellite_which_doesnt_know_conversations_is_told_it_needs_updating():
    # Not offered to Claude there, and the phrase refused - though they're switched on.
    conversation = OpenConversation(lambda: {"enabled": True})
    executor = ActionExecutor(satellite="pc", conversation=conversation, spoken_name=_spoken,
                              converses=lambda: False)
    assert _offered_conversation(executor) == set()
    result = executor.execute(Action("start_conversation"))
    assert (result.ok, result.say) == (False, "This satellite needs updating for conversations.")
    assert conversation.satellite is None
    # Switched off, that's what's said, wherever it's asked.
    off = ActionExecutor(satellite="pc", conversation=OpenConversation(lambda: {"enabled": False}),
                         converses=lambda: False)
    assert off.execute(Action("start_conversation")).say == "Conversations are switched off."


def test_a_routine_never_runs_start_conversation():
    # A routine may run by itself, with nobody there - see Conversation._run.
    assert ActionExecutor.routine_refusals() == {"start_conversation": "A routine can't start a conversation."}


def test_end_conversation_ends_it_and_says_okay():
    executor, conversation = _talking("pc", on_at="pc")
    result = executor.execute(Action("end_conversation"))
    assert (result.ok, result.say, result.confirm) == (True, "Okay.", "speak")
    assert conversation.satellite is None
    assert confirm([result], fallback="Okay.") == ([], ["Okay."])


def test_end_conversation_is_offered_only_while_one_is_on_here():
    executor, conversation = _talking("pc")
    assert _offered_conversation(executor) == {"start_conversation"}
    conversation.start("pc")
    assert _offered_conversation(executor) == {"start_conversation", "end_conversation"}
    # On at another satellite, there's nothing here to end.
    elsewhere, _ = _talking("bedroom", on_at="pc")
    assert _offered_conversation(elsewhere) == {"start_conversation"}
    # Asked for by name where none is on, it's said, and nothing is ended.
    result = elsewhere.execute(Action("end_conversation", from_model=True))
    assert (result.ok, result.say) == (False, "We're not in a conversation.")
    assert elsewhere.conversation.satellite == "pc"


def test_end_conversation_says_so_when_it_ended_itself_since_it_was_offered():
    executor, conversation = _talking("pc", on_at="pc")
    method = ActionExecutor.tools()["end_conversation"]
    conversation.end("idle")
    result = method(executor)
    assert (result.ok, result.say) == (False, "We're not in a conversation.")


def test_end_conversation_never_ends_one_at_another_satellite():
    # Only a race reaches it: the one here ended since the tool was offered, and another began elsewhere.
    executor, conversation = _talking("pc", on_at="bedroom")
    result = ActionExecutor.tools()["end_conversation"](executor)
    assert (result.ok, result.say) == (False, "We're not in a conversation.")
    assert conversation.satellite == "bedroom"


def test_switched_off_there_are_no_tools():
    settings = {"enabled": False}
    executor, conversation = _talking("pc", settings=settings)
    assert _offered_conversation(executor) == set()
    result = executor.execute(Action("start_conversation", from_model=True))
    assert (result.ok, result.say) == (False, "Conversations are switched off.")
    assert conversation.satellite is None
    # It is read as it is now: switched on, they come back.
    settings["enabled"] = True
    assert _offered_conversation(executor) == {"start_conversation"}
    # Switched off while one is on, a start goes but the end stays, so the microphone is never left open.
    conversation.start("pc")
    settings["enabled"] = False
    assert _offered_conversation(executor) == {"end_conversation"}
    assert executor.execute(Action("end_conversation")).ok
    assert conversation.satellite is None


def test_start_conversation_says_so_when_it_was_switched_off_since_it_was_offered():
    settings = {"enabled": True}
    executor, conversation = _talking("pc", settings=settings)
    # Between the tool being offered and run - the race start() answers "off" for.
    method = ActionExecutor.tools()["start_conversation"]
    executor.conversation = SimpleNamespace(start=lambda satellite, by=None: "off")
    result = method(executor)
    assert (result.ok, result.say) == (False, "Conversations are switched off.")


def test_without_a_conversation_there_are_no_tools():
    # As for every executor which isn't given one - and one with no satellite to talk at.
    assert _offered_conversation(ActionExecutor(satellite="pc")) == set()
    assert _offered_conversation(ActionExecutor(conversation=OpenConversation(lambda: {"enabled": True}))) == set()
    result = ActionExecutor(satellite="pc").execute(Action("start_conversation"))
    assert (result.ok, result.say) == (False, "Conversations are switched off.")


def test_the_conversation_tools_are_for_claude_to_use_in_any_words():
    executor, _ = _talking("pc", on_at="pc")
    schemas = {tool["name"]: tool for tool in executor.tool_schemas()}
    for name in ("start_conversation", "end_conversation"):
        assert schemas[name]["input_schema"] == {"type": "object", "properties": {}, "required": []}
    assert "never call it unasked" in schemas["start_conversation"]["description"]
    assert "'stop'" in schemas["end_conversation"]["description"]


def test_the_model_tools_move_the_conversation_up_and_back():
    executor, conversation = _talking("pc", on_at="pc")
    assert executor.execute(Action("use_bigger_model")).ok and conversation.bigger
    result = executor.execute(Action("use_quick_model"))
    assert (result.ok, result.say, result.confirm) == (True, "Okay, quick answers.", "speak")
    assert not conversation.bigger
    # Neither is Claude's to call - its own way up is think_harder - and neither works where none is on.
    assert not executor.execute(Action("use_bigger_model", from_model=True)).ok and not conversation.bigger
    elsewhere, _ = _talking("bedroom", on_at="pc")
    result = elsewhere.execute(Action("use_bigger_model"))
    assert (result.ok, result.say) == (False, "We're not in a conversation.")
    assert not elsewhere.conversation.bigger


def test_the_model_tools_only_move_the_conversation_which_is_on_here():
    # Asked for between the tool being found available and run: it ended, and another began at the Pi.
    executor, conversation = _talking("pc", on_at="pc")
    method = ActionExecutor.tools()["use_bigger_model"]
    conversation.end("idle")
    conversation.start("bedroom")
    result = method(executor)
    assert (result.ok, result.say) == (False, "We're not in a conversation.") and not conversation.bigger
    conversation.move_up("bedroom")
    result = ActionExecutor.tools()["use_quick_model"](executor)
    assert (result.ok, result.say) == (False, "We're not in a conversation.") and conversation.bigger
