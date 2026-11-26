"""
PC Module
Turning the user's PC on and off by voice, through Home Assistant - decision 13 of
docs/decisions/0008.

The PC is a HASS.Agent device, found by its maker in the registry, never by the
windows_pc_ in its entity ids, which the user can rename - and only one holding entities
of HASS.Agent's own, as its integration's device for the same machine holds none. Its buttons are found by what
each does (see ha_registry.button_kind), and only those in PC_KINDS are ever pressed - never
its launchurl, which opens any web address. It's woken by the Wake-on-LAN integration's
button: the only one, with one PC and one button, else the one chosen for it on the page.

Whether it's on is whether any of HASS.Agent's own entities on it is available: while it's
off, every one of them reads "unavailable" (live check 5).

Every power command confirms with the success chime, not words (his words, 21 Nov): the
words stay as the message, for the log and for when the chime is switched off. Shutting down
and restarting don't ask first, as a deliberate "Hey Nova" or press came before them - except
in a conversation, where none need, so a stray remark could: there they ask - "Shut down the
PC?" - and only a yes said next does it (see Conversation.handle). The yes runs
press_pc_button, which Claude isn't offered, so it can't skip the question. Failures still
speak. At the PC, the button is pressed only once the chime has played (decision 0034).

Every press goes through HomeAssistant.press, whose own check of the snapshot is the last
word on which buttons may be pressed.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from voice_assistant.actions import Action
from voice_assistant.batteries import spoken_name
from voice_assistant.choices import ChoiceStore
from voice_assistant.ha_registry import PC_KINDS, Snapshot
from voice_assistant.home_assistant import CANT_REACH, HomeAssistant, NotAllowed
from voice_assistant.lists import NO_ANSWER

# Logger setup
logger = logging.getLogger(__name__)

# The button each action presses, by HASS.Agent's kind.
KINDS = {"off": "shutdown", "restart": "restart", "sleep": "sleep", "screen_off": "monitorsleep", "lock": "lock"}
# What's asked before shutting down or restarting in a conversation.
ASKED = {"off": "Shut down the PC?", "restart": "Restart the PC?"}
# What's said as each button is pressed - kept for the log, and said if the chime's off.
PRESSED = {"off": "Shutting down the PC.", "restart": "Restarting the PC.", "sleep": "Putting your PC to sleep.",
           "screen_off": "Turning your PC's screen off.", "lock": "Locking your PC."}
# Each kind as it's said, for a button the PC hasn't got.
KIND_SAID = {"shutdown": "shutdown", "restart": "restart", "sleep": "sleep", "monitorsleep": "monitor sleep",
             "lock": "lock"}

# The integration HASS.Agent's own entities come from. Only they say whether the PC is on:
# Home Assistant can merge another's onto its device by the hardware address - Wake-on-LAN's
# button, whose state is when it was last pressed, is never unavailable.
HASS_AGENT_PLATFORM = "mqtt"

# The words which say nothing of which PC was meant - "my PC" is any of them.
NAME_FILLER = {"my", "the", "pc", "computer"}


@dataclass
class PC:
    """
    One HASS.Agent PC, as Home Assistant has it now.

    Attributes:
        device: Its Home Assistant device id.
        name: Its name, as it's said, e.g. "Desk PC".
        on: Whether any of HASS.Agent's own entities on it is available.
        buttons: Its buttons in PC_KINDS, by kind, e.g. {"shutdown": "button.desk_pc_shutdown"}.
        wake: The Wake-on-LAN button which wakes it, or None if that isn't known.
    """

    device: str
    name: str
    on: bool
    buttons: Dict[str, str] = field(default_factory=dict)
    wake: Optional[str] = None


def find_pcs(states: List[Dict], snapshot: Snapshot, wake: Dict[str, str]) -> List[PC]:
    """
    Every HASS.Agent PC.

    Args:
        states: Every entity's state, as /api/states has them.
        snapshot: The registry.
        wake: The Wake-on-LAN button chosen for each PC, by device id.

    Returns:
        One for each HASS.Agent device with entities of HASS.Agent's own, in the order the
        registry has them.
    """
    state_of = {state.get("entity_id"): str(state.get("state")) for state in states}
    buttons = snapshot.hass_agent_buttons()
    # Each device's own entities. One with none isn't a PC of its own: the HASS.Agent
    # integration makes a second device for the same machine, under the same name, which
    # holds only its media player - nothing to press, and nothing to say whether it's on.
    own = {device: [entity for entity in snapshot.on_device(device)
                    if snapshot.platform(entity) == HASS_AGENT_PLATFORM]
           for device in dict.fromkeys(device for device, _ in snapshot.hass_agent_commands.values())}
    devices = [device for device, entities in own.items() if entities]
    wake_buttons = snapshot.wake_buttons()
    pcs = []
    for device in devices:
        entities = own[device]
        chosen = wake.get(device)
        if len(devices) == 1 and len(wake_buttons) == 1:
            woken_by = wake_buttons[0]
        else:
            woken_by = chosen if chosen in wake_buttons else None
        pcs.append(PC(
            device=device,
            name=spoken_name(snapshot.device_name(device) or "PC"),
            # An entity missing from the states says nothing either way.
            on=any(entity in state_of and state_of[entity] != "unavailable" for entity in entities),
            buttons={kind: entity for entity, (owner, kind) in buttons.items() if owner == device and kind in PC_KINDS},
            wake=woken_by))
    return pcs


def _words(name: str) -> set:
    """A name's words, lower-cased without punctuation, e.g. "Gaming-PC's" as {"gaming", "pcs"}."""
    return set(re.sub(r"[^a-z0-9 ]", "", name.lower().replace("-", " ")).split())


