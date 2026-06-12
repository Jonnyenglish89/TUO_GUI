#!/usr/bin/env python3
"""
TUO Sim Runner — GUI
====================
A Tkinter front-end for tuo.exe. Drop in the same folder as tuo.exe and run:
    python run_sim_gui.py

Fort rules enforced:
    - 0 or 2 forts only (never 1)
    - Siege forts (CS, LC, DF, IA, SF, DS, MC) -> your fort slot (yf)
    - Defense forts (TC, MF, FA, IB, FF)       -> enemy fort slot (ef)
    - Cannot mix siege and defense forts
    - Duplicates: "CS #2" or "Corrosive Spore #2"

Menu bar:
    Data -> Update XMLs   downloads fresh game data into the data/ folder
                          (same files as download-all.sh, no .etg caching)
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# Fort definitions
# ---------------------------------------------------------------------------

SIEGE_FORTS = {
    "CS": "Corrosive Spore",
    "LC": "Lightning Cannon",
    "DF": "Death Factory",
    "IA": "Inspiring Altar",
    "SF": "Sky Fortress",
    "DS": "Darkspire",
    "MC": "Medical Center",
}

DEFENSE_FORTS = {
    "TC": "Tesla Coil",
    "MF": "Minefield",
    "FA": "Foreboding Archway",
    "IB": "Illuminary Blockade",
    "FF": "Forcefield",
}

SIEGE_FULL_LOOKUP = {v.lower(): v for v in SIEGE_FORTS.values()}
DEFENSE_FULL_LOOKUP = {v.lower(): v for v in DEFENSE_FORTS.values()}

# Dropdown values: full names only (typing the abbreviation still works).
# Header rows group the list; selecting a header just clears the box.
SIEGE_HEADER = "───  SIEGE FORTS  ───"
DEFENSE_HEADER = "───  DEFENSE FORTS  ───"
FORT_CHOICES = (
    [""]
    + [SIEGE_HEADER]
    + list(SIEGE_FORTS.values())
    + [DEFENSE_HEADER]
    + list(DEFENSE_FORTS.values())
)

# ---------------------------------------------------------------------------
# Static options
# ---------------------------------------------------------------------------

MODES = [
    ("Battle / Mission", "pvp"),
    ("Battle (defense)", "pvp-defense"),
    ("GW", "gw"),
    ("GW (defense)", "gw-defense"),
    ("Brawl", "brawl"),
    ("Brawl (defense)", "brawl-defense"),
    ("Raid", "raid"),
    ("Campaign", "campaign"),
    ("CQ / Surge", "surge"),
]

ORDERS = [
    ("Random", "random"),
    ("Ordered (honor 3-card hand)", "ordered"),
    ("Flexible", "flexible"),
]

OPERATIONS = [
    ("Climb", "climb"),
    ("Sim", "sim"),
    ("Reorder", "reorder"),
    ("Climbex", "climbex"),
    ("Anneal", "anneal"),
    ("Debug", "debug sim"),
    ("Climb Forts", "climb_forts"),
    ("Genetic", "genetic"),
    ("Beam", "beam"),
]

DOMINION_OPTS = [
    ("dom-owned — owned dominions only", "dom-owned"),
    ("dom-maxed — suggest any dominion", "dom-maxed"),
    ("dom-none — dominions disabled", "dom-none"),
]

ENDGAME_OPTS = [
    ("none", None),
    ("0 - Maxed Units", "0"),
    ("1 - Maxed Fused", "1"),
    ("2 - Maxed Quads", "2"),
]

# Entries with placeholders are editable — X = number, n = count,
# S/S1/S2 = skill name, e.g. "Enfeeble all 2" or "Enhance all Counter 6".
BG_EFFECTS = [
    "none", "Blood-Vengeance", "Bloodlust X", "Brigade", "Cold-Sleep",
    "Counterflux", "Crackdown", "CriticalReach", "Devotion X", "Devour",
    "Divert", "EnduringRage", "Enfeeble all X", "Enhance all S X",
    "Evolve n S1 S2", "Fortification", "Furiosity", "HaltedOrders",
    "Heal all X", "Heroism", "Iron-Will", "Megamorphosis", "Metamorphosis",
    "Mortar X", "Oath-of-Loyalty", "Protect all X", "Rally all X",
    "Revenge X", "Siege all X", "Strike all X", "SuperHeroism",
    "TemporalBacklash", "TurningTides", "Unity", "Virulence",
    "Weaken all X", "ZealotsPreservation",
]

# File layout: data/ belongs to tuo (XMLs, ownedcards, bges.txt, database),
# tuo_gui_data/ belongs to this app (settings, presets, accounts, results).
GUI_DATA_DIR = Path("tuo_gui_data")
SETTINGS_FILE = GUI_DATA_DIR / "settings.json"
LEGACY_SETTINGS_FILE = Path("run_sim_gui_settings.json")  # pre-v1 location

# Hints shown when tuo exits with an error code
EXIT_HINTS = {
    "3": (
        "tuo crashed before finishing. Common causes: letters where a number is "
        "expected (e.g. iterations), an invalid effect/flag value, or a bad deck. "
        "Note: tuo's output can be cut off when it crashes, so the log above may "
        "be incomplete — double-check the command line."
    ),
}
GENERIC_FAIL_HINT = "tuo reported an error — check the output above for details."

# Timeout dropdown choices -> hours (tuo's 'timeout' flag takes hours)
TIMEOUT_CHOICES = [
    ("none", None),
    ("5 min", 5 / 60), ("10 min", 10 / 60), ("15 min", 0.25),
    ("30 min", 0.5), ("45 min", 0.75),
    ("1 hour", 1.0), ("2 hours", 2.0), ("4 hours", 4.0), ("8 hours", 8.0),
]


def parse_timeout(s: str) -> float | None:
    """Accept a dropdown label, plain hours ('2.5'), or '90 min' / '2 h'."""
    s = (s or "").strip().lower()
    if not s or s == "none":
        return None
    for label, hours in TIMEOUT_CHOICES:
        if s == label.lower():
            return hours
    m = re.fullmatch(r"([\d.]+)\s*(m|min|mins|minutes|h|hr|hrs|hours)?", s)
    if m:
        try:
            num = float(m.group(1))
        except ValueError:
            m = None
        else:
            unit = m.group(2) or "h"
            hours = num / 60 if unit.startswith("m") else num
            return hours if hours > 0 else None
    if not m:
        raise ValueError(
            f"Timeout '{s}' not understood — pick from the list, or type hours "
            "('2.5') or minutes ('90 min')."
        )
    return None


def parse_int(s: str, name: str, default: int | None = None) -> int:
    s = (s or "").strip()
    if not s:
        if default is None:
            raise ValueError(f"{name} is required.")
        return default
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"{name} must be a whole number (got '{s}').") from None


def parse_float(s: str, name: str, default: float | None = None) -> float:
    s = (s or "").strip()
    if not s:
        if default is None:
            raise ValueError(f"{name} is required.")
        return default
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"{name} must be a number (got '{s}').") from None

# ---------------------------------------------------------------------------
# Game-data (XML) updating  — mirrors download-all.sh, minus the .etg caching
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")
XML_BASE_URL = "https://mobile.tyrantonline.com/assets/"
XML_FILES = (
    ["fusion_recipes_cj2.xml", "missions.xml", "skills_set.xml"]
    + [f"cards_section_{i}.xml" for i in range(1, 22)]
    + ["items.xml", "levels.xml", "codex.xml", "achievements.xml", "raids.xml"]
)
FLAGS_WIKI_URL = "https://github.com/APN-Pucky/tyrant_optimize/wiki/Flags"

# War effects live in data/bges.txt as "Name: skill specs". Each side can have
# its own one in wars: yeffect/ye (yours), eeffect/ee (enemy's).
# tuo accepts the name directly: ye "Plasma Burst"
PLACEHOLDER_TOKENS = ("X", "S", "S1", "S2", "n")


def check_effect_placeholders(effect: str, label: str) -> None:
    """Reject unreplaced template placeholders before tuo crashes on them."""
    for tok in effect.replace(",", " ").split():
        if tok in PLACEHOLDER_TOKENS:
            raise ValueError(
                f"{label} '{effect}' still contains the placeholder '{tok}'. "
                "Replace it with a real value, e.g. 'Bloodlust 2' or "
                "'Enhance all Counter 6'."
            )


# data/*.txt files that are game data, not owned-cards inventories
NON_INVENTORY_TXT = {"bges.txt", "cardabbrs.txt"}


def list_inventory_files() -> list[str]:
    """Candidate owned-cards files in data/ (and data/ownedcards/, where the
    account updater writes), as relative path strings."""
    files: list[str] = []
    try:
        files += [
            str(Path("data") / p.name)
            for p in DATA_DIR.glob("*.txt")
            if p.name.lower() not in NON_INVENTORY_TXT
            and not p.name.lower().startswith("customdecks")
        ]
        files += [
            str(Path("data") / "ownedcards" / p.name)
            for p in (DATA_DIR / "ownedcards").glob("*.txt")
            # leftovers from older tooling, no longer generated
            if not p.name.endswith("_owned_only.txt")
        ]
    except OSError:
        pass
    return sorted(files)


# ---------------------------------------------------------------------------
# Accounts (Tyrant Unleashed API credentials for tu_inventory.py)
# ---------------------------------------------------------------------------

COOKIES_DIR = GUI_DATA_DIR / "cookies"
INVENTORY_SCRIPT = "tu_inventory.py"

# Stable credential keys worth saving; the rest of the POST data is
# per-request noise (timestamps, hashes, stat counters).
COOKIE_KEEP_KEYS = (
    "user_id", "password", "syncode", "kong_name", "kong_token", "kong_id",
    "client_version", "unity",
)
COOKIE_REQUIRED_KEYS = ("user_id", "password", "syncode", "kong_name", "kong_token")

ACCOUNT_INSTRUCTIONS = """\
How to get your API data (Firefox):

1. Go to:  https://www.kongregate.com/en/games/synapticon/tyrant-unleashed-web
2. Log in with your Kongregate username and password if you haven't already.
3. Press F12 and select the 'Network' tab.
4. In the filter box type:  api.php?message=init
   (if nothing appears, reload the page with F5)
5. Right-click that request -> Copy Value -> Copy POST Data.
6. Paste it below (Ctrl+V) and click 'Save account'.

Repeat for each Kongregate account you want to add — each saves separately.

