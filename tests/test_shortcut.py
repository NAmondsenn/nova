"""Tests for the shortcut's text: reading what's typed on the settings page, and writing and showing it."""

import pytest

from voice_assistant.shortcut import DEFAULT, Shortcut, ShortcutError, read, shown, written


def test_the_default_reads():
    assert read(DEFAULT) == Shortcut(("ctrl", "alt"), "n")


def test_case_spaces_and_order_dont_matter():
    assert read(" Alt + CTRL+n ") == read("ctrl+alt+n")
    assert written(read("shift+ctrl+f5")) == "ctrl+shift+f5"


def test_other_names_for_the_same_keys():
    assert read("control+n") == Shortcut(("ctrl",), "n")
    assert read("windows+n") == Shortcut(("win",), "n")
    assert read("option+n") == Shortcut(("alt",), "n")


def test_a_repeated_modifier_counts_once():
    assert read("ctrl+ctrl+n") == Shortcut(("ctrl",), "n")


def test_empty_is_no_shortcut():
    assert read("") is None
    assert read("   ") is None
    assert written(None) == ""


def test_a_function_key_alone_is_allowed():
    assert read("f13") == Shortcut((), "f13")
    with pytest.raises(ShortcutError):
        read("f25")


def test_a_letter_alone_is_refused():
    with pytest.raises(ShortcutError) as refused:
        read("n")
    assert str(refused.value) == "A shortcut needs Ctrl, Alt, Shift or Win with the key, unless it's a function key."


def test_no_key_is_refused():
    with pytest.raises(ShortcutError) as refused:
        read("ctrl+alt")
    assert str(refused.value) == "A shortcut needs a key as well as Ctrl, Alt, Shift or Win."


def test_an_unknown_key_is_named():
    with pytest.raises(ShortcutError) as refused:
        read("ctrl+banana")
    assert str(refused.value) == "Nova doesn't know the key 'banana'."
    with pytest.raises(ShortcutError) as refused:
        read("ctrl+a+b")
    assert str(refused.value) == "A shortcut has one key, and that had two: 'a' and 'b'."


@pytest.mark.parametrize("not_text", [None, 5])
def test_not_text_is_refused(not_text):
    with pytest.raises(ShortcutError) as refused:
        read(not_text)
    assert str(refused.value) == "That didn't make sense."


def test_shown():
    assert shown(read("ctrl+alt+n")) == "Ctrl+Alt+N"
    assert shown(read("ctrl+f5")) == "Ctrl+F5"
    assert shown(read("f5")) == "F5"
    assert shown(read("shift+space")) == "Shift+Space"
    assert shown(read("win+space")) == "Win+Space"