def _named(pcs: List[PC], name: Optional[str]) -> List[PC]:
    """The PCs a name fits - all of them for none, else those whose name has every word said."""
    words = _words(name or "") - NAME_FILLER
    if not words:
        return pcs
    return [pc for pc in pcs if words <= _words(pc.name)]


class PCPower:
    """Turns the user's PC on and off, and sleeps, locks or restarts it, confirming each with the chime."""

    def __init__(self, home: HomeAssistant, choices: ChoiceStore):
        """
        Args:
            home: The Home Assistant to ask.
            choices: The Wake-on-LAN button chosen for each PC, under "wake", by device id -
                needed only with more than one PC or more than one button.
        """
        self.home = home
        self.choices = choices

    def overview(self) -> Dict:
        """
        Every PC and Wake-on-LAN button Home Assistant has, for the settings page.

        Returns:
            {"pcs": [{"device", "name", "wake"}], "buttons": [{"id", "name"}]} - each button
            by its friendly name, as the user knows it, and "wake" the button which wakes
            the PC, or None. Both are empty while the registry can't be read.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If its states can't be read.
        """
        snapshot = self.home.snapshot()
        if snapshot is None:
            return {"pcs": [], "buttons": []}
        states = self.home.states()
        names = {state.get("entity_id"): (state.get("attributes") or {}).get("friendly_name") for state in states}
        return {"pcs": [{"device": pc.device, "name": pc.name, "wake": pc.wake}
                        for pc in find_pcs(states, snapshot, self._wake())],
                # Never the entity id as a name - it holds the PC's hardware address.
                "buttons": [{"id": button, "name": names.get(button) or "Wake-on-LAN button"}
                            for button in snapshot.wake_buttons()]}

    def choose_wake(self, device: str, button: str, seen: Optional[Dict] = None) -> List[str]:
        """
        Chooses the Wake-on-LAN button which wakes a PC, from the next "turn on the PC".

        One button wakes one machine, so it's taken off any other PC it was chosen for -
        rather than refused, which would leave two PCs chosen the wrong way round with
        neither able to be put right.

        Args:
            device: The PC's Home Assistant device id, a HASS.Agent device.
            button: The button's entity id, a Wake-on-LAN button.
            seen: What overview gave just before, if the caller read it already - so it can
                read without holding a lock the save needs.

        Returns:
            The names of the PCs the button was taken off, as overview has them - only
            those Home Assistant is still showing, as the page shows no other.

        Raises:
            ValueError: If Home Assistant isn't showing that PC and button.
            OSError: If the choice can't be saved.
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        if seen is None:
            seen = self.overview()
        if (not any(pc["device"] == device for pc in seen["pcs"])
                or not any(each["id"] == button for each in seen["buttons"])):
            raise ValueError("Home Assistant isn't showing that PC or button just now.")
        taken_from = []

        def choose(state):
            wake = state["wake"] if isinstance(state["wake"], dict) else {}
            taken_from[:] = [other for other, chosen in wake.items() if chosen == button and other != device]
            state["wake"] = {other: chosen for other, chosen in wake.items() if other not in taken_from}
            state["wake"][device] = button

        self.choices.change(choose)
        names = {pc["device"]: pc["name"] for pc in seen["pcs"]}
        return [names[other] for other in taken_from if other in names]

    def _wake(self) -> Dict[str, str]:
        """The button chosen for each PC - none, if pcs.json's been edited into something else."""
        wake = self.choices.get("wake")
        return wake if isinstance(wake, dict) else {}

    def _find(self, name: Optional[str]):
        """
        The PC meant, read afresh.

        Returns:
            (the PC, the snapshot, None) - or (None, None, the reply to give instead).
        """
        snapshot = self.home.snapshot()
        if snapshot is None:
            return None, None, {"success": False, "message": "I can't check your PC's buttons just now."}
        try:
            states = self.home.states()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't read Home Assistant's states for the PC: {e}")
            return None, None, {"success": False, "message": CANT_REACH}
        pcs = find_pcs(states, snapshot, self._wake())
        if not pcs:
            return None, None, {"success": False, "message": "I can't find your PC in Home Assistant."}
        if len(pcs) == 1:
            return pcs[0], snapshot, None
        fits = _named(pcs, name)
        if len(fits) != 1:
            names = [pc.name for pc in pcs]
            return None, None, {"success": False, "message": f"Which PC, {', '.join(names[:-1])} or {names[-1]}?"}
        return fits[0], snapshot, None

    def act(self, action: str, name: Optional[str] = None, defer: bool = False, ask: bool = False) -> Dict:
        """
        Does what was asked of the PC, confirming a success with the chime.

        Args:
            action: "on", "off", "restart", "sleep", "screen_off", "lock" or "status".
            name: Which PC, as the user said it, or None - needed only with several.
            defer: Whether to leave pressing the button until the confirmation has been given
                - the chime - as at the PC, checked first so the reply is still true.
            ask: Whether shutting down or restarting asks first, as in a conversation.

        Returns:
            {"success", "message"}, with "chime" for a success which is confirmed by the
            chime, and "after_said" for what defer leaves, which presses the button and gives
            {"success", "message"} when called - or, asking first, the question, with
            "ask_first", the Action a yes runs.
        """
        if action not in ("on", "status", *KINDS):
            return {"success": False, "message": "I didn't catch what to do with your PC."}
        pc, snapshot, reply = self._find(name)
        if reply:
            return reply
        if action == "status":
            return {"success": True, "message": "Your PC's on." if pc.on else "Your PC's off."}
        if action == "on":
            if pc.on:
                return {"success": True, "message": "Your PC's already on."}
            if pc.wake is None:
                if not snapshot.wake_buttons():
                    return {"success": False, "message": "There's no Wake-on-LAN button for your PC in Home Assistant."}
                return {"success": False, "message": "I don't know which Wake-on-LAN button wakes your PC yet. "
                                                     "Choose it on the settings page, under Calendars and lists."}
            return self._chimed(self._press(pc.wake, "Wake-on-LAN", "Turning your PC on."))
        if not pc.on:
            return {"success": False,
                    "message": "Your PC's already off." if action in ("off", "restart") else "Your PC's off."}
        # Not asked about a button that isn't there, which a yes couldn't press.
        missing = self._missing(pc, action)
        if missing:
            return missing
        if ask and action in ASKED:
            return {"success": True, "message": ASKED[action],
                    "ask_first": Action("press_pc_button", {"action": action, "name": name})}
        button, kind = pc.buttons[KINDS[action]], KINDS[action]
        if defer:
            return {"success": True, "message": PRESSED[action], "chime": True,
                    "after_said": lambda: self._press(button, kind, PRESSED[action])}
        return self._chimed(self._press(button, kind, PRESSED[action]))

    @staticmethod
    def _chimed(reply: Dict) -> Dict:
        """A press's reply, confirmed by the chime if it worked - a failure is left to speak."""
        return {**reply, "chime": True} if reply["success"] else reply

    @staticmethod
    def _missing(pc: PC, action: str) -> Optional[Dict]:
        """What to say if the PC hasn't got the button an action presses, else None."""
        if KINDS[action] in pc.buttons:
            return None
        return {"success": False,
                "message": f"There's no {KIND_SAID[KINDS[action]]} button for your PC in Home Assistant."}

    def _press(self, entity_id: str, kind: str, said: str) -> Dict:
        """
        Presses a button, saying so - or why not.

        Args:
            entity_id: The button.
            kind: What it is, for the log, e.g. "shutdown" - never its entity id, as the
                Wake-on-LAN button's holds the PC's hardware address.
            said: What to say once it's pressed.
        """
        try:
            self.home.press(entity_id)
        except requests.ConnectTimeout:
            # Never sent, so certainly not pressed.
            logger.warning(f"Couldn't reach Home Assistant to press the PC's {kind} button")
            return {"success": False, "message": CANT_REACH}
        except requests.Timeout:
            # Sent, but unanswered - so it may well have been pressed.
            logger.warning(f"Home Assistant didn't answer in time pressing the PC's {kind} button")
            return {"success": False, "message": f"{NO_ANSWER}, so I can't tell whether your PC heard."}
        except (requests.RequestException, NotAllowed) as e:
            logger.warning(f"Couldn't press the PC's {kind} button: {e}")
            return {"success": False, "message": HomeAssistant.failure_message(e)}
        logger.info(f"Pressed the PC's {kind} button")
        return {"success": True, "message": said}