Note: credentials are stored as plain text in tuo_gui_data\\cookies on this
computer. Only do this on a computer you trust."""


def parse_api_post_data(text: str) -> dict[str, str]:
    """Parse pasted POST data — newline-separated 'k=v' lines or one
    '&'-joined string. First occurrence of a key wins."""
    text = (text or "").strip()
    parts: list[str]
    if "\n" in text:
        parts = text.splitlines()
    else:
        parts = text.split("&")
    creds: dict[str, str] = {}
    for part in parts:
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k and v and k not in creds:
            creds[k] = v
    return creds


def save_account(creds: dict[str, str]) -> str:
    """Validate credentials and write cookie_<kong_name>. Returns kong name."""
    missing = [k for k in COOKIE_REQUIRED_KEYS if not creds.get(k)]
    if missing:
        raise ValueError(
            "This doesn't look like complete POST data — missing: "
            + ", ".join(missing)
            + ".\nMake sure you used 'Copy POST Data' on the api.php?message=init "
            "request and pasted everything."
        )
    kong_name = re.sub(r"[^\w.-]", "", creds["kong_name"])
    if not kong_name:
        raise ValueError("Could not derive a valid account name from kong_name.")
    keep = {k: creds[k] for k in COOKIE_KEEP_KEYS if k in creds}
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path = COOKIES_DIR / f"cookie_{kong_name}"
    cookie_path.write_text(
        "; ".join(f"{k}={v}" for k, v in keep.items()) + ";", encoding="utf-8"
    )
    return kong_name


def list_accounts() -> list[str]:
    try:
        return sorted(
            p.name[len("cookie_"):]
            for p in COOKIES_DIR.glob("cookie_*")
            if p.is_file()
        )
    except OSError:
        return []


# Deck files written by tu_inventory.py: lines like
#   gauntlet_<guild>_<kong_name>:<deck string>
DECK_TYPE_FILES = {
    "attack": GUI_DATA_DIR / "attack_decks.txt",
    "defence": GUI_DATA_DIR / "defence_decks.txt",
}


def load_account_deck(kong_name: str, deck_type: str) -> str | None:
    path = DECK_TYPE_FILES[deck_type]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    pat = re.compile(rf"^[^:]*_{re.escape(kong_name)}:(.*)$")
    for line in lines:
        m = pat.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def all_deck_account_names() -> set[str]:
    """Account names that appear in the attack/defence deck files."""
    names: set[str] = set()
    for path in DECK_TYPE_FILES.values():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            label = line.split(":", 1)[0].strip()
            if "_" in label:
                names.add(label.rsplit("_", 1)[1])
    return names


def account_owned_file(kong_name: str) -> Path:
    return Path("data") / "ownedcards" / f"ownedcards_{kong_name}.txt"


# ---------------------------------------------------------------------------
# Results: capture, persist (tuo_gui_data/results.txt), parse back
# ---------------------------------------------------------------------------

RESULTS_FILE = GUI_DATA_DIR / "results.txt"

_UNITS_RE = re.compile(r"(?:Optimized Deck:\s*)?(\d+)\s+units?:\s*(.+)")
_RESULT_HEADER_RE = re.compile(
    r"^===\s*(.+?)"                                      # name (+ optional label)
    r"(?:\s*@\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}))?"  # optional timestamp
    r"\s*===$"
)


def _find_deck_colon(s: str) -> int:
    """Position of the ': ' separating score info from the deck string."""
    best = -1
    for i in range(len(s)):
        if s[i] == ":" and (i + 1 >= len(s) or s[i + 1] == " "):
            if i > 0 and s[i - 1] in "0123456789)]":
                best = i
    return best


def parse_optimised_deck_line(raw_line: str) -> dict | None:
    """Parse 'Optimized Deck: N units: [$cost] [(x% win)] score [[per win]]: deck'.
    Handles all game modes, funded and unfunded, with or without the prefix."""
    m = _UNITS_RE.match((raw_line or "").strip())
    if not m:
        return None
    units, remainder = m.group(1), m.group(2)
    colon = _find_deck_colon(remainder)
    if colon >= 0:
        middle, deck = remainder[:colon].strip(), remainder[colon + 1:].strip()
    else:
        middle, deck = "", remainder.strip()

    cost = ""
    cm = re.search(r"\$[\d,]+", middle)
    if cm:
        cost = cm.group(0)
        middle = middle[:cm.start()] + middle[cm.end():]
    per_win = ""
    bm = re.search(r"\[[^\]]+\]", middle)
    if bm:
        per_win = bm.group(0)
        middle = middle[:bm.start()] + middle[bm.end():]
    win_pct = ""
    pm = re.search(r"\(([^)]*%\s*(?:win|stall))\)", middle)
    if pm:
        win_pct = f"({pm.group(1)})"
        middle = middle[:pm.start()] + middle[pm.end():]
    return {
        "units": units,
        "cost": cost,
        "win_pct": win_pct,
        "per_win": per_win,
        "score": middle.strip(),
        "deck": deck,
    }


def extract_result_lines(output_text: str) -> dict:
    """Pull the interesting result lines out of a tuo run's full output."""
    deck_line = upgraded = win_line = error = ""
    fallback_units = ""
    for line in output_text.splitlines():
        s = line.strip()
        if s.startswith("Optimized Deck:") and not s.startswith("Optimized Deck IDs"):
            deck_line = s
        elif _UNITS_RE.match(s):
            fallback_units = s  # GW sometimes omits the prefix
        elif s.startswith("Upgraded Cards:"):
            upgraded = s
        elif s.startswith("win%:"):
            win_line = s
        elif s.startswith("Error:"):
            error = s[len("Error:"):].strip()
    if not deck_line and fallback_units:
        deck_line = fallback_units
    return {
        "deck_line": deck_line, "upgraded": upgraded,
        "win_line": win_line, "error": error,
    }


