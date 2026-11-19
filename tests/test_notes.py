"""
Notes Test
Saving a note as a text file in the Downloads folder - safely, whatever the title says.
"""

import os

import pytest

from voice_assistant import notes


@pytest.fixture
def downloads(tmp_path):
    """A Downloads folder inside a tmp folder, so a file landing anywhere else shows."""
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


def test_saves_a_new_text_file(downloads):
    assert notes.save_note(str(downloads), "Bins", "Green bin on Tuesday\n", windows=False) == "Bins.txt"
    assert (downloads / "Bins.txt").read_bytes() == b"Green bin on Tuesday\n"


def test_windows_line_endings(downloads):
    notes.save_note(str(downloads), "Bins", "a\nb", windows=True)
    assert (downloads / "Bins.txt").read_bytes() == b"a\r\nb"


def test_other_machines_keep_plain_line_endings(downloads):
    notes.save_note(str(downloads), "Bins", "a\nb", windows=False)
    assert (downloads / "Bins.txt").read_bytes() == b"a\nb"


def test_saves_as_utf_8(downloads):
    notes.save_note(str(downloads), "Café", "crème brûlée", windows=False)
    assert (downloads / "Café.txt").read_bytes() == "crème brûlée".encode("utf-8")


def test_never_overwrites(downloads):
    (downloads / "Bins.txt").write_text("mine")
    assert notes.save_note(str(downloads), "Bins", "new", windows=False) == "Bins (2).txt"
    assert (downloads / "Bins.txt").read_text() == "mine"
    assert notes.save_note(str(downloads), "Bins", "newer", windows=False) == "Bins (3).txt"
    assert (downloads / "Bins (2).txt").read_text() == "new"
    assert (downloads / "Bins (3).txt").read_text() == "newer"


def test_gives_up_after_99(downloads):
    (downloads / "Bins.txt").write_text("1")
    for number in range(2, 100):
        (downloads / f"Bins ({number}).txt").write_text(str(number))
    with pytest.raises(notes.NoteRefused) as refused:
        notes.save_note(str(downloads), "Bins", "one too many", windows=False)
    assert str(refused.value) == "There are already 99 notes called that."
    assert len(os.listdir(downloads)) == 99


@pytest.mark.parametrize(
    "title", ["../../x", "C:\\Windows\\x", "con", ".bashrc", "", "a" * 500, "bins\x00\x07", "what?.  "]
)
def test_hostile_titles_stay_in_downloads(tmp_path, downloads, title):
    name = notes.save_note(str(downloads), title, "text", windows=False)
    # The only file made is the one in Downloads itself, and nothing else turned up beside it.
    assert os.listdir(downloads) == [name]
    assert os.listdir(tmp_path) == ["Downloads"]
    assert os.path.dirname(os.path.join(str(downloads), name)) == str(downloads)
    assert not name.startswith(".")
    assert name.endswith(".txt")
    assert len(name) <= 84
    for character in '<>:"/\\|?*\x00\x07':
        assert character not in name


def test_the_names_hostile_titles_become(downloads):
    assert notes.save_note(str(downloads), "con", "text", windows=False) == "con note.txt"
    assert notes.save_note(str(downloads), "", "text", windows=False) == "Note.txt"
    assert notes.save_note(str(downloads), "../../x", "text", windows=False) == "x.txt"
    assert notes.save_note(str(downloads), ".bashrc", "text", windows=False) == "bashrc.txt"
    assert notes.save_note(str(downloads), "what?.  ", "text", windows=False) == "what.txt"
    assert notes.save_note(str(downloads), "a" * 500, "text", windows=False) == "a" * 80 + ".txt"


@pytest.mark.parametrize(
    "title, safe",
    [
        ("Bins", "Bins"),
        ("  Shopping   list  ", "Shopping list"),
        ("The 5:30 train", "The 530 train"),
        ("   ", "Note"),
        ("...", "Note"),
        ("PRN", "PRN note"),
        ("Aux", "Aux note"),
        ("com1", "com1 note"),
        ("LPT9", "LPT9 note"),
        ("com10", "com10"),
        ("console", "console"),
        # Windows treats everything from the first dot as an extension, so con.x is still the console.
        ("con.x", "con note.x"),
        ("a" * 79 + " b", "a" * 79),
    ],
)
def test_safe_name(title, safe):
    assert notes.safe_name(title) == safe


def test_too_long_text_is_refused(downloads):
    with pytest.raises(notes.NoteRefused) as refused:
        notes.save_note(str(downloads), "Bins", "a" * (notes.MOST_TEXT + 1), windows=False)
    assert str(refused.value) == "That note's too long to save."
    assert os.listdir(downloads) == []


def test_the_longest_text_is_saved(downloads):
    notes.save_note(str(downloads), "Bins", "a" * notes.MOST_TEXT, windows=False)
    assert len((downloads / "Bins.txt").read_text()) == notes.MOST_TEXT


def test_missing_folder_is_refused(tmp_path):
    with pytest.raises(notes.NoteRefused) as refused:
        notes.save_note(str(tmp_path / "Downloads"), "Bins", "text", windows=False)
    assert str(refused.value) == "There's no Downloads folder to save it in."


def test_a_file_in_place_of_the_folder_is_refused(tmp_path):
    (tmp_path / "Downloads").write_text("not a folder")
    with pytest.raises(notes.NoteRefused):
        notes.save_note(str(tmp_path / "Downloads"), "Bins", "text", windows=False)


def test_another_failure_to_write_is_refused_in_a_sentence(downloads, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", refuse)
    with pytest.raises(notes.NoteRefused) as refused:
        notes.save_note(str(downloads), "Bins", "text", windows=False)
    assert str(refused.value) == "The file couldn't be written."


def test_text_that_cant_be_encoded_is_refused_and_leaves_no_file(downloads):
    # A lone surrogate can arrive in JSON, and can't be written as UTF-8.
    with pytest.raises(notes.NoteRefused) as refused:
        notes.save_note(str(downloads), "Bins", "bad \ud800 text", windows=False)
    assert str(refused.value) == "The file couldn't be written."
    assert os.listdir(downloads) == []
