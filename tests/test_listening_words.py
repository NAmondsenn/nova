"""
Listening Words Test
Checks the text Claude is given to answer listening questions from: built from the snapshot of
SoulSync's stats recorded live on 15 November (tests/fixtures, with made-up names), so nothing is
ever sent to SoulSync and no real listening is in the repo.
"""

import copy
import json
import os

import pytest

from voice_assistant.listening import NOT_YET, WINDOWS, Listening, ListeningUnavailable, Snapshot
from voice_assistant.listening_words import describe


def _fixture(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class FakeListening:
    """Stands in for Listening: gives the snapshot it was made with, or says why there isn't one."""

    def __init__(self, snapshot=None, problem=None):
        self._snapshot = snapshot
        self._problem = problem

    def require(self):
        if self._problem:
            raise ListeningUnavailable(self._problem)
        return self._snapshot


def _listening(**week):
    """A Listening whose every window is the fixture's, with the changes given to the week's overview."""
    answer = _fixture("soulsync_stats_cached.json")
    for key, value in week.items():
        if key in answer:
            answer[key] = value
    return FakeListening(Snapshot(windows={window: copy.deepcopy(answer) for window in WINDOWS.values()}))


def _with(**changes):
    """The fixture's week with changes to the overview, and the period before when it is given."""
    answer = _fixture("soulsync_stats_cached.json")
    answer["overview"].update(changes.pop("overview", {}))
    if "previous" in changes:
        answer["previous"] = changes.pop("previous")
    answer.update(changes)
    return FakeListening(Snapshot(windows={window: copy.deepcopy(answer) for window in WINDOWS.values()}))


def test_summary_week():
    # Made-up figures: 600 plays against 400 before is 50% more, and 37,800,000 ms is 10 hours 30 minutes.
    listening = _with(overview={"total_plays": 600, "total_time_ms": 37_800_000, "unique_artists": 14},
                      previous={"total_plays": 400, "total_time_ms": 1, "unique_artists": 1})
    result = describe(listening, None, None)
    assert result["success"] is True
    message = result["message"]
    assert "the last 7 days" in message
    assert "600 plays" in message
    assert "10 hours 30 minutes" in message
    assert "14 artists" in message
    assert "up 50%" in message
    # The first three artists, in order, with their plays, and no more.
    assert message.index("Artist One (30 plays)") < message.index("Artist Two (26 plays)") < message.index("Artist Three (22 plays)")
    assert "Artist Four" not in message


def test_summary_is_the_default_and_the_week_the_default_period():
    assert describe(_listening(), None, None) == describe(_listening(), "week", "summary")


def test_summary_down_and_same():
    down = describe(_with(overview={"total_plays": 80}, previous={"total_plays": 100}), "week", "summary")["message"]
    assert "down 20%" in down
    same = describe(_with(overview={"total_plays": 100}, previous={"total_plays": 100}), "week", "summary")["message"]
    assert "up" not in same and "down" not in same and "the same" in same


def test_all_time_has_no_change_figure():
    # SoulSync gives no period before for all time, so none is said, whatever else it holds.
    message = describe(_with(previous=None), "all", "summary")["message"]
    assert "all time" in message
    assert "up " not in message and "down " not in message and "before" not in message


def test_no_change_when_the_period_before_had_no_plays():
    message = describe(_with(previous={"total_plays": 0}), "month", "summary")["message"]
    assert "the last 30 days" in message
    assert "up " not in message and "down " not in message


def test_listening_time_is_said_plainly():
    def said(ms):
        return describe(_with(overview={"total_time_ms": ms}), "week", "summary")["message"]

    assert "1 hour 1 minute" in said(3_660_000)
    assert "2 hours of listening" in said(7_200_000)
    assert "45 minutes" in said(2_700_000) and "hour" not in said(2_700_000)


def test_empty_window():
    listening = _with(overview={"total_plays": 0, "total_time_ms": 0, "unique_artists": 0}, previous=None)
    result = describe(listening, "week", "summary")
    assert result == {"success": True, "message": "You haven't listened to anything in the last 7 days."}
    # Whatever is asked of an empty window, no top lists are made of it.
    for about in ("artists", "songs", "albums", "genres", "when"):
        assert describe(listening, "week", about) == result
    assert describe(listening, "all", "summary")["message"] == "You haven't listened to anything yet."


def test_unavailable():
    result = describe(FakeListening(problem=NOT_YET), "week", "summary")
    assert result == {"success": False, "message": NOT_YET}


def test_a_listening_which_has_never_worked_says_so():
    result = describe(Listening({}, None), "week", "summary")
    assert result == {"success": False, "message": NOT_YET}


def test_artists_albums_and_songs_are_the_top_five():
    artists = describe(_listening(), "month", "artists")["message"]
    assert "the last 30 days" in artists
    for name in ("Artist One (30 plays)", "Artist Two (26 plays)", "Artist Three (22 plays)", "Artist Four (18 plays)",
                 "Artist Five (14 plays)"):
        assert name in artists
    assert "Artist Six" not in artists
    # The fixture has three albums: all of them are said, with their artist.
    albums = describe(_listening(), "week", "albums")["message"]
    assert "Album One by Artist One (20 plays)" in albums and "Album Three by Artist Three (14 plays)" in albums


def test_songs_name_artist():
    message = describe(_listening(), "year", "songs")["message"]
    assert "the last 12 months" in message
    assert "Song A by Artist One (12 plays)" in message
    assert message.index("Song A") < message.index("Song B") < message.index("Song C")


def test_one_play_is_not_plays():
    top = [{"name": "Song A", "artist": "Artist One", "play_count": 1}]
    assert "Song A by Artist One (1 play)" in describe(_with(top_tracks=top), "week", "songs")["message"]


def test_an_entry_without_a_name_is_passed_over():
    top = [{"play_count": 9}, {"name": "Song B", "artist": "Artist Two", "play_count": 4}, "odd", {"name": "Song C", "play_count": "x"}]
    message = describe(_with(top_tracks=top), "week", "songs")["message"]
    assert "Song B by Artist Two (4 plays)" in message and "9" not in message and "odd" not in message
    assert "Song C" not in message


def test_a_missing_list_is_said_so():
    answer = _fixture("soulsync_stats_cached.json")
    answer["top_albums"] = []
    listening = FakeListening(Snapshot(windows={"7d": answer}))
    assert describe(listening, "week", "albums") == {
        "success": True, "message": "SoulSync has no top albums for the last 7 days."}


def test_genres_are_the_top_three_with_percentages():
    genres = [{"genre": "Pop", "play_count": 70, "percentage": 58.3}, {"genre": "Rock", "play_count": 30, "percentage": 25.0},
              {"genre": "Jazz", "play_count": 10, "percentage": 8.3}, {"genre": "Folk", "play_count": 5, "percentage": 4.2}]
    message = describe(_with(genres=genres), "week", "genres")["message"]
    assert "Pop (58%)" in message and "Rock (25%)" in message and "Jazz (8%)" in message
    assert "Folk" not in message


def test_when_uses_peak_and_streak():
    # The fixture's peak is weekday 4 (Thursday, as SoulSync counts from Sunday) at 18:00, the longest streak
    # 7 days, the busiest day 30 plays.
    message = describe(_listening(), "week", "when")["message"]
    assert "Thursdays" in message and "6pm" in message
    assert "longest streak is 7 days" in message
    assert "Friday the 13th of November" in message and "30 plays" in message


def test_weekdays_count_from_sunday_as_soulsync_does():
    # SoulSync's clock is SQLite's strftime('%w'): 0 is Sunday, 1 Monday ... 6 Saturday.
    def said(weekday):
        clock = {"peak": {"weekday": weekday, "hour": 9, "plays": 3}}
        return describe(_with(clock=clock, rhythm={}), "week", "when")["message"]

    for weekday, name in enumerate(("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")):
        assert f"most on {name}s" in said(weekday)
    assert "Saturdays" not in said(7) and "Sundays" not in said(7)


def test_hours_are_said_as_a_person_would():
    def said(hour):
        clock = {"peak": {"weekday": 0, "hour": hour, "plays": 3}}
        return describe(_with(clock=clock, rhythm={}), "week", "when")["message"]

    assert "Sundays" in said(0) and "midnight" in said(0)
    assert "9am" in said(9)
    assert "midday" in said(12)
    assert "11pm" in said(23)


def test_when_with_nothing_known_says_so():
    message = describe(_with(clock={}, rhythm={}), "week", "when")["message"]
    assert message == "SoulSync has nothing on when you listen for the last 7 days."


@pytest.mark.parametrize("period, about", [("fortnight", None), ("week", "moods"), (7, None), ("week", ["artists"])])
def test_what_isnt_offered_is_refused(period, about):
    result = describe(_listening(), period, about)
    assert result["success"] is False and result["message"]


def test_a_window_missing_from_the_snapshot_is_unavailable():
    listening = FakeListening(Snapshot(windows={}))
    assert describe(listening, "week", "summary")["success"] is False


def test_an_odd_answer_never_raises():
    # SoulSync's answer is checked at the refresh only for the keys it must have, so the words must not trust the rest.
    for answer in ({"overview": {"total_plays": "x"}, "top_artists": None}, {"overview": [], "top_artists": []},
                   {"overview": {"total_plays": 5, "total_time_ms": None}, "top_artists": [None, 3], "clock": 4,
                    "rhythm": "x", "genres": {"a": 1}, "previous": {"total_plays": "y"}}):
        listening = FakeListening(Snapshot(windows={"7d": answer}))
        for about in ("summary", "artists", "songs", "albums", "genres", "when"):
            result = describe(listening, "week", about)
            assert isinstance(result["success"], bool) and isinstance(result["message"], str)