def append_result_block(name: str, label: str, result: dict) -> None:
    """Append one result block to tuo_gui_data/results.txt (same format as
    other TUO tools: '=== name (label) @ timestamp ===')."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    label_tag = f" ({label})" if label else ""
    parts = [f"=== {name}{label_tag} @ {ts} ==="]
    if result["deck_line"]:
        parts.append(result["deck_line"])
        if result["upgraded"]:
            parts.append(result["upgraded"])
    elif result["win_line"]:
        parts.append(result["win_line"])
    elif result["error"]:
        parts.append(f"ERROR: {result['error']}")
    else:
        parts.append("ERROR: no result found in output")
    parts.append("")
    try:
        GUI_DATA_DIR.mkdir(exist_ok=True)
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(parts) + "\n")
    except OSError:
        pass


def summarize_result(result: dict) -> str:
    """One-line human summary of an extracted result."""
    if result["deck_line"]:
        parsed = parse_optimised_deck_line(result["deck_line"])
        if parsed:
            bits = [b for b in (
                parsed["score"], parsed["win_pct"], parsed["per_win"], parsed["cost"],
            ) if b]
            return f"{' '.join(bits)} — {parsed['deck']}"
        return result["deck_line"]
    if result["win_line"]:
        return result["win_line"]
    if result["error"]:
        return f"ERROR: {result['error']}"
    return "no result found"


def parse_results_file() -> list[dict]:
    """Read results.txt back into records for the Results tab."""
    try:
        content = RESULTS_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = []
    for block in re.split(r"(?=^===\s)", content, flags=re.MULTILINE):
        block = block.strip()
        if not block.startswith("==="):
            continue
        lines = block.splitlines()
        hm = _RESULT_HEADER_RE.match(lines[0].strip())
        if not hm:
            continue
        raw_name = hm.group(1).strip()
        ts = hm.group(2) or ""
        label = ""
        lm = re.match(r"^(.+?)\s*\(([\w-]+)\)\s*$", raw_name)
        if lm:
            raw_name, label = lm.group(1).strip(), lm.group(2)
        rec = {
            "time": ts, "name": raw_name, "op": label,
            "score": "", "win": "", "cost": "", "deck": "", "error": "",
        }
        for line in lines[1:]:
            s = line.strip()
            if not s:
                continue
            if s.startswith("ERROR:"):
                rec["error"] = s[6:].strip()
            elif s.startswith("win%:"):
                rec["win"] = s[5:].strip()
            elif s.startswith("Upgraded Cards:"):
                continue
            else:
                parsed = parse_optimised_deck_line(s)
                if parsed:
                    rec["score"] = parsed["score"]
                    rec["win"] = parsed["win_pct"] or rec["win"]
                    rec["cost"] = parsed["cost"]
                    rec["deck"] = parsed["deck"]
        records.append(rec)
    return records


WAR_EFFECT_SEP = " — "


def strip_war_effect(s: str) -> str:
    """'Plasma Burst — Heal All 9; ...' -> 'Plasma Burst'. Plain text passes through."""
    return s.split(WAR_EFFECT_SEP, 1)[0].strip()


PRESETS_FILE = GUI_DATA_DIR / "presets.json"
LEGACY_PRESETS_FILE = DATA_DIR / "presets.json"  # pre-v1 location


def load_presets() -> dict[str, dict]:
    """Event presets from tuo_gui_data/presets.json -> {name: {description, draft, settings}}."""
    for path in (PRESETS_FILE, LEGACY_PRESETS_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            presets = data.get("presets", {})
            return presets if isinstance(presets, dict) else {}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def load_named_bges() -> dict[str, str]:
    """Parse data/bges.txt -> {name: skill spec}. Missing file -> empty dict."""
    bges: dict[str, str] = {}
    try:
        for line in (DATA_DIR / "bges.txt").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if ":" in line:
                name, spec = line.split(":", 1)
                if name.strip():
                    bges[name.strip()] = spec.strip()
    except OSError:
        pass
    return bges

# ---------------------------------------------------------------------------
# Tooltips
# ---------------------------------------------------------------------------

class Tooltip:
    """Hover tooltip for any widget. Shows after a short delay."""

    DELAY_MS = 450

    def __init__(self, widget: tk.Widget, text: str, wraplength: int = 460):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self._tip: tk.Toplevel | None = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=self.text, justify="left", background="#ffffe0",
            relief="solid", borderwidth=1, wraplength=self.wraplength,
            font=("Segoe UI", 9), padx=6, pady=4,
        ).pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


TIPS = {
    "my_deck": (
        "Your deck: Commander, Card1, Card2, ...\n"
        "Use 'Card #2' for two copies of the same card.\n"
        "Put '!' before a name to force tuo to keep that card during optimization."
    ),
    "enemy_deck": (
        "The enemy: a gauntlet name (e.g. Arena), a custom deck name from\n"
        "data\\customdecks.txt, or a full deck string."
    ),
    "forts": (
        "0 or 2 forts per side — all siege OR all defense, never mixed.\n"
        "'Corrosive Spore #2' means two copies of the same fort.\n"
        "Typing abbreviations also works: CS, LC, DF, IA, SF, DS, MC (siege) /\n"
        "TC, MF, FA, IB, FF (defense)."
    ),
    "war_effect_mine": (
        "Your war battleground effect (ye flag) — in Guild Wars each side can\n"
        "set its own effect. Leave blank for none.\n\n"
        "The list comes from data\\bges.txt; the effects are shown next to the\n"
        "name so you can spot outdated values if the game rebalanced a BGE."
    ),
    "war_effect_enemy": (
        "The enemy's war battleground effect (ee flag) — in Guild Wars each\n"
        "side can set its own effect. Leave blank for none.\n\n"
        "The list comes from data\\bges.txt; the effects are shown next to the\n"
        "name so you can spot outdated values if the game rebalanced a BGE."
    ),
    "mode": (
        "The game mode to simulate — must match what you're actually playing.\n\n"
        "Battle / Mission — standard pvp battle\n"
        "Battle (defense) — how your deck performs as a defense\n"
        "GW / GW (defense) — Guild War attack / defense\n"
        "Brawl / Brawl (defense) — Brawl attack / defense\n"
        "Raid — maximize raid score\n"
        "Campaign — maximize surviving cards (plays as surge)\n"
        "CQ / Surge — enemy plays the first unit"
    ),
    "order": (
        "How your cards are played each turn during simulation:\n\n"
        "Random — random order each sim (tuo's default)\n"
        "Ordered — always plays the furthest-forward card of the 3-card hand\n"
        "Flexible — each turn plays the card with the best estimated win rate.\n"
        "Most realistic, but much slower."
    ),
    "bg_effect": (
        "Global battleground effect (-e flag) — applies equally to BOTH sides.\n"
        "Must match the BGE of the content you're simming. 'none' = no effect.\n\n"
        "Entries with placeholders must be edited before running:\n"
        "X = number, n = count, S/S1/S2 = skill name.\n"
        "e.g. 'Enfeeble all 2', 'Enhance all Counter 6', 'Evolve 1 Strike Besiege'."
    ),
    "endgame": (
        "Assume cards are upgraded to a certain level — speeds up optimization\n"
        "by removing upgrade variables:\n\n"
        "none — use cards exactly as they are in your inventory\n"
        "0 — all units assumed maxed\n"
        "1 — all units assumed maxed and fused\n"
        "2 — all units assumed maxed quads  ★ recommended for most runs"
    ),
    "dominion": (
        "How dominions are handled during optimization:\n\n"
        "dom-owned — only dominions you have in your ownedcards file (default)\n"
        "dom-maxed — suggest any dominion, including ones you don't own; the\n"
        "materials from your current owned dominion offset the upgrade cost\n"
        "dom-none — dominions disabled, none are considered"
    ),
    "deck_size": (
        "Limit the optimized deck size (-L from to).\n"
        "Both 1-10; equal values force an exact size (e.g. 10 to 10).\n"
        "Leave both blank for no limit."
    ),
    "operation": (
        "What tuo does:\n\n"
        "Climb — swaps one card at a time, keeps improvements. Fast, reliable;\n"
        "good starting point.\n"
        "Sim — no optimization, just reports your deck's win rate as-is.\n"
        "Reorder — keeps your exact cards, finds the best play order. Use after climb.\n"
        "Climbex — extended climb: starts at 'initial' iterations and multiplies\n"
        "up to 'final' for more thorough testing.\n"
        "Anneal — simulated annealing: temporarily accepts worse decks to escape\n"
        "dead ends. Slower but more thorough. Try 1000 / 100 / 0.001.\n"
        "Debug — verbose diagnostic run, for troubleshooting.\n"
        "Climb Forts — finds the best fort combination instead of cards.\n"
        "Genetic — evolves a pool of decks over generations. Wide exploration.\n"
        "Beam — explores several deck paths at once. Speed/coverage balance."
    ),
    "iterations": (
        "How many games are simulated per evaluation.\n"
        "Higher = more accurate, but slower.\n\n"
        "Quick check: 1,000\n"
        "Normal optimization: 10,000\n"
        "Final / accurate: 100,000+"
    ),
    "climbex_initial": "Climbex starting iterations per test. Recommended: 10",
    "climbex_final": (
        "Climbex final iterations — the starting count is multiplied (×10 by\n"
        "default) until it reaches this. Recommended: 1000"
    ),
    "anneal_iter": "Number of annealing iterations. Recommended: 1000",
    "anneal_temp": (
        "Starting temperature for simulated annealing.\n\n"
        "High temperature = explores wildly at first, accepting worse decks to\n"
        "escape dead ends. As it cools it behaves like a normal climb.\n"
        "Recommended: 100"
    ),
    "anneal_decay": (
        "How quickly the annealing temperature cools.\n"
        "Lower = cools slowly, explores more broadly.\n"
        "Higher = settles faster.\n"
        "Recommended: 0.001"
    ),
    "use_owned": (
        "Restrict optimization to cards you own (recommended).\n"
        "Untick to let tuo consider EVERY card in the game — useful for\n"
        "theoretical 'best possible deck' searches."
    ),
    "owned_file": (
        "Which owned-cards file to use. Blank = data\\ownedcards.txt.\n"
        "The file MUST be in the data folder or tuo silently ignores it —\n"
        "Browse… offers to copy outside files in for you."
    ),
    "threads": (
        "Number of CPU threads tuo uses. Set this near your core count.\n"
        "tuo drops to 1 thread automatically when iterations < 10."
    ),
    "timeout": (
        "Stop the run after this long and output the best deck found so far.\n"
        "Works with all optimization operations; checked between iterations so\n"
        "it can overrun slightly. 'none' = run until finished.\n"
        "You can also type a value: '90 min' or '2.5' (hours)."
    ),
    "fund": (
        "SP (salvage point) budget for buying upgrades. tuo considers upgrades\n"
        "up to this amount. 0 = no funded sim."
    ),
    "keep_commander": (
        "Lock your commander so optimization never swaps it out.\n"
        "Use when building around a specific commander."
    ),
    "no_db": (
        "no-db — disable tuo's simulation database (data\\database.yml).\n\n"
        "By default tuo stores every simulation there and reuses past results\n"
        "(when they had enough iterations) instead of simulating again.\n"
        "Ticked: nothing is loaded from or written to the database — every\n"
        "sim is computed fresh.\n\n"
        "Related flags for the Extra box: no-db-write, no-db-load, strict-db,\n"
        "db-limit N."
    ),
    "flag_so": "+so — shorter tuo score output.",
    "flag_uc": (
        "+uc — print an extra list of upgraded cards at the end.\n"
        "Recommended: shows exactly what to fuse/level."
    ),
    "flag_vc": (
        "+vc — print each card's estimated value in the result deck.\n"
        "Useful for seeing which cards contribute most."
    ),
    "flag_ci": "+ci — show a confidence interval alongside the win rate.",
    "flag_v": "+v — verbose output. Lots of text; useful for debugging.",
    "extra_flags": (
        "Any extra tuo flags appended to the command, e.g.\n"
        "'no-db mis 0.02 cl 0.98 +hm'.\n"
        "See Help → TUO flags wiki for the full list."
    ),
    "preset": (
        "Recommended settings per event — pick one and the form fills itself.\n"
        "Only event-related fields change; your deck, inventory file and\n"
        "threads are never touched.\n\n"
        "'Save as…' stores the current form as a preset. Presets live in\n"
        "tuo_gui_data\\presets.json using plain tuo values (pvp, climb,\n"
        "dom-owned, endgame 2…) and can also be edited by hand."
    ),
    "accounts_list": (
        "Accounts added via Accounts → Add account.\n"
        "Their decks and owned cards come from Accounts → Update owned cards & decks.\n\n"
        "Select one and 'Load into form' to fill My Deck + inventory, or select\n"
        "several (Ctrl+click) and 'Sim selected' to run the current sim settings\n"
        "once per account."
    ),
    "deck_type": (
        "Which of the account's saved decks to sim:\n"
        "Attack — the deck in the game's attack slot\n"
        "Defence — the deck in the game's defense slot"
    ),
    "load_account": (
        "Fill 'My Deck' and the owned-cards file from the selected account,\n"
        "using the deck type chosen above. You can then tweak and run normally."
    ),
    "sim_selected": (
        "Run the current sim settings once per selected account, one after\n"
        "another. Each run uses that account's saved deck (attack or defence,\n"
        "as chosen above) and that account's owned-cards file.\n"
        "Stop cancels the current run and skips the rest."
    ),
}


# ---------------------------------------------------------------------------
# Fort parsing
# ---------------------------------------------------------------------------

def resolve_fort(token: str) -> tuple[str, str]:
    """Token -> (canonical full name with optional ' #N', 'siege' | 'defense')."""
    raw = token.strip()
    if not raw:
        raise ValueError("Empty fort name")

    suffix = ""
    base = raw
    if "#" in raw:
        base, _, num = raw.partition("#")
        base = base.strip()
        num = num.strip()
        if num:
            suffix = f" #{num}"

    base_upper = base.upper()
    base_lower = base.lower()

    if base_upper in SIEGE_FORTS:
        return SIEGE_FORTS[base_upper] + suffix, "siege"
    if base_upper in DEFENSE_FORTS:
        return DEFENSE_FORTS[base_upper] + suffix, "defense"
    if base_lower in SIEGE_FULL_LOOKUP:
        return SIEGE_FULL_LOOKUP[base_lower] + suffix, "siege"
    if base_lower in DEFENSE_FULL_LOOKUP:
        return DEFENSE_FULL_LOOKUP[base_lower] + suffix, "defense"

    raise ValueError(
        f"Unknown fort: '{raw}'. Valid abbreviations: "
        f"{', '.join(sorted(list(SIEGE_FORTS) + list(DEFENSE_FORTS)))}"
    )


def strip_dropdown(s: str) -> str:
    """Convert 'CS (Corrosive Spore)' back to 'CS'. Pass through other strings."""
    if s.strip() in (SIEGE_HEADER, DEFENSE_HEADER):
        return ""  # group headers are not forts
    if "(" in s:
        return s.split("(", 1)[0].strip()
    return s.strip()


# ---------------------------------------------------------------------------
# Config + command builder
# ---------------------------------------------------------------------------

@dataclass
class SimConfig:
    my_deck: str = ""
    enemy_deck: str = ""
    my_forts: list[str] = field(default_factory=list)
    enemy_forts: list[str] = field(default_factory=list)
    effect: str = "none"
    y_effect: str = ""
    e_effect: str = ""
    mode: str = "pvp"
    order: str = "random"
    operation: str = "climb"
    iterations: int = 1000
    climbex_initial: int = 10
    climbex_final: int = 1000
    anneal_iter: int = 1000
    anneal_temp: float = 100.0
    anneal_decay: float = 0.001
    dominion: str = "dom-owned"
    endgame: str | None = "2"
    fund: int = 0
    owned_cards_path: str = ""
    use_owned: bool = True
    deck_min: int | None = 10
    deck_max: int | None = 10
    threads: int = 4
    seed: int | None = None
    timeout_hours: float | None = None
    keep_commander: bool = False
    no_db: bool = True
    short_output: bool = True
    print_upgraded: bool = True
    print_card_values: bool = False
    print_ci: bool = False
    verbose: bool = False
    extra_flags: str = ""
    binary: str = "tuo.exe"


def build_command(cfg: SimConfig) -> list[str]:
    if not cfg.my_deck:
        raise ValueError("My Deck is required.")
    if not cfg.enemy_deck:
        raise ValueError("Enemy Deck is required.")

    cmd: list[str] = [cfg.binary, cfg.my_deck, cfg.enemy_deck, cfg.mode, cfg.order]

    if cfg.my_forts:
        cmd += ["yf", ", ".join(cfg.my_forts)]
    if cfg.enemy_forts:
        cmd += ["ef", ", ".join(cfg.enemy_forts)]

    if cfg.effect and cfg.effect.lower() != "none":
        check_effect_placeholders(cfg.effect, "BG Effect")
        cmd += ["-e", cfg.effect]
    if cfg.y_effect:
        check_effect_placeholders(cfg.y_effect, "Your war effect")
        cmd += ["ye", cfg.y_effect]
    if cfg.e_effect:
        check_effect_placeholders(cfg.e_effect, "Enemy war effect")
        cmd += ["ee", cfg.e_effect]

    if cfg.threads and cfg.threads != 4:
        cmd += ["-t", str(cfg.threads)]

    if cfg.endgame is not None:
        cmd += ["endgame", str(cfg.endgame)]

    if cfg.fund and cfg.fund > 0:
        cmd += ["fund", str(cfg.fund)]

    if cfg.use_owned:
        if cfg.owned_cards_path:
            # tuo ignores owned-cards files outside its data folder
            p = Path(cfg.owned_cards_path)
            if DATA_DIR.resolve() not in p.resolve().parents:
                raise ValueError(
                    f"Owned-cards file '{cfg.owned_cards_path}' is not in the "
                    "data folder — tuo would ignore it. Use Browse…, which "
                    "offers to copy it there."
                )
            if not p.exists():
                raise ValueError(f"Owned-cards file not found: {cfg.owned_cards_path}")
            cmd.append(f'-o={cfg.owned_cards_path}')
        else:
            cmd.append("-o")
    else:
        cmd.append("-o-")

    cmd.append(cfg.dominion)

    # -L FROM TO: deck size limits, each 1-10, FROM <= TO (equal is fine)
    if (cfg.deck_min is None) != (cfg.deck_max is None):
        raise ValueError("Deck size: fill in both values (or leave both empty).")
    if cfg.deck_min is not None and cfg.deck_max is not None:
        if not (1 <= cfg.deck_min <= 10) or not (1 <= cfg.deck_max <= 10):
            raise ValueError("Deck size: values must be between 1 and 10.")
        if cfg.deck_min > cfg.deck_max:
            raise ValueError(
                f"Deck size: 'from' ({cfg.deck_min}) cannot be larger than "
                f"'to' ({cfg.deck_max})."
            )
        cmd += ["-L", str(cfg.deck_min), str(cfg.deck_max)]

    if cfg.keep_commander:
        cmd.append("-c")

    if cfg.seed is not None:
        cmd += ["seed", str(cfg.seed)]

    if cfg.timeout_hours is not None and cfg.timeout_hours > 0:
        cmd += ["timeout", str(round(cfg.timeout_hours, 4))]

    if cfg.no_db:
        cmd.append("no-db")

    if cfg.short_output:
        cmd.append("+so")
    if cfg.print_upgraded:
        cmd.append("+uc")
    if cfg.print_card_values:
        cmd.append("+vc")
    if cfg.print_ci:
        cmd.append("+ci")
    if cfg.verbose:
        cmd.append("+v")

    op = cfg.operation
    if op in ("climb", "sim", "reorder", "climb_forts", "genetic", "beam"):
        cmd += [op, str(cfg.iterations)]
    elif op == "climbex":
        cmd += ["climbex", str(cfg.climbex_initial), str(cfg.climbex_final)]
    elif op == "anneal":
        cmd += ["anneal", str(cfg.anneal_iter), str(cfg.anneal_temp), str(cfg.anneal_decay)]
    elif op == "debug sim":
        cmd += ["debug", "sim"]
    else:
        cmd.append(op)

    if cfg.extra_flags.strip():
        cmd += shlex.split(cfg.extra_flags)

    return cmd


def format_command(cmd: list[str]) -> str:
    out = []
    for part in cmd:
        if " " in part or "," in part or part == "":
            out.append(f'"{part}"')
        else:
            out.append(part)
    return " ".join(out)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SimRunnerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TUO Sim Runner")
        root.geometry("1180x660")
        root.minsize(1024, 600)

        self.process: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.updating_xmls = False
        self.updating_inventories = False
        self.batch_running = False
        self.batch_stop = False

        self._build_menubar()
        self._build_ui()
        self._load_settings()
        self._update_operation_params()
        self._poll_output()
        root.after(600, self._first_run_check)

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------
    def _build_menubar(self) -> None:
        menubar = tk.Menu(self.root)

        data_menu = tk.Menu(menubar, tearoff=0)
        data_menu.add_command(label="Update XMLs (game data)", command=self._update_xmls)
        data_menu.add_command(label="Open data folder", command=self._open_data_folder)
        menubar.add_cascade(label="Data", menu=data_menu)

        acct_menu = tk.Menu(menubar, tearoff=0)
        acct_menu.add_command(label="Add account…", command=self._add_account_dialog)
        acct_menu.add_command(
            label="Update owned cards & decks", command=self._update_inventories,
        )
        menubar.add_cascade(label="Accounts", menu=acct_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(
            label="TUO flags wiki",
            command=lambda: webbrowser.open(FLAGS_WIKI_URL),
        )
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _open_data_folder(self) -> None:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            if os.name == "nt":
                os.startfile(DATA_DIR.resolve())  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(DATA_DIR.resolve())])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open data folder: {e}")

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def _add_account_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Add account")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.columnconfigure(0, weight=1)
        dlg.rowconfigure(1, weight=1)

        existing = list_accounts()
        header = ACCOUNT_INSTRUCTIONS
        if existing:
            header += "\n\nAccounts already added: " + ", ".join(existing)
        ttk.Label(dlg, text=header, justify="left").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 6),
        )

        paste_box = tk.Text(dlg, height=14, width=86)
        paste_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        paste_box.focus_set()

        def on_save():
            try:
                creds = parse_api_post_data(paste_box.get("1.0", "end"))
                kong_name = save_account(creds)
            except ValueError as e:
                messagebox.showerror("Could not save account", str(e), parent=dlg)
                return
            dlg.destroy()
            if messagebox.askyesno(
                "Account saved",
                f"Account '{kong_name}' saved.\n\nDownload owned cards & decks "
                "for all accounts now?",
            ):
                self._update_inventories()

        btns = ttk.Frame(dlg)
        btns.grid(row=2, column=0, sticky="e", padx=10, pady=(4, 10))
        ttk.Button(btns, text="Save account", command=on_save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    def _update_inventories(self) -> None:
        if self.updating_inventories:
            messagebox.showinfo("Busy", "An account update is already running.")
            return
        if self.process is not None or self.updating_xmls or self.batch_running:
            messagebox.showinfo("Busy", "Wait for the current task to finish first.")
            return
        accounts = list_accounts()
        if not accounts:
            if messagebox.askyesno(
                "No accounts",
                "No accounts added yet. Add one now?",
            ):
                self._add_account_dialog()
            return
        if not Path(INVENTORY_SCRIPT).exists():
            messagebox.showerror(
                "Missing script",
                f"{INVENTORY_SCRIPT} was not found next to the GUI.",
            )
            return

        self.updating_inventories = True
        self.run_btn.configure(state="disabled")
        self.status_var.set("Updating owned cards & decks…")
        self.notebook.select(self.output_tab)
        self._append_output(
            f"\n=== Updating owned cards & decks for: {', '.join(accounts)} ===\n"
        )
        threading.Thread(target=self._update_inventories_worker, daemon=True).start()

    def _update_inventories_worker(self) -> None:
        if getattr(sys, "frozen", False):
            # Packaged exe: there's no separate Python to spawn — run the
            # bundled tu_inventory module in-process instead.
            self._update_inventories_inprocess()
            return
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [sys.executable, INVENTORY_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
        except Exception as e:
            self.output_queue.put(f"[ERROR] Could not launch {INVENTORY_SCRIPT}: {e}\n")
            self.output_queue.put("__INV_DONE__:1")
            return
        assert proc.stdout is not None
        while True:
            try:
                chunk = proc.stdout.read(4096)
            except Exception:
                break
            if not chunk:
                break
            self.output_queue.put(chunk.decode("utf-8", errors="replace"))
        rc = proc.wait()
        self.output_queue.put(f"__INV_DONE__:{rc}")

    def _update_inventories_inprocess(self) -> None:
        """Frozen-exe path: import tu_inventory and run it here, with its
        prints redirected into the Output tab."""
        import contextlib

        class _QueueWriter:
            def __init__(self, q):
                self._q = q

            def write(self, text):
                if text:
                    self._q.put(text)

            def flush(self):
                pass

        rc = 0
        writer = _QueueWriter(self.output_queue)
        try:
            import tu_inventory
            cwd = Path.cwd()
            # tu_inventory derives its paths from __file__, which points into
            # the bundle when frozen — repoint everything at the app folder.
            tu_inventory.SCRIPT_DIR = str(cwd)
            tu_inventory.GUI_DATA_DIR = str(cwd / "tuo_gui_data")
            tu_inventory.COOKIES_DIR = str(cwd / "tuo_gui_data" / "cookies")
            tu_inventory.DATA_DIR = tu_inventory.GUI_DATA_DIR
            tu_inventory.XML_DIR = str(cwd / "data")
            tu_inventory.PLAYERS_JSON = os.path.join(tu_inventory.DATA_DIR, "players.json")
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                cards = tu_inventory.load_all_cards(tu_inventory.XML_DIR)
                if not cards:
                    print("ERROR: no cards loaded — run Data → Update XMLs first")
                    rc = 1
                else:
                    names = tu_inventory.list_kong_names()
                    with tu_inventory.PoolManager(
                        1,
                        timeout=tu_inventory.Timeout(connect=15.0, read=20.0, total=30.0),
                        retries=tu_inventory.Retry(total=3),
                        cert_reqs="CERT_REQUIRED",
                        ca_certs=tu_inventory.certifi.where(),
                    ) as http:
                        for name in names:
                            try:
                                tu_inventory.export_for_user(name, cards, http)
                            except Exception as e:
                                print("ERROR: failed for {}: {}".format(name, e))
                                rc = 1
        except SystemExit as e:
            rc = int(e.code or 0)
        except Exception as e:
            self.output_queue.put(f"[ERROR] Inventory update failed: {e}\n")
            rc = 1
        self.output_queue.put(f"__INV_DONE__:{rc}")

    # ------------------------------------------------------------------
    # XML updating
    # ------------------------------------------------------------------
    def _first_run_check(self) -> None:
        """Offer to download game data when it's missing (fresh install)."""
        if (DATA_DIR / "cards_section_1.xml").exists():
            return
        if messagebox.askyesno(
            "Game data missing",
            "The game data (XML files) hasn't been downloaded yet — tuo can't "
            "run without it.\n\nDownload it now? (You can always rerun this "
            "via Data → Update XMLs.)",
        ):
            self._update_xmls(ask=False)

    def _update_xmls(self, ask: bool = True) -> None:
        if self.updating_xmls:
            messagebox.showinfo("Busy", "An XML update is already running.")
            return
        if self.process is not None or self.updating_inventories or self.batch_running:
            messagebox.showinfo("Busy", "Wait for the current task to finish first.")
            return
        if ask and not messagebox.askyesno(
            "Update XMLs",
            f"Download the latest game data ({len(XML_FILES)} files) into the "
            f"'{DATA_DIR}' folder? Existing files are replaced.",
        ):
            return

        self.updating_xmls = True
        self.run_btn.configure(state="disabled")
        self.status_var.set("Updating XMLs…")
        self.notebook.select(self.output_tab)
        self._append_output(f"\n=== Updating game data from {XML_BASE_URL} ===\n")
        threading.Thread(target=self._update_xmls_worker, daemon=True).start()

    def _update_xmls_worker(self) -> None:
        try:
            DATA_DIR.mkdir(exist_ok=True)
        except Exception as e:
            self.output_queue.put(f"[ERROR] Could not create folder: {e}\n")
            self.output_queue.put("__XML_DONE__:0:1")
            return

        # One-time cleanup of leftovers from older tooling: backed-up XMLs
        # and stale parse caches. Only the fresh XMLs are kept.
        try:
            if (DATA_DIR / "old-xmls.d").is_dir():
                shutil.rmtree(DATA_DIR / "old-xmls.d", ignore_errors=True)
                self.output_queue.put("Removed legacy backup folder data/old-xmls.d\n")
            for pck in DATA_DIR.glob("*.pck"):
                pck.unlink(missing_ok=True)
        except Exception:
            pass

        ok = failed = 0
        for i, fname in enumerate(XML_FILES, 1):
            url = XML_BASE_URL + fname
            tmp_file = DATA_DIR / f".{fname}~"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "tuo-gui"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    content = resp.read()
                tmp_file.write_bytes(content)
                tmp_file.replace(DATA_DIR / fname)
                # Drop tu_inventory.py's now-stale parse cache for this file —
                # it's rebuilt automatically on the next account update.
                if fname.endswith(".xml"):
                    (DATA_DIR / (fname[:-4] + ".pck")).unlink(missing_ok=True)
                ok += 1
                self.output_queue.put(
                    f"[{i}/{len(XML_FILES)}] {fname} — OK ({len(content):,} bytes)\n"
                )
            except Exception as e:
                failed += 1
                self.output_queue.put(f"[{i}/{len(XML_FILES)}] {fname} — FAILED: {e}\n")
                try:
                    tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass
        self.output_queue.put(f"__XML_DONE__:{ok}:{failed}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Two tabs: Sim (everything) and Output
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.sim_tab = ttk.Frame(nb)
        self.output_tab = ttk.Frame(nb)
        self.results_tab = ttk.Frame(nb)
        nb.add(self.sim_tab, text="Sim")
        nb.add(self.output_tab, text="Output")
        nb.add(self.results_tab, text="Results")
        self.notebook = nb

        self._build_sim_tab(self.sim_tab)
        self._build_output_tab(self.output_tab)
        self._build_results_tab(self.results_tab)
        self._build_bottom_bar()

    @staticmethod
    def _autowiden(cb: ttk.Combobox) -> ttk.Combobox:
        """Make the popup list wide enough for its longest entry, even when
        the combobox itself is narrow."""
        def on_post():
            values = cb.cget("values")
            if values:
                width = max(len(str(v)) for v in values) + 2
                try:
                    popdown = cb.tk.call("ttk::combobox::PopdownWindow", cb)
                    cb.tk.call(f"{popdown}.f.l", "configure", "-width", width)
                except tk.TclError:
                    pass
        cb.configure(postcommand=on_post)
        return cb

    @staticmethod
    def _option_label(value, opts) -> str:
        """Accept either a tuo flag value ('pvp-defense') or a GUI label
        ('Battle (defense)') and return the GUI label."""
        sval = str(value)
        for label, flag in opts:
            if sval == label or sval == str(flag):
                return label
        return opts[0][0]

    @staticmethod
    def _option_value(label, opts) -> str:
        """GUI label -> tuo flag value (what presets store)."""
        for n, v in opts:
            if label == n:
                return "none" if v is None else str(v)
        return str(label)

    def _on_preset_selected(self, _event=None) -> None:
        name = self.preset_var.get()
        preset = self.presets.get(name)
        if not preset:
            return
        self._apply_preset_settings(preset.get("settings", {}))
        hint = preset.get("description", "")
        self.preset_hint_var.set(hint)
        self.status_var.set(f"Preset applied: {name}")

    def _collect_preset_settings(self) -> dict:
        """Current form -> preset settings dict, in tuo-flag terms. Personal
        fields (My Deck, inventory file, threads) are deliberately excluded."""
        return {
            "enemy_deck": self.enemy_deck_text.get("1.0", "end").strip().replace("\n", " "),
            "deck_type": self.deck_type_var.get(),
            "mode": self._option_value(self.mode_var.get(), MODES),
            "order": self._option_value(self.order_var.get(), ORDERS),
            "operation": self._option_value(self.operation_var.get(), OPERATIONS),
            "iterations": self.iter_var.get(),
            "climbex_init": self.climbex_init_var.get(),
            "climbex_final": self.climbex_final_var.get(),
            "anneal_iter": self.anneal_iter_var.get(),
            "anneal_temp": self.anneal_temp_var.get(),
            "anneal_decay": self.anneal_decay_var.get(),
            "effect": self.effect_var.get().strip() or "none",
            "my_fort1": strip_dropdown(self.my_fort1_var.get()),
            "my_fort2": strip_dropdown(self.my_fort2_var.get()),
            "enemy_fort1": strip_dropdown(self.enemy_fort1_var.get()),
            "enemy_fort2": strip_dropdown(self.enemy_fort2_var.get()),
            "my_effect": strip_war_effect(self.my_effect_var.get()),
            "enemy_effect": strip_war_effect(self.enemy_effect_var.get()),
            "dominion": next(
                (v for n, v in DOMINION_OPTS if n == self.dominion_var.get()),
                "dom-owned",
            ),
            "endgame": self._option_value(self.endgame_var.get(), ENDGAME_OPTS),
            "deck_min": self.deck_min_var.get(),
            "deck_max": self.deck_max_var.get(),
            "use_owned": self.use_owned_var.get(),
            "fund": self.fund_var.get(),
            "timeout": self.timeout_var.get(),
            "keep_commander": self.keep_commander_var.get(),
            "no_db": self.no_db_var.get(),
            "so": self.so_var.get(), "uc": self.uc_var.get(),
            "vc": self.vc_var.get(), "ci": self.ci_var.get(),
            "verbose": self.verbose_var.get(),
            "extra": self.extra_var.get().strip(),
        }

    def _write_presets(self, mutate) -> bool:
        """Load the full presets file, apply mutate(presets_dict), save."""
        raw = None
        for path in (PRESETS_FILE, LEGACY_PRESETS_FILE):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("presets", {})
        mutate(raw["presets"])
        try:
            GUI_DATA_DIR.mkdir(exist_ok=True)
            PRESETS_FILE.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8",
            )
        except OSError as e:
            messagebox.showerror("Error", f"Could not save presets: {e}")
            return False
        return True

    def _refresh_presets(self, select: str = "") -> None:
        self.presets = load_presets()
        self.preset_cb.configure(values=list(self.presets))
        self.preset_var.set(select)
        if select and select in self.presets:
            self.preset_hint_var.set(self.presets[select].get("description", ""))
        elif not select:
            self.preset_hint_var.set("Pick an event to apply recommended settings.")

    def _save_preset_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Save preset")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.columnconfigure(1, weight=1)

        ttk.Label(dlg, text="Name:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        name_var = tk.StringVar(value=self.preset_var.get())
        ttk.Entry(dlg, textvariable=name_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6)

        ttk.Label(dlg, text="Description:").grid(row=1, column=0, sticky="ne", padx=6, pady=6)
        desc_box = tk.Text(dlg, width=60, height=3, wrap="word")
        existing = self.presets.get(self.preset_var.get(), {})
        desc_box.insert("1.0", existing.get("description", ""))
        desc_box.grid(row=1, column=1, sticky="ew", padx=6, pady=6)

        ttk.Label(
            dlg, text="Saves the current form (except My Deck, inventory file "
                      "and threads) to tuo_gui_data\\presets.json.",
            foreground="#555", wraplength=420, justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6)

        def on_save():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Name required", "Give the preset a name.", parent=dlg)
                return
            if name in self.presets and not messagebox.askyesno(
                "Overwrite?", f"Preset '{name}' already exists — overwrite it?",
                parent=dlg,
            ):
                return
            entry = {
                "draft": False,
                "description": desc_box.get("1.0", "end").strip(),
                "settings": self._collect_preset_settings(),
            }
            if self._write_presets(lambda p: p.__setitem__(name, entry)):
                self._refresh_presets(select=name)
                self.status_var.set(f"Preset saved: {name}")
                dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=3, column=1, sticky="e", padx=6, pady=(4, 8))
        ttk.Button(btns, text="Save", command=on_save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    def _delete_preset(self) -> None:
        name = self.preset_var.get()
        if name not in self.presets:
            messagebox.showinfo("No preset", "Select a preset to delete first.")
            return
        if not messagebox.askyesno("Delete preset", f"Delete preset '{name}'?"):
            return
        if self._write_presets(lambda p: p.pop(name, None)):
            self._refresh_presets()
            self.status_var.set(f"Preset deleted: {name}")

    def _apply_preset_settings(self, s: dict) -> None:
        """Apply preset keys to the form. Keys absent from the preset are left
        untouched (so personal fields survive); empty strings clear fields."""
        option_fields = {
            "mode": (self.mode_var, MODES),
            "order": (self.order_var, ORDERS),
            "operation": (self.operation_var, OPERATIONS),
            "endgame": (self.endgame_var, ENDGAME_OPTS),
        }
        string_vars = {
            "effect": self.effect_var,
            "iterations": self.iter_var,
            "climbex_init": self.climbex_init_var,
            "climbex_final": self.climbex_final_var,
            "anneal_iter": self.anneal_iter_var,
            "anneal_temp": self.anneal_temp_var,
            "anneal_decay": self.anneal_decay_var,
            "deck_min": self.deck_min_var, "deck_max": self.deck_max_var,
            "threads": self.threads_var, "timeout": self.timeout_var,
            "fund": self.fund_var, "extra": self.extra_var,
            "owned_path": self.owned_path_var,
            "my_fort1": self.my_fort1_var, "my_fort2": self.my_fort2_var,
            "enemy_fort1": self.enemy_fort1_var, "enemy_fort2": self.enemy_fort2_var,
            "my_effect": self.my_effect_var, "enemy_effect": self.enemy_effect_var,
            "deck_type": self.deck_type_var,
        }
        bool_vars = {
            "use_owned": self.use_owned_var,
            "keep_commander": self.keep_commander_var,
            "no_db": self.no_db_var,
            "so": self.so_var, "uc": self.uc_var, "vc": self.vc_var,
            "ci": self.ci_var, "verbose": self.verbose_var,
        }
        for key, value in s.items():
            if key == "my_deck":
                self.my_deck_text.delete("1.0", "end")
                self.my_deck_text.insert("1.0", str(value))
            elif key == "enemy_deck":
                self.enemy_deck_text.delete("1.0", "end")
                self.enemy_deck_text.insert("1.0", str(value))
            elif key == "dominion":
                label = next(
                    (n for n, v in DOMINION_OPTS
                     if n == value or str(value).startswith(v)),
                    DOMINION_OPTS[0][0],
                )
                self.dominion_var.set(label)
            elif key in option_fields:
                var, opts = option_fields[key]
                var.set(self._option_label(value, opts))
            elif key in bool_vars:
                bool_vars[key].set(bool(value))
            elif key in string_vars:
                string_vars[key].set(str(value))
            # unknown keys are ignored — forward compatibility
        self._update_operation_params()

    def _fort_combo(self, parent: tk.Widget, var: tk.StringVar) -> ttk.Combobox:
        """Fort dropdown with group headers; picking a header clears the box."""
        cb = ttk.Combobox(
            parent, textvariable=var, values=FORT_CHOICES, width=22, height=16,
        )

        def on_select(_event, v=var):
            if v.get().strip() in (SIEGE_HEADER, DEFENSE_HEADER):
                v.set("")

        cb.bind("<<ComboboxSelected>>", on_select)
        Tooltip(cb, TIPS["forts"])
        return self._autowiden(cb)

    def _war_effect_combo(
        self, parent: tk.Widget, var: tk.StringVar, tip: str,
    ) -> ttk.Combobox:
        """War effect dropdown (yeffect/eeffect) fed by data/bges.txt.

        Entries show 'Name — effects' so users can verify the effects in
        bges.txt still match the game (nerfs/buffs). Only the name is passed
        to tuo.
        """
        values = [""] + [
            f"{name}{WAR_EFFECT_SEP}{self.named_bges[name]}"
            for name in sorted(self.named_bges)
        ]
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=22, height=20)
        Tooltip(cb, tip)
        self._autowiden(cb)
        return cb

    def _build_sidebar(self, frame: ttk.LabelFrame) -> None:
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.account_list = tk.Listbox(
            frame, selectmode="extended", width=18, exportselection=False,
        )
        self.account_list.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 2))
        Tooltip(self.account_list, TIPS["accounts_list"])

        self.deck_type_var = tk.StringVar(value="attack")
        rb_frame = ttk.Frame(frame)
        rb_frame.grid(row=1, column=0, sticky="w", padx=4, pady=2)
        for text, value in (("Attack deck", "attack"), ("Defence deck", "defence")):
            rb = ttk.Radiobutton(
                rb_frame, text=text, value=value, variable=self.deck_type_var,
            )
            rb.pack(anchor="w")
            Tooltip(rb, TIPS["deck_type"])

        load_btn = ttk.Button(frame, text="Load into form", command=self._load_account_into_form)
        load_btn.grid(row=2, column=0, sticky="ew", padx=4, pady=2)
        Tooltip(load_btn, TIPS["load_account"])

        sim_btn = ttk.Button(frame, text="Sim selected", command=self._run_batch_clicked)
        sim_btn.grid(row=3, column=0, sticky="ew", padx=4, pady=(2, 6))
        Tooltip(sim_btn, TIPS["sim_selected"])

        self._refresh_accounts_list()

    def _refresh_accounts_list(self) -> None:
        names = sorted(set(list_accounts()) | all_deck_account_names())
        self.account_list.delete(0, "end")
        for name in names:
            self.account_list.insert("end", name)

    def _selected_accounts(self) -> list[str]:
        return [self.account_list.get(i) for i in self.account_list.curselection()]

    def _load_account_into_form(self) -> None:
        selected = self._selected_accounts()
        if not selected:
            messagebox.showinfo("No account", "Select an account in the list first.")
            return
        name = selected[0]
        deck_type = self.deck_type_var.get()
        deck = load_account_deck(name, deck_type)
        if deck is None:
            messagebox.showwarning(
                "No deck on file",
                f"No {deck_type} deck found for '{name}'.\n"
                "Run Accounts → Update owned cards & decks first.",
            )
            return
        self.my_deck_text.delete("1.0", "end")
        self.my_deck_text.insert("1.0", deck)
        owned = account_owned_file(name)
        if owned.exists():
            self.owned_path_var.set(str(owned))
        else:
            messagebox.showwarning(
                "No inventory on file",
                f"{owned} not found — run Accounts → Update owned cards & decks.\n"
                "Deck was loaded anyway.",
            )
        self.status_var.set(f"Loaded {name}'s {deck_type} deck.")

    def _run_batch_clicked(self) -> None:
        if self.process is not None or self.batch_running:
            messagebox.showinfo("Busy", "A sim is already running.")
            return
        if self.updating_xmls or self.updating_inventories:
            messagebox.showinfo("Busy", "Wait for the running update to finish first.")
            return
        selected = self._selected_accounts()
        if not selected:
            messagebox.showinfo(
                "No accounts",
                "Select one or more accounts in the list (Ctrl+click for several).",
            )
            return
        deck_type = self.deck_type_var.get()

        problems = []
        for name in selected:
            if load_account_deck(name, deck_type) is None:
                problems.append(f"• {name} — no {deck_type} deck on file")
            if not account_owned_file(name).exists():
                problems.append(f"• {name} — no owned-cards file")
        if problems:
            messagebox.showerror(
                "Missing account data",
                "Fix these first (Accounts → Update owned cards & decks):\n\n"
                + "\n".join(problems),
            )
            return

        try:
            cfg = self._harvest_config()
            jobs = []
            for name in selected:
                job_cfg = dataclasses.replace(
                    cfg,
                    my_deck=load_account_deck(name, deck_type),
                    owned_cards_path=str(account_owned_file(name)),
                    use_owned=True,
                )
                jobs.append((name, build_command(job_cfg), cfg.operation))
        except ValueError as e:
            messagebox.showerror("Validation error", str(e))
            return

        self._save_settings()
        self.batch_running = True
        self.batch_stop = False
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"Simming {len(jobs)} account(s)…")
        self.notebook.select(self.output_tab)
        threading.Thread(target=self._run_batch_worker, args=(jobs,), daemon=True).start()

    def _run_batch_worker(self, jobs: list[tuple[str, list[str], str]]) -> None:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        summary: list[tuple[str, str]] = []
        for i, (name, cmd, op_label) in enumerate(jobs, 1):
            if self.batch_stop:
                self.output_queue.put("\n[Batch stopped — remaining accounts skipped]\n")
                break
            self.output_queue.put(
                f"\n===== [{i}/{len(jobs)}] {name} =====\n$ {format_command(cmd)}\n\n"
            )
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    creationflags=creationflags,
                )
            except Exception as e:
                self.output_queue.put(f"[ERROR] Could not launch tuo for {name}: {e}\n")
                summary.append((name, f"ERROR: could not launch tuo ({e})"))
                continue
            assert self.process.stdout is not None
            stream = self.process.stdout
            captured = ""
            while True:
                try:
                    chunk = stream.read(4096)
                except Exception:
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                captured = (captured + text)[-200_000:]  # keep the tail
                self.output_queue.put(text)
            rc = self.process.wait()
            self.process = None
            self.output_queue.put(f"\n[{name}: exited with code {rc}]\n")
            if rc != 0:
                self.output_queue.put(f"[Hint] {EXIT_HINTS.get(str(rc), GENERIC_FAIL_HINT)}\n")

            result = extract_result_lines(captured)
            if rc != 0 and not any(result.values()):
                result["error"] = f"tuo exited with code {rc}"
            append_result_block(name, op_label, result)
            summary.append((name, summarize_result(result)))

        if summary:
            width = max(len(n) for n, _ in summary)
            lines = "\n".join(f"  {n.ljust(width)}  {s}" for n, s in summary)
            self.output_queue.put(f"\n===== Batch summary =====\n{lines}\n")
        self.output_queue.put("__BATCH_DONE__")

    def _build_sim_tab(self, parent: ttk.Frame) -> None:
        # War effects (per-side, used in wars) come from data/bges.txt
        self.named_bges = load_named_bges()
        # SIDEBAR (accounts) | TOP (full width sides) above two settings columns
        parent.columnconfigure(0, weight=0)
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(2, weight=1)
        parent.rowconfigure(1, weight=1)

        sidebar = ttk.LabelFrame(parent, text="Accounts")
        sidebar.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(4, 2), pady=4)
        self._build_sidebar(sidebar)

        top = ttk.Frame(parent)
        top.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        top.columnconfigure(0, weight=1)

        left = ttk.Frame(parent)
        left.grid(row=1, column=1, sticky="nsew", padx=(4, 4), pady=4)
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(parent)
        right.grid(row=1, column=2, sticky="nsew", padx=(4, 4), pady=4)
        right.columnconfigure(0, weight=1)

        # =========================================================
        # TOP — Event preset, then My/Enemy side, full width
        # =========================================================
        self.presets = load_presets()
        preset_frame = ttk.LabelFrame(top, text="Event preset")
        preset_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        preset_frame.columnconfigure(1, weight=1)
        self.preset_var = tk.StringVar()
        preset_cb = ttk.Combobox(
            preset_frame, textvariable=self.preset_var, state="readonly",
            values=list(self.presets), width=24,
        )
        preset_cb.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        preset_cb.bind("<<ComboboxSelected>>", self._on_preset_selected)
        Tooltip(preset_cb, TIPS["preset"])
        self._autowiden(preset_cb)
        self.preset_cb = preset_cb
        self.preset_hint_var = tk.StringVar(
            value="Pick an event to apply recommended settings."
        )
        ttk.Label(
            preset_frame, textvariable=self.preset_hint_var,
            foreground="#555", wraplength=680, justify="left",
        ).grid(row=0, column=1, sticky="w", padx=(8, 4), pady=4)
        save_btn = ttk.Button(preset_frame, text="Save as…", command=self._save_preset_dialog)
        save_btn.grid(row=0, column=2, sticky="e", padx=2, pady=4)
        Tooltip(save_btn, "Save the current form as a preset (My Deck, inventory "
                          "file and threads are not stored).")
        del_btn = ttk.Button(preset_frame, text="Delete", command=self._delete_preset)
        del_btn.grid(row=0, column=3, sticky="e", padx=(2, 4), pady=4)
        Tooltip(del_btn, "Delete the selected preset from tuo_gui_data\\presets.json.")

        my_frame = ttk.LabelFrame(top, text="My side")
        my_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        my_frame.columnconfigure(1, weight=1)
        my_frame.columnconfigure(3, weight=1)

        ttk.Label(my_frame, text="Deck:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.my_deck_text = tk.Text(my_frame, height=2, wrap="word")
        self.my_deck_text.grid(row=0, column=1, columnspan=3, sticky="ew", padx=4, pady=4)
        Tooltip(self.my_deck_text, TIPS["my_deck"])

        ttk.Label(my_frame, text="Fort 1:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.my_fort1_var = tk.StringVar()
        self._fort_combo(my_frame, self.my_fort1_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(my_frame, text="Fort 2:").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        self.my_fort2_var = tk.StringVar()
        self._fort_combo(my_frame, self.my_fort2_var).grid(
            row=1, column=3, sticky="ew", padx=4, pady=4)

        ttk.Label(my_frame, text="War effect:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.my_effect_var = tk.StringVar()
        self._war_effect_combo(
            my_frame, self.my_effect_var, TIPS["war_effect_mine"],
        ).grid(row=2, column=1, columnspan=3, sticky="ew", padx=4, pady=4)

        enemy_frame = ttk.LabelFrame(top, text="Enemy side")
        enemy_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        enemy_frame.columnconfigure(1, weight=1)
        enemy_frame.columnconfigure(3, weight=1)

        ttk.Label(enemy_frame, text="Deck:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.enemy_deck_text = tk.Text(enemy_frame, height=2, wrap="word")
        self.enemy_deck_text.grid(row=0, column=1, columnspan=3, sticky="ew", padx=4, pady=4)
        Tooltip(self.enemy_deck_text, TIPS["enemy_deck"])

        ttk.Label(enemy_frame, text="Fort 1:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.enemy_fort1_var = tk.StringVar()
        self._fort_combo(enemy_frame, self.enemy_fort1_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(enemy_frame, text="Fort 2:").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        self.enemy_fort2_var = tk.StringVar()
        self._fort_combo(enemy_frame, self.enemy_fort2_var).grid(
            row=1, column=3, sticky="ew", padx=4, pady=4)

        ttk.Label(enemy_frame, text="War effect:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        self.enemy_effect_var = tk.StringVar()
        self._war_effect_combo(
            enemy_frame, self.enemy_effect_var, TIPS["war_effect_enemy"],
        ).grid(row=2, column=1, columnspan=3, sticky="ew", padx=4, pady=4)

        ttk.Label(
            top,
            text="Tip: hover over any field for an explanation.",
            foreground="#777", justify="left",
        ).grid(row=3, column=0, sticky="w", padx=4, pady=(0, 4))

        # =========================================================
        # LEFT COLUMN — sim settings, operation
        # =========================================================
        sim_frame = ttk.LabelFrame(left, text="Sim settings")
        sim_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        sim_frame.columnconfigure(1, weight=1)
        sim_frame.columnconfigure(3, weight=1)

        ttk.Label(sim_frame, text="Mode:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.mode_var = tk.StringVar(value=MODES[0][0])
        cb = ttk.Combobox(
            sim_frame, textvariable=self.mode_var, state="readonly",
            values=[m[0] for m in MODES],
        )
        cb.grid(row=0, column=1, sticky="ew", padx=4, pady=3)
        Tooltip(cb, TIPS["mode"])
        self._autowiden(cb)

        ttk.Label(sim_frame, text="Order:").grid(row=0, column=2, sticky="e", padx=4, pady=3)
        self.order_var = tk.StringVar(value=ORDERS[0][0])
        cb = ttk.Combobox(
            sim_frame, textvariable=self.order_var, state="readonly",
            values=[o[0] for o in ORDERS],
        )
        cb.grid(row=0, column=3, sticky="ew", padx=4, pady=3)
        Tooltip(cb, TIPS["order"])
        self._autowiden(cb)

        ttk.Label(sim_frame, text="BG Effect:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.effect_var = tk.StringVar(value="none")
        cb = ttk.Combobox(
            sim_frame, textvariable=self.effect_var, values=BG_EFFECTS,
        )
        cb.grid(row=1, column=1, sticky="ew", padx=4, pady=3)
        Tooltip(cb, TIPS["bg_effect"])
        self._autowiden(cb)

        ttk.Label(sim_frame, text="Endgame:").grid(row=1, column=2, sticky="e", padx=4, pady=3)
        self.endgame_var = tk.StringVar(value=ENDGAME_OPTS[3][0])
        cb = ttk.Combobox(
            sim_frame, textvariable=self.endgame_var, state="readonly",
            values=[e[0] for e in ENDGAME_OPTS],
        )
        cb.grid(row=1, column=3, sticky="ew", padx=4, pady=3)
        Tooltip(cb, TIPS["endgame"])
        self._autowiden(cb)

        ttk.Label(sim_frame, text="Dominion:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.dominion_var = tk.StringVar(value=DOMINION_OPTS[0][0])
        cb = ttk.Combobox(
            sim_frame, textvariable=self.dominion_var, state="readonly",
            values=[d[0] for d in DOMINION_OPTS],
        )
        cb.grid(row=2, column=1, sticky="ew", padx=4, pady=3)
        Tooltip(cb, TIPS["dominion"])
        self._autowiden(cb)

        ttk.Label(sim_frame, text="Deck size:").grid(row=2, column=2, sticky="e", padx=4, pady=3)
        ds_frame = ttk.Frame(sim_frame)
        ds_frame.grid(row=2, column=3, sticky="w", padx=4, pady=3)
        self.deck_min_var = tk.StringVar(value="10")
        self.deck_max_var = tk.StringVar(value="10")
        deck_sizes = [""] + [str(i) for i in range(1, 11)]
        ds_min = ttk.Combobox(
            ds_frame, textvariable=self.deck_min_var, values=deck_sizes,
            state="readonly", width=4,
        )
        ds_min.pack(side="left")
        Tooltip(ds_min, TIPS["deck_size"])
        ttk.Label(ds_frame, text=" to ").pack(side="left")
        ds_max = ttk.Combobox(
            ds_frame, textvariable=self.deck_max_var, values=deck_sizes,
            state="readonly", width=4,
        )
        ds_max.pack(side="left")
        Tooltip(ds_max, TIPS["deck_size"])

        # --- Operation ---
        op_frame = ttk.LabelFrame(left, text="Operation")
        op_frame.grid(row=1, column=0, sticky="ew", pady=6)
        for c in range(8):
            op_frame.columnconfigure(c, weight=1)

        ttk.Label(op_frame, text="Operation:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.operation_var = tk.StringVar(value=OPERATIONS[0][0])
        op_combo = ttk.Combobox(
            op_frame, textvariable=self.operation_var, state="readonly",
            values=[o[0] for o in OPERATIONS], width=14,
        )
        op_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        op_combo.bind("<<ComboboxSelected>>", lambda e: self._update_operation_params())
        Tooltip(op_combo, TIPS["operation"])
        self._autowiden(op_combo)

        self.iter_label = ttk.Label(op_frame, text="Iterations:")
        self.iter_var = tk.StringVar(value="1000")
        self.iter_entry = ttk.Entry(op_frame, textvariable=self.iter_var, width=10)
        Tooltip(self.iter_entry, TIPS["iterations"])

        self.climbex_init_label = ttk.Label(op_frame, text="Initial:")
        self.climbex_init_var = tk.StringVar(value="10")
        self.climbex_init_entry = ttk.Entry(op_frame, textvariable=self.climbex_init_var, width=8)
        Tooltip(self.climbex_init_entry, TIPS["climbex_initial"])
        self.climbex_final_label = ttk.Label(op_frame, text="Final:")
        self.climbex_final_var = tk.StringVar(value="1000")
        self.climbex_final_entry = ttk.Entry(op_frame, textvariable=self.climbex_final_var, width=8)
        Tooltip(self.climbex_final_entry, TIPS["climbex_final"])

        self.anneal_iter_label = ttk.Label(op_frame, text="Iter:")
        self.anneal_iter_var = tk.StringVar(value="1000")
        self.anneal_iter_entry = ttk.Entry(op_frame, textvariable=self.anneal_iter_var, width=8)
        Tooltip(self.anneal_iter_entry, TIPS["anneal_iter"])
        self.anneal_temp_label = ttk.Label(op_frame, text="Temp:")
        self.anneal_temp_var = tk.StringVar(value="100")
        self.anneal_temp_entry = ttk.Entry(op_frame, textvariable=self.anneal_temp_var, width=8)
        Tooltip(self.anneal_temp_entry, TIPS["anneal_temp"])
        self.anneal_decay_label = ttk.Label(op_frame, text="Decay:")
        self.anneal_decay_var = tk.StringVar(value="0.001")
        self.anneal_decay_entry = ttk.Entry(op_frame, textvariable=self.anneal_decay_var, width=8)
        Tooltip(self.anneal_decay_entry, TIPS["anneal_decay"])

        # initial layout — single iteration entry
        self.iter_label.grid(row=0, column=2, sticky="e", padx=4)
        self.iter_entry.grid(row=0, column=3, sticky="w", padx=4)

        # --- Owned cards ---
        # =========================================================
        # RIGHT COLUMN — owned cards, execution, output, extras
        # =========================================================
        oc_frame = ttk.LabelFrame(
            right, text="Owned cards inventory (must be in the data folder; "
                        "blank = data\\ownedcards.txt)",
        )
        oc_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        oc_frame.columnconfigure(1, weight=1)

        self.use_owned_var = tk.BooleanVar(value=True)
        use_owned_chk = ttk.Checkbutton(
            oc_frame, text="Use file", variable=self.use_owned_var,
        )
        use_owned_chk.grid(row=0, column=0, sticky="w", padx=4, pady=3)
        Tooltip(use_owned_chk, TIPS["use_owned"])
        self.owned_path_var = tk.StringVar(value="")
        # Dropdown of .txt files already in data/ (blank = default); Browse
        # copies in files from elsewhere.
        self.owned_combo = ttk.Combobox(
            oc_frame, textvariable=self.owned_path_var,
            values=[""] + list_inventory_files(),
        )
        self.owned_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=3)
        Tooltip(self.owned_combo, TIPS["owned_file"])
        self._autowiden(self.owned_combo)
        ttk.Button(oc_frame, text="Browse…", command=self._browse_owned).grid(row=0, column=2, padx=4, pady=3)

        # --- Execution ---
        exec_frame = ttk.LabelFrame(right, text="Execution")
        exec_frame.grid(row=1, column=0, sticky="ew", pady=6)
        for c in range(6):
            exec_frame.columnconfigure(c, weight=1)

        ttk.Label(exec_frame, text="Threads:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.threads_var = tk.StringVar(value="4")
        threads_entry = ttk.Entry(exec_frame, textvariable=self.threads_var, width=6)
        threads_entry.grid(row=0, column=1, sticky="w", padx=4)
        Tooltip(threads_entry, TIPS["threads"])
        ttk.Label(exec_frame, text="Timeout:").grid(row=0, column=2, sticky="e", padx=4)
        self.timeout_var = tk.StringVar(value="none")
        cb = ttk.Combobox(
            exec_frame, textvariable=self.timeout_var,
            values=[label for label, _ in TIMEOUT_CHOICES], width=8,
        )
        cb.grid(row=0, column=3, sticky="w", padx=4)
        Tooltip(cb, TIPS["timeout"])
        self._autowiden(cb)
        ttk.Label(exec_frame, text="Fund (SP):").grid(row=0, column=4, sticky="e", padx=4)
        self.fund_var = tk.StringVar(value="0")
        fund_entry = ttk.Entry(exec_frame, textvariable=self.fund_var, width=8)
        fund_entry.grid(row=0, column=5, sticky="w", padx=4)
        Tooltip(fund_entry, TIPS["fund"])

        self.keep_commander_var = tk.BooleanVar(value=False)
        kc_chk = ttk.Checkbutton(
            exec_frame, text="Keep commander (-c)", variable=self.keep_commander_var,
        )
        kc_chk.grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=3)
        Tooltip(kc_chk, TIPS["keep_commander"])

        self.no_db_var = tk.BooleanVar(value=True)
        nodb_chk = ttk.Checkbutton(
            exec_frame, text="no-db", variable=self.no_db_var,
        )
        nodb_chk.grid(row=1, column=3, columnspan=3, sticky="w", padx=4, pady=3)
        Tooltip(nodb_chk, TIPS["no_db"])

        # --- Output flags ---
        out_flags = ttk.LabelFrame(right, text="Output flags")
        out_flags.grid(row=2, column=0, sticky="ew", pady=6)
        for c in range(5):
            out_flags.columnconfigure(c, weight=1)

        self.so_var = tk.BooleanVar(value=True)
        self.uc_var = tk.BooleanVar(value=True)
        self.vc_var = tk.BooleanVar(value=False)
        self.ci_var = tk.BooleanVar(value=False)
        self.verbose_var = tk.BooleanVar(value=False)
        for col, (text, var, tip_key) in enumerate([
            ("+so", self.so_var, "flag_so"),
            ("+uc", self.uc_var, "flag_uc"),
            ("+vc", self.vc_var, "flag_vc"),
            ("+ci", self.ci_var, "flag_ci"),
            ("+v", self.verbose_var, "flag_v"),
        ]):
            chk = ttk.Checkbutton(out_flags, text=text, variable=var)
            chk.grid(row=0, column=col, sticky="w", padx=4)
            Tooltip(chk, TIPS[tip_key])

        # --- Extra flags ---
        extras_frame = ttk.LabelFrame(right, text="Extra flags")
        extras_frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        extras_frame.columnconfigure(0, weight=1)
        self.extra_var = tk.StringVar(value="")
        extra_entry = ttk.Entry(extras_frame, textvariable=self.extra_var)
        extra_entry.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        Tooltip(extra_entry, TIPS["extra_flags"])

    def _build_output_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        # Command preview line
        cmd_frame = ttk.Frame(parent)
        cmd_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=(8, 4))
        cmd_frame.columnconfigure(0, weight=1)

        ttk.Label(cmd_frame, text="Command:").grid(row=0, column=0, sticky="w")
        self.cmd_preview = tk.Text(cmd_frame, height=3, wrap="word", bg="#f5f5f5")
        self.cmd_preview.grid(row=1, column=0, sticky="ew")
        self.cmd_preview.configure(state="disabled")

        # Output area
        out_frame = ttk.Frame(parent)
        out_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 8))
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)

        self.output_text = tk.Text(out_frame, wrap="word", bg="#1e1e1e", fg="#dcdcdc", insertbackground="#fff")
        self.output_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(out_frame, orient="vertical", command=self.output_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.output_text.configure(yscrollcommand=scroll.set)

    def _build_results_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(8, 4))
        ttk.Button(bar, text="Refresh", command=self._load_results).pack(side="left", padx=2)
        ttk.Button(bar, text="Copy deck", command=self._copy_result_deck).pack(side="left", padx=2)
        ttk.Button(bar, text="Open results file", command=self._open_results_file).pack(side="left", padx=2)
        ttk.Button(bar, text="Clear results", command=self._clear_results).pack(side="left", padx=2)
        ttk.Label(
            bar, text="Newest first. Click a column to sort. Double-click a row to copy its deck.",
            foreground="#777",
        ).pack(side="left", padx=10)

        cols = ("time", "account", "op", "score", "win", "cost", "deck")
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        headings = {
            "time": ("Time", 130), "account": ("Account", 110), "op": ("Op", 70),
            "score": ("Score", 70), "win": ("Win", 100), "cost": ("Cost", 70),
            "deck": ("Deck", 520),
        }
        for col, (title, width) in headings.items():
            tree.heading(col, text=title, command=lambda c=col: self._sort_results(c))
            tree.column(col, width=width, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 8))
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=(0, 8))
        tree.configure(yscrollcommand=scroll.set)
        tree.bind("<Double-1>", lambda e: self._copy_result_deck())
        self.results_tree = tree
        self._results_sort_reverse: dict[str, bool] = {}
        self._load_results()

    def _load_results(self) -> None:
        tree = self.results_tree
        tree.delete(*tree.get_children())
        for rec in reversed(parse_results_file()):  # newest first
            deck = rec["deck"] or (f"ERROR: {rec['error']}" if rec["error"] else "")
            tree.insert("", "end", values=(
                rec["time"], rec["name"], rec["op"],
                rec["score"], rec["win"], rec["cost"], deck,
            ))

    def _sort_results(self, col: str) -> None:
        tree = self.results_tree
        idx = tree["columns"].index(col)
        reverse = self._results_sort_reverse.get(col, False)

        def key(item):
            val = tree.item(item, "values")[idx]
            try:
                return (0, float(re.sub(r"[^\d.]", "", val) or "nan"))
            except ValueError:
                return (1, val.lower())

        for pos, item in enumerate(sorted(tree.get_children(), key=key, reverse=reverse)):
            tree.move(item, "", pos)
        self._results_sort_reverse[col] = not reverse

    def _copy_result_deck(self) -> None:
        sel = self.results_tree.selection()
        if not sel:
            self.status_var.set("Select a result row first.")
            return
        deck = self.results_tree.item(sel[0], "values")[6]
        self.root.clipboard_clear()
        self.root.clipboard_append(deck)
        self.status_var.set("Deck copied to clipboard.")

    def _open_results_file(self) -> None:
        try:
            GUI_DATA_DIR.mkdir(exist_ok=True)
            RESULTS_FILE.touch(exist_ok=True)
            if os.name == "nt":
                os.startfile(RESULTS_FILE.resolve())  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(RESULTS_FILE.resolve())])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open results file: {e}")

    def _clear_results(self) -> None:
        if not messagebox.askyesno(
            "Clear results", "Delete all saved results? This empties results.txt.",
        ):
            return
        try:
            RESULTS_FILE.write_text("", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Error", f"Could not clear results: {e}")
            return
        self._load_results()

    def _build_bottom_bar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(0, 8))

        self.preview_btn = ttk.Button(bar, text="Preview Command", command=self._preview_command)
        self.preview_btn.pack(side="left", padx=4)

        self.run_btn = ttk.Button(bar, text="Run Sim", command=self._run_clicked)
        self.run_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop_clicked, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        self.clear_btn = ttk.Button(bar, text="Clear Output", command=self._clear_output)
        self.clear_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, foreground="#444").pack(side="right", padx=8)

    # ------------------------------------------------------------------
    # Operation params toggle
    # ------------------------------------------------------------------
    def _update_operation_params(self) -> None:
        op_label = self.operation_var.get()
        op = next((v for n, v in OPERATIONS if n == op_label), "climb")

        # Hide everything first
        for w in [
            self.iter_label, self.iter_entry,
            self.climbex_init_label, self.climbex_init_entry,
            self.climbex_final_label, self.climbex_final_entry,
            self.anneal_iter_label, self.anneal_iter_entry,
            self.anneal_temp_label, self.anneal_temp_entry,
            self.anneal_decay_label, self.anneal_decay_entry,
        ]:
            w.grid_forget()

        if op == "climbex":
            self.climbex_init_label.grid(row=0, column=2, sticky="e", padx=4)
            self.climbex_init_entry.grid(row=0, column=3, sticky="w", padx=4)
            self.climbex_final_label.grid(row=0, column=4, sticky="e", padx=4)
            self.climbex_final_entry.grid(row=0, column=5, sticky="w", padx=4)
        elif op == "anneal":
            self.anneal_iter_label.grid(row=0, column=2, sticky="e", padx=4)
            self.anneal_iter_entry.grid(row=0, column=3, sticky="w", padx=4)
            self.anneal_temp_label.grid(row=0, column=4, sticky="e", padx=4)
            self.anneal_temp_entry.grid(row=0, column=5, sticky="w", padx=4)
            self.anneal_decay_label.grid(row=0, column=6, sticky="e", padx=4)
            self.anneal_decay_entry.grid(row=0, column=7, sticky="w", padx=4)
        elif op == "debug sim":
            pass  # no extra params
        else:
            self.iter_label.grid(row=0, column=2, sticky="e", padx=4)
            self.iter_entry.grid(row=0, column=3, sticky="w", padx=4)

    def _resolve_side_forts(self, side_label: str, raw_f1: str, raw_f2: str) -> list[str]:
        """Validate and resolve forts for one side. Returns list of canonical names (0 or 2)."""
        f1 = strip_dropdown(raw_f1)
        f2 = strip_dropdown(raw_f2)
        provided = [f for f in (f1, f2) if f]
        if len(provided) == 0:
            return []
        if len(provided) == 1:
            raise ValueError(f"{side_label}: select 0 or 2 forts, not 1.")
        resolved = [resolve_fort(f) for f in provided]
        cats = {c for _, c in resolved}
        if len(cats) > 1:
            raise ValueError(
                f"{side_label}: cannot mix siege and defense forts. "
                "Siege: " + ", ".join(SIEGE_FORTS) + ". "
                "Defense: " + ", ".join(DEFENSE_FORTS) + "."
            )
        return [n for n, _ in resolved]

    # ------------------------------------------------------------------
    # Config harvesting
    # ------------------------------------------------------------------
    def _harvest_config(self) -> SimConfig:
        cfg = SimConfig()
        cfg.my_deck = self.my_deck_text.get("1.0", "end").strip().replace("\n", " ")
        cfg.enemy_deck = self.enemy_deck_text.get("1.0", "end").strip().replace("\n", " ")

        # Forts — independent validation per side
        cfg.my_forts = self._resolve_side_forts(
            "My side", self.my_fort1_var.get(), self.my_fort2_var.get(),
        )
        cfg.enemy_forts = self._resolve_side_forts(
            "Enemy side", self.enemy_fort1_var.get(), self.enemy_fort2_var.get(),
        )

        cfg.effect = self.effect_var.get().strip() or "none"
        cfg.y_effect = strip_war_effect(self.my_effect_var.get())
        cfg.e_effect = strip_war_effect(self.enemy_effect_var.get())
        cfg.mode = next((v for n, v in MODES if n == self.mode_var.get()), "pvp")
        cfg.order = next((v for n, v in ORDERS if n == self.order_var.get()), "random")
        cfg.operation = next(
            (v for n, v in OPERATIONS if n == self.operation_var.get()), "climb"
        )

        if cfg.operation == "climbex":
            cfg.climbex_initial = parse_int(self.climbex_init_var.get(), "Climbex initial", 10)
            cfg.climbex_final = parse_int(self.climbex_final_var.get(), "Climbex final", 1000)
        elif cfg.operation == "anneal":
            cfg.anneal_iter = parse_int(self.anneal_iter_var.get(), "Anneal iterations", 1000)
            cfg.anneal_temp = parse_float(self.anneal_temp_var.get(), "Anneal temperature", 100)
            cfg.anneal_decay = parse_float(self.anneal_decay_var.get(), "Anneal decay", 0.001)
        elif cfg.operation == "debug sim":
            pass
        else:
            cfg.iterations = parse_int(self.iter_var.get(), "Iterations", 1000)

        # Match by label, falling back to prefix (handles old saved labels)
        dom_label = self.dominion_var.get()
        cfg.dominion = next(
            (v for n, v in DOMINION_OPTS if n == dom_label or dom_label.startswith(v)),
            "dom-owned",
        )
        cfg.endgame = next(
            (v for n, v in ENDGAME_OPTS if n == self.endgame_var.get()), "2"
        )

        cfg.deck_min = (
            parse_int(self.deck_min_var.get(), "Deck size (min)")
            if self.deck_min_var.get().strip() else None
        )
        cfg.deck_max = (
            parse_int(self.deck_max_var.get(), "Deck size (max)")
            if self.deck_max_var.get().strip() else None
        )

        cfg.use_owned = self.use_owned_var.get()
        cfg.owned_cards_path = self.owned_path_var.get().strip()

        cfg.threads = parse_int(self.threads_var.get(), "Threads", 4)
        cfg.timeout_hours = parse_timeout(self.timeout_var.get())
        cfg.fund = parse_int(self.fund_var.get(), "Fund", 0)
        cfg.keep_commander = self.keep_commander_var.get()
        cfg.no_db = self.no_db_var.get()

        cfg.short_output = self.so_var.get()
        cfg.print_upgraded = self.uc_var.get()
        cfg.print_card_values = self.vc_var.get()
        cfg.print_ci = self.ci_var.get()
        cfg.verbose = self.verbose_var.get()

        cfg.extra_flags = self.extra_var.get().strip()
        # Binary is always tuo.exe
        cfg.binary = "tuo.exe"
        return cfg

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _preview_command(self) -> None:
        try:
            cfg = self._harvest_config()
            cmd = build_command(cfg)
        except ValueError as e:
            messagebox.showerror("Validation error", str(e))
            return
        text = format_command(cmd)
        self.cmd_preview.configure(state="normal")
        self.cmd_preview.delete("1.0", "end")
        self.cmd_preview.insert("1.0", text)
        self.cmd_preview.configure(state="disabled")
        self.notebook.select(self.output_tab)
        self.status_var.set("Command previewed.")

    def _run_clicked(self) -> None:
        if self.process is not None or self.batch_running:
            messagebox.showinfo("Busy", "A sim is already running.")
            return
        if self.updating_xmls or self.updating_inventories:
            messagebox.showinfo("Busy", "Wait for the running update to finish first.")
            return
        try:
            cfg = self._harvest_config()
            cmd = build_command(cfg)
        except ValueError as e:
            messagebox.showerror("Validation error", str(e))
            return

        self._save_settings()  # remember inputs on each run
        self._last_run_op = cfg.operation

        text = format_command(cmd)
        self.cmd_preview.configure(state="normal")
        self.cmd_preview.delete("1.0", "end")
        self.cmd_preview.insert("1.0", text)
        self.cmd_preview.configure(state="disabled")

        self.notebook.select(self.output_tab)
        self._append_output(f"\n$ {text}\n\n")

        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Running…")

        thread = threading.Thread(target=self._run_process, args=(cmd,), daemon=True)
        thread.start()

    def _stop_clicked(self) -> None:
        if self.batch_running:
            self.batch_stop = True
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.status_var.set("Stopping…")
        except Exception as e:
            messagebox.showerror("Error", f"Could not stop process: {e}")

    def _clear_output(self) -> None:
        self.output_text.delete("1.0", "end")

    def _browse_owned(self) -> None:
        path = filedialog.askopenfilename(
            title="Select ownedcards file",
            initialdir=str(DATA_DIR.resolve()) if DATA_DIR.exists() else ".",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        try:
            in_data = p.resolve().parent == DATA_DIR.resolve()
        except OSError:
            in_data = False
        if not in_data:
            # tuo ignores owned-cards files outside its data folder
            if not messagebox.askyesno(
                "Copy to data folder?",
                "tuo only reads owned-cards files from its 'data' folder, so "
                f"this file would be ignored where it is.\n\nCopy "
                f"'{p.name}' into the data folder and use the copy?",
            ):
                return
            try:
                DATA_DIR.mkdir(exist_ok=True)
                shutil.copy2(p, DATA_DIR / p.name)
            except OSError as e:
                messagebox.showerror("Error", f"Could not copy file: {e}")
                return
        self.owned_path_var.set(str(Path("data") / p.name))
        self.owned_combo.configure(values=[""] + list_inventory_files())

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------
    def _run_process(self, cmd: list[str]) -> None:
        try:
            # On Windows, prevent flashing a console window if the user double-clicked the .pyw
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            # Unbuffered binary reads: show tuo's output as soon as it arrives,
            # including progress text without newlines and partial output
            # written just before a crash.
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            self.output_queue.put(f"\n[ERROR] Could not find '{cmd[0]}'. "
                                  f"Make sure the binary is in this folder or on PATH.\n")
            self.output_queue.put("__DONE__:127")
            self.process = None
            return
        except Exception as e:
            self.output_queue.put(f"\n[ERROR] Failed to launch: {e}\n")
            self.output_queue.put("__DONE__:1")
            self.process = None
            return

        assert self.process.stdout is not None
        stream = self.process.stdout
        captured = ""
        while True:
            try:
                chunk = stream.read(4096)
            except Exception:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            captured = (captured + text)[-200_000:]  # keep the tail
            self.output_queue.put(text)
        rc = self.process.wait()
        # Record the outcome so it shows up in the Results tab too
        result = extract_result_lines(captured)
        if any(result.values()):
            append_result_block("manual", getattr(self, "_last_run_op", ""), result)
        self.output_queue.put(f"__DONE__:{rc}")
        self.process = None

    def _poll_output(self) -> None:
        try:
            while True:
                line = self.output_queue.get_nowait()
                if isinstance(line, str) and line.startswith("__DONE__:"):
                    rc = line.split(":", 1)[1]
                    self._append_output(f"\n[Process exited with code {rc}]\n")
                    if rc not in ("0", "127"):
                        hint = EXIT_HINTS.get(rc, GENERIC_FAIL_HINT)
                        self._append_output(f"[Hint] {hint}\n")
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set(f"Done (exit {rc}).")
                    self._load_results()
                elif isinstance(line, str) and line == "__BATCH_DONE__":
                    self._append_output("\n=== Batch finished ===\n")
                    self.batch_running = False
                    self.batch_stop = False
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_var.set("Batch done.")
                    self._load_results()
                elif isinstance(line, str) and line.startswith("__INV_DONE__:"):
                    rc = line.split(":", 1)[1]
                    ok = rc == "0"
                    self._append_output(
                        "=== Account update "
                        + ("finished ===\n" if ok else f"failed (exit {rc}) ===\n")
                    )
                    self.updating_inventories = False
                    self.run_btn.configure(state="normal")
                    # New inventory files/accounts may have appeared — refresh
                    self.owned_combo.configure(values=[""] + list_inventory_files())
                    self._refresh_accounts_list()
                    self.status_var.set(
                        "Owned cards & decks updated." if ok
                        else f"Account update failed (exit {rc})."
                    )
                elif isinstance(line, str) and line.startswith("__XML_DONE__:"):
                    _, ok, failed = line.split(":")
                    self._append_output(
                        f"=== XML update finished: {ok} updated, {failed} failed ===\n"
                    )
                    self.updating_xmls = False
                    self.run_btn.configure(state="normal")
                    self.status_var.set(f"XML update done ({ok} ok, {failed} failed).")
                else:
                    self._append_output(line)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_output)

    def _append_output(self, text: str) -> None:
        self.output_text.insert("end", text)
        self.output_text.see("end")

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _save_settings(self) -> None:
        data = {
            "my_deck": self.my_deck_text.get("1.0", "end").strip(),
            "enemy_deck": self.enemy_deck_text.get("1.0", "end").strip(),
            "my_fort1": self.my_fort1_var.get(),
            "my_fort2": self.my_fort2_var.get(),
            "enemy_fort1": self.enemy_fort1_var.get(),
            "enemy_fort2": self.enemy_fort2_var.get(),
            "my_effect": self.my_effect_var.get(),
            "enemy_effect": self.enemy_effect_var.get(),
            "deck_type": self.deck_type_var.get(),
            "mode": self.mode_var.get(),
            "order": self.order_var.get(),
            "operation": self.operation_var.get(),
            "effect": self.effect_var.get(),
            "iterations": self.iter_var.get(),
            "climbex_init": self.climbex_init_var.get(),
            "climbex_final": self.climbex_final_var.get(),
            "anneal_iter": self.anneal_iter_var.get(),
            "anneal_temp": self.anneal_temp_var.get(),
            "anneal_decay": self.anneal_decay_var.get(),
            "dominion": self.dominion_var.get(),
            "endgame": self.endgame_var.get(),
            "deck_min": self.deck_min_var.get(),
            "deck_max": self.deck_max_var.get(),
            "use_owned": self.use_owned_var.get(),
            "owned_path": self.owned_path_var.get(),
            "threads": self.threads_var.get(),
            "timeout": self.timeout_var.get(),
            "fund": self.fund_var.get(),
            "keep_commander": self.keep_commander_var.get(),
            "no_db": self.no_db_var.get(),
            "so": self.so_var.get(),
            "uc": self.uc_var.get(),
            "vc": self.vc_var.get(),
            "ci": self.ci_var.get(),
            "verbose": self.verbose_var.get(),
            "extra": self.extra_var.get(),
        }
        try:
            GUI_DATA_DIR.mkdir(exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(data, indent=2))
            # settings now live in tuo_gui_data/ — drop the old root-level file
            LEGACY_SETTINGS_FILE.unlink(missing_ok=True)
        except Exception:
            pass  # don't disrupt user if save fails

    def _load_settings(self) -> None:
        path = SETTINGS_FILE if SETTINGS_FILE.exists() else LEGACY_SETTINGS_FILE
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except Exception:
            return
        # Apply each field if present
        if data.get("my_deck"):
            self.my_deck_text.insert("1.0", data["my_deck"])
        if data.get("enemy_deck"):
            self.enemy_deck_text.insert("1.0", data["enemy_deck"])

        self.my_fort1_var.set(data.get("my_fort1", ""))
        self.my_fort2_var.set(data.get("my_fort2", ""))
        self.enemy_fort1_var.set(data.get("enemy_fort1", ""))
        self.enemy_fort2_var.set(data.get("enemy_fort2", ""))
        self.my_effect_var.set(data.get("my_effect", ""))
        self.enemy_effect_var.set(data.get("enemy_effect", ""))
        if data.get("deck_type") in DECK_TYPE_FILES:
            self.deck_type_var.set(data["deck_type"])
        self.mode_var.set(data.get("mode", MODES[0][0]))
        self.order_var.set(data.get("order", ORDERS[0][0]))
        self.operation_var.set(data.get("operation", OPERATIONS[0][0]))
        self.effect_var.set(data.get("effect", "none"))
        self.iter_var.set(data.get("iterations", "1000"))
        self.climbex_init_var.set(data.get("climbex_init", "10"))
        self.climbex_final_var.set(data.get("climbex_final", "1000"))
        self.anneal_iter_var.set(data.get("anneal_iter", "1000"))
        self.anneal_temp_var.set(data.get("anneal_temp", "100"))
        self.anneal_decay_var.set(data.get("anneal_decay", "0.001"))
        # Old settings stored a longer dominion label — normalize by prefix
        dom_saved = data.get("dominion", DOMINION_OPTS[0][0])
        dom_label = next(
            (n for n, v in DOMINION_OPTS if n == dom_saved or dom_saved.startswith(v)),
            DOMINION_OPTS[0][0],
        )
        self.dominion_var.set(dom_label)
        self.endgame_var.set(data.get("endgame", ENDGAME_OPTS[3][0]))
        self.deck_min_var.set(data.get("deck_min", "10"))
        self.deck_max_var.set(data.get("deck_max", "10"))
        self.use_owned_var.set(data.get("use_owned", True))
        self.owned_path_var.set(data.get("owned_path", ""))
        self.threads_var.set(data.get("threads", "4"))
        self.timeout_var.set(data.get("timeout", "none") or "none")
        self.fund_var.set(data.get("fund", "0"))
        self.keep_commander_var.set(data.get("keep_commander", False))
        self.no_db_var.set(data.get("no_db", True))
        self.so_var.set(data.get("so", True))
        self.uc_var.set(data.get("uc", True))
        self.vc_var.set(data.get("vc", False))
        self.ci_var.set(data.get("ci", False))
        self.verbose_var.set(data.get("verbose", False))
        # Migrate 'no-db' out of old saved extra flags — it's a checkbox now
        extra_tokens = data.get("extra", "").split()
        if "no-db" in extra_tokens:
            extra_tokens = [t for t in extra_tokens if t != "no-db"]
            self.no_db_var.set(True)
        self.extra_var.set(" ".join(extra_tokens))


def main() -> None:
    # Work relative to the app folder no matter how we were launched
    # (double-clicked exe, shortcut, or script) — tuo.exe, data/ and
    # tuo_gui_data/ all live next to the app.
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    else:
        os.chdir(Path(__file__).resolve().parent)

    # Give the app its own Windows taskbar identity — without this, running
    # via python.exe makes the taskbar show Python's icon instead of ours.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Jonnyenglish89.TUO_GUI.1"
            )
        except Exception:
            pass

    root = tk.Tk()
    # App icon: icon.ico lives next to the script, or inside the PyInstaller
    # bundle (extracted to sys._MEIPASS) when running as an exe.
    try:
        if getattr(sys, "frozen", False):
            icon_base = Path(getattr(sys, "_MEIPASS", "."))
        else:
            icon_base = Path(__file__).resolve().parent
        icon_path = icon_base / "icon.ico"
        if icon_path.exists():
            root.iconbitmap(default=str(icon_path))
        # Tk caps window icons at the 32px system size, which the taskbar
        # then upscales (blurry, especially at 125%+ display scaling).
        # Bypass Tk: hand Windows a full-resolution icon via WM_SETICON.
        if os.name == "nt" and icon_path.exists():
            def _sharp_taskbar_icon(attempt: int = 1):
                try:
                    import ctypes
                    from ctypes import wintypes
                    user32 = ctypes.windll.user32
                    # Correct signatures — without these, 64-bit Python
                    # truncates the icon handle and the call does nothing.
                    user32.GetParent.restype = wintypes.HWND
                    user32.GetParent.argtypes = [wintypes.HWND]
                    user32.LoadImageW.restype = wintypes.HANDLE
                    user32.LoadImageW.argtypes = [
                        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                        ctypes.c_int, ctypes.c_int, wintypes.UINT,
                    ]
                    user32.SendMessageW.restype = ctypes.c_ssize_t
                    user32.SendMessageW.argtypes = [
                        wintypes.HWND, wintypes.UINT,
                        ctypes.c_size_t, ctypes.c_ssize_t,
                    ]

                    root.update_idletasks()
                    hwnd = user32.GetParent(root.winfo_id())
                    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
                    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
                    big = user32.LoadImageW(
                        None, str(icon_path), IMAGE_ICON, 192, 192, LR_LOADFROMFILE,
                    )
                    small = user32.LoadImageW(
                        None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE,
                    )
                    if hwnd and big:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
                        if small:
                            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
                    elif attempt < 3:
                        root.after(500, lambda: _sharp_taskbar_icon(attempt + 1))
                except Exception:
                    pass
            root.after(250, _sharp_taskbar_icon)
    except Exception:
        pass  # icon is cosmetic — never block startup over it
    # Slightly nicer default theme on Windows / cross-platform
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    app = SimRunnerApp(root)

    def on_close():
        app._save_settings()
        if app.process is not None:
            try:
                app.process.terminate()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
