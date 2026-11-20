"""
Satellite Names Module.

How a satellite is named in words: which satellite "my Pi", "the PC" or a name set on
the settings page means, and what it's called in a reply. Nothing here reads a file or
asks the brain, so it can be tried with any satellites; the brain gives it what it knows
of each (see Brain.find_satellite).

A satellite can be meant by its own name ("home assistant"), a name given to it on the
settings page, or a word for the kind of machine it is ("pc", "computer"). The first two
are what he chose, so they win: with "computer" set on the PC, "the computer" means the
PC alone, though the Mac is one too. Only when neither fits are the machine's words tried.
"""

import re
from typing import Dict, List, Optional, Tuple

# What each kind of machine is called when it isn't named, in plain() form. "computer" is
# both a PC and a Mac, so it's ambiguous until he names one on the settings page. A Linux
# computer that isn't a Pi has none: it's only ever found by its name.
MACHINE_WORDS: Dict[str, Tuple[str, ...]] = {
    "pi": ("pi", "pie", "raspberry pi", "raspberry pie"),
    "pc": ("pc", "computer", "desktop"),
    "mac": ("mac", "macbook", "laptop", "computer"),
    "linux": (),
}

# What a reply calls a machine which has no name set on the settings page.
SPOKEN = {"pi": "the Pi", "pc": "the PC", "mac": "the Mac"}

# Said before a name, and dropped: "my PC", "the PC" and "this PC" are all "pc".
_ARTICLES = ("the", "my", "this", "that")

# What means the satellite he is talking to, in plain() form: "here", and "this device", "this
# one", "this speaker" and "this satellite" once "this" is dropped. A word for a kind of machine
# is never one of these, so "this PC" is still the PC wherever he says it.
_HERE_WORDS = ("here", "device", "one", "speaker", "satellite")


def plain(said: str) -> str:
    """
    Words as they're compared: lower case, with the leading "the", "my", "this" or "that" and
    a trailing "satellite" dropped, and the dashes and underscores a satellite's name has read as
    spaces. The last word is always kept, so a satellite called "the" is still found.

    Args:
        said: What was said, or a satellite's name, or a name set for it.

    Returns:
        The words, single-spaced - e.g. "The PC satellite" and "p.c." are both "pc", and
        "home-assistant" is "home assistant".
    """
    # Dots and apostrophes go without a space, so "p.c." (as a transcript spells it) is "pc"
    # and "Dad's PC" is the same whichever way it's typed; every other mark is a space.
    words = re.sub(r"[\W_]+", " ", re.sub(r"[.'\u2019]", "", said.lower())).split()
    while len(words) > 1 and words[0] in _ARTICLES:
        words.pop(0)
    if len(words) > 1 and words[-1] == "satellite":
        words.pop()
    return " ".join(words)


def means_here(said: str) -> bool:
    """
    Args:
        said: Words which may mean the satellite he is talking to, e.g. "this device".

    Returns:
        Whether they do - "here", "this device", "this one", "this speaker" or "this satellite".
        Nothing else does, so "this PC" is judged by matching like any other name.
    """
    return plain(said) in _HERE_WORDS


def matching(said: str, satellites: Dict[str, Tuple[Optional[str], List[str]]]) -> List[str]:
    """
    Finds the satellites some words name.

    Args:
        said: The words, e.g. "my Pi".
        satellites: Each satellite there is, in the order they should be given back, by
            name, with its machine (one of machine.MACHINES, or None if it isn't known)
            and the names set for it on the settings page.

    Returns:
        The satellites named, in the order given. First those whose own name or page
        names are the words; only if there are none, those whose machine's words are.
        Empty if nothing fits, and two or more if the words don't say which.
    """
    wanted = plain(said)
    if not wanted:
        return []
    named = [name for name, (_, called) in satellites.items()
             if wanted in [plain(each) for each in [name] + called]]
    if named:
        return named
    return [name for name, (machine, _) in satellites.items() if wanted in MACHINE_WORDS.get(machine, ())]


def spoken(name: str, machine: Optional[str], called: List[str]) -> str:
    """
    What a satellite is called in a reply.

    Args:
        name: The satellite's name, e.g. "home-assistant".
        machine: What kind of machine it is, or None if it isn't known.
        called: The names set for it on the settings page.

    Returns:
        The first name set on the settings page which isn't one of the machine's own words,
        as it's what he calls it; otherwise "the Pi", "the PC" or "the Mac"; otherwise the
        satellite's own name. The case is as it is in the middle of a sentence - the caller
        capitalises one it starts.
    """
    own = MACHINE_WORDS.get(machine, ())
    for each in called:
        # "computer" set on the PC is for settling what "the computer" means, and in a reply
        # "Moved to computer." is worse than "Moved to the PC.".
        if plain(each) not in own:
            return each
    return SPOKEN.get(machine, name)


def listed(names: List[str], last: str) -> str:
    """
    Joins names to be said in a sentence.

    Args:
        names: What's to be listed, e.g. ["the PC", "the Mac", "Desk"].
        last: The word before the last one - "and" or "or".

    Returns:
        "the PC, the Mac or Desk" - with commas and no more, as it's said aloud.
    """
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} {last} {names[-1]}"
