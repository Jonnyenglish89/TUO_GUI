#!/usr/bin/python3
"""
tu_inventory.py

Reads cookies from ./tuo_gui_data/cookies/cookie_<kong_name>, calls the Tyrant Unleashed API
with message=init, then exports:
  - data/ownedcards/ownedcards_<kong_name>.txt   (owned + restorable cards)
  - tuo_gui_data/attack_decks.txt
  - tuo_gui_data/defence_decks.txt

XML card data is read from ./data/ (TUO's own folder, unchanged).
"""

import os
import sys
import copy
import json
import time
import urllib3
import certifi
import xml.etree.ElementTree as ET

from random import randint
from urllib3.util.timeout import Timeout
from urllib3 import PoolManager, Retry

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
GUI_DATA_DIR    = os.path.join(SCRIPT_DIR, 'tuo_gui_data')
COOKIES_DIR     = os.path.join(GUI_DATA_DIR, 'cookies')
DATA_DIR        = GUI_DATA_DIR                          # attack/defence decks, players.json, results
OWNED_DIR = os.path.join('data', 'ownedcards') # ownedcards must use a relative path instead of an absolute one and be in the data folder or else tuo silently breaks
XML_DIR         = os.path.join(SCRIPT_DIR, 'data')     # TUO's own XML files — never moved

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------

PROTOCOL = "https"
API_HOST  = "mobile.tyrantonline.com"
API_PATH  = "api.php"

STATIC_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (X11; Linux x86_64; rv:38.0) Gecko/20100101 Firefox/38.0",
    "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":  "en-US,en;q=0.5",
    "Accept-Encoding":  "gzip, compress",
    "Connection":       "keep-alive",
    "Content-Type":     "application/x-www-form-urlencoded",
}

BASIC_BODY_PARAMS = {
    "unity":          "Unity4_6_6",
    "client_version": "68",
    "device_type":    "Intel(R)+Pentium(R)+4+CPU+2.40GHz+(7830+MB)",
    "os_version":     "Windows+XP+Service+Pack+3+(5.1.2600)",
    "platform":       "Web",
}

# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------

FACTION_MAP = {
    1: 'Imperial',
    2: 'Raider',
    3: 'Bloodthirsty',
    4: 'Xeno',
    5: 'Righteous',
    6: 'Progenitor',
}

RARITY_MAP = {
    1: 'Common',
    2: 'Rare',
    3: 'Epic',
    4: 'Legendary',
    5: 'Vindicator',
    6: 'Mythic',
}

CARD_SETS = {
    1000: 'Standard',
    2000: 'BoxOrReward',
    2500: 'BaseFusion',
    3000: 'Box',
    3001: 'BoxAlternate',
    4000: 'PveReward',
    4001: 'PveRewardHold',
    4250: 'MutantReward',
    4500: 'PvpReward',
    4501: 'PvpRewardHold',
    4700: 'PveLegacyReward',
    4750: 'PvpLegacyReward',
    5000: 'InvisibleRewardSet',
    6000: 'GameOnly',
    7000: 'Commander',
    8000: 'Fortress',
    8500: 'Dominion',
    9000: 'Chance',
    9500: 'Summoned',
    9999: 'Waitlisted',
}

SECTIONS = [
    {'rarity': 6, 'fusion': 2, 'label': 'MYTHIC_QUAD'},
    {'rarity': 5, 'fusion': 2, 'label': 'VINDICATOR_QUAD'},
    {'rarity': 4, 'fusion': 2, 'label': 'LEGENDARY_QUAD'},
    {'rarity': 3, 'fusion': 2, 'label': 'EPIC_QUAD'},
    {'rarity': 6, 'fusion': 1, 'label': 'MYTHIC_DUAL'},
    {'rarity': 5, 'fusion': 1, 'label': 'VINDICATOR_DUAL'},
    {'rarity': 4, 'fusion': 1, 'label': 'LEGENDARY_DUAL'},
    {'rarity': 3, 'fusion': 1, 'label': 'EPIC_DUAL'},
    {'rarity': 6, 'fusion': 0, 'label': 'MYTHIC_UNFUSED'},
    {'rarity': 5, 'fusion': 0, 'label': 'VINDICATOR_UNFUSED'},
    {'rarity': 4, 'fusion': 0, 'label': 'LEGENDARY_UNFUSED'},
    {'rarity': 3, 'fusion': 0, 'label': 'EPIC_UNFUSED'},
]

# ---------------------------------------------------------------------------
# Card model
# ---------------------------------------------------------------------------

class Card:
    def __init__(self, card_id, name, level, card_type, card_set, rarity, fusion):
        self.card_id = card_id
        self.name    = name
        self.level   = level
        self.type    = card_type
        self.set     = card_set
        self.rarity  = rarity
        self.fusion  = fusion

    @property
    def rarity_name(self):
        return RARITY_MAP.get(self.rarity, 'Common')

    @property
    def set_name(self):
        return CARD_SETS.get(self.set, 'Unknown')

    @property
    def is_max_level(self):
        return self.level is None

    @property
    def display_name(self):
        return self.name if self.level is None else "{}-{}".format(self.name, self.level)

    @property
    def section(self):
        set_name = self.set_name
        if set_name == 'Commander':
            return 'COMMANDERS'
        if set_name == 'Dominion':
            return 'DOMINIONS'
        rarity_name = self.rarity_name
        if rarity_name in ('Common', 'Rare'):
            return 'RARE_AND_COMMON'
        for s in SECTIONS:
            if self.rarity == s['rarity'] and self.fusion == s['fusion']:
                return s['label']
        return 'UNKNOWN'

    def __repr__(self):
        return "<Card id={} name={!r} lvl={} rarity={} fusion={} set={}>".format(
            self.card_id, self.name, self.level,
            self.rarity_name, self.fusion, self.set_name,
        )

# ---------------------------------------------------------------------------
# XML parser
# ---------------------------------------------------------------------------

def _child_int(element, tag, default=None):
    """Text of <tag> inside element as an int, or default if absent/empty."""
    text = element.findtext(tag)
    if text is None or not text.strip():
        return default
    return int(text)


def _parse_xml_file(xml_fname: str) -> dict:
    """Parse one cards_section_N.xml into {card_id: Card}.

    Each <unit> describes a base card plus its <upgrade> levels, e.g.:

        <unit>
            <id>1234</id>
            <name>Some Card</name>
            <rarity>4</rarity>
            <fusion_level>2</fusion_level>
            <upgrade><card_id>1235</card_id><level>2</level></upgrade>
            ...
        </unit>

    Every level gets its own Card entry; the highest level is the
    'max level' card and displays without a level suffix.
    """
    cards = {}
    root = ET.parse(xml_fname).getroot()

    for unit in root.findall('unit'):
        card_id = _child_int(unit, 'id')
        name    = unit.findtext('name')
        if card_id is None:
            continue
        if name is None:
            print("WARNING: {}: unit id={} has no name".format(xml_fname, card_id))
            continue

        level       = _child_int(unit, 'level', default=1)
        card_type   = _child_int(unit, 'type')
        card_set    = _child_int(unit, 'set')
        card_rarity = _child_int(unit, 'rarity')
        card_fusion = _child_int(unit, 'fusion_level', default=0)

        cards[card_id] = Card(card_id, name, level, card_type, card_set, card_rarity, card_fusion)

        # Each <upgrade> is the same card at the next level, with its own id.
        top_level_id = card_id
        for upgrade in unit.findall('upgrade'):
            upg_id    = _child_int(upgrade, 'card_id')
            upg_level = _child_int(upgrade, 'level')
            if upg_id is None or upg_level is None:
                continue
            top_level_id = upg_id
            cards[upg_id] = Card(upg_id, name, upg_level, card_type, card_set, card_rarity, card_fusion)

        # The last upgrade (or the base card if there were none) is max level.
        cards[top_level_id].level = None

    return cards


def load_all_cards(xml_dir: str = XML_DIR) -> dict:
    """Load every cards_section_N.xml in xml_dir (N = 1, 2, ... until missing)."""
    all_cards = {}
    xml_dir   = os.path.expanduser(xml_dir)

    if not os.path.isdir(xml_dir):
        print("ERROR: XML directory not found: {}".format(xml_dir))
        return all_cards

    for i in range(1, 100):
        xml_fname = os.path.join(xml_dir, 'cards_section_{}.xml'.format(i))
        if not os.path.exists(xml_fname):
            break
        try:
            all_cards.update(_parse_xml_file(xml_fname))
        except ET.ParseError as e:
            print("WARNING: {} is not valid XML ({}) — skipped. "
                  "Try Data -> Update XMLs.".format(xml_fname, e))

    print("INFO: {} card entries loaded".format(len(all_cards)))
    return all_cards


def get_card_name(cards: dict, card_id: int) -> str:
    card = cards.get(card_id)
    return card.display_name if card else '[{}]'.format(card_id)

# ---------------------------------------------------------------------------
# Cookie loader
# ---------------------------------------------------------------------------

def load_cookie(kong_name: str) -> dict:
    path = os.path.join(COOKIES_DIR, 'cookie_{}'.format(kong_name))
    if not os.path.exists(path):
        raise FileNotFoundError("Cookie file not found: {}".format(path))

    with open(path, 'r') as f:
        raw = f.read().strip().rstrip(';')

    params = {}
    for part in raw.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            params[k.strip()] = v.strip()
    return params

def list_kong_names() -> list:
    if not os.path.isdir(COOKIES_DIR):
        return []
    names = []
    for fname in os.listdir(COOKIES_DIR):
        if fname.startswith('cookie_'):
            names.append(fname[len('cookie_'):])
    return sorted(names)

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _mk_params(kvmap: dict) -> str:
    return "&".join("{}={}".format(k, v) for k, v in kvmap.items() if v is not None)


def fetch_init(http: PoolManager, cookie: dict, retries: int = 3) -> dict:
    url_params = _mk_params({
        "message": "init",
        "user_id": cookie["user_id"],
    })

    body_map = {
        "api_stat_name": "init",
        "api_stat_time": str(randint(22, 777)),
        "data_usage":    str(randint(11111, 888888)),
        "timestamp":     str(int(time.time())),
    }
    body_map.update(BASIC_BODY_PARAMS)
    body_map.update({k: str(v) for k, v in cookie.items()})
    body_params = _mk_params(body_map)

    url = "{}://{}/{}?{}".format(PROTOCOL, API_HOST, API_PATH, url_params)

    for attempt in range(1, retries + 1):
        r    = http.request(
            'POST', url,
            headers         = STATIC_HEADERS,
            decode_content  = True,
            preload_content = False,
            body            = body_params,
        )
        data = r.read().decode("UTF-8")
        if data:
            return json.loads(data)
        print("WARN: fetch_init: empty response (attempt {}/{})".format(attempt, retries))

    raise RuntimeError("fetch_init: no data after {} attempts".format(retries))

# ---------------------------------------------------------------------------
# Owned-cards file builder
# ---------------------------------------------------------------------------

def _merge_buyback(user_cards: dict, buyback_data: dict) -> dict:
    for card_id, buyback in buyback_data.items():
        count = int(buyback.get('number', 0))
        if count <= 0:
            continue
        if card_id in user_cards:
            if isinstance(user_cards[card_id], dict):
                user_cards[card_id]['num_owned'] = int(user_cards[card_id].get('num_owned', 0)) + count
            else:
                user_cards[card_id] = int(user_cards[card_id]) + count
        else:
            user_cards[card_id] = count
    return user_cards


def _format_section(label: str, items: list) -> str:
    divider = '//-----------------------------------------'
    out     = "{}\n//{}\n{}\n".format(divider, label, divider)
    if not items:
        out += "\n"
        return out
    for item in sorted(items, key=lambda x: x['name']):
        out += "[{card_id}]#{count} //{name}\n".format(**item)
    return out


def build_owned_cards_file(user_cards: dict, cards: dict) -> str:
    owned = []
    for card_id, card_data in user_cards.items():
        count = int(card_data.get('num_owned', 0) if isinstance(card_data, dict) else card_data)
        if count <= 0:
            continue

        card = cards.get(int(card_id))
        if not card:
            owned.append({
                'card_id': int(card_id),
                'name':    'Unknown Card {}'.format(card_id),
                'count':   count,
                'section': 'UNKNOWN',
            })
            continue

        owned.append({
            'card_id': card.card_id,
            'name':    card.display_name,
            'count':   count,
            'section': card.section,
        })

    by_section = {}
    for item in owned:
        by_section.setdefault(item['section'], []).append(item)

    output = '//Updated: ' + time.strftime('%d %b %Y %H:%M:%S') + "\n\n"

    section_order = [s['label'] for s in SECTIONS] + [
        'RARE_AND_COMMON', 'COMMANDERS', 'DOMINIONS',
    ]

    for label in section_order:
        output += _format_section(label, by_section.get(label, []))

    unknowns = by_section.get('UNKNOWN', [])
    if unknowns:
        output += _format_section('UNKNOWN', unknowns)

    return output

# ---------------------------------------------------------------------------
# Deck file builder
# ---------------------------------------------------------------------------

def build_deck_string(deck: dict, cards: dict) -> str:
    parts = []

    if deck.get('commander_id'):
        parts.append(get_card_name(cards, int(deck['commander_id'])))

    if deck.get('dominion_id'):
        parts.append(get_card_name(cards, int(deck['dominion_id'])))

    for card_id, qty in (deck.get('cards') or {}).items():
        name = get_card_name(cards, int(card_id))
        qty  = int(qty)
        parts.append("{} #{}".format(name, qty) if qty > 1 else name)

    return ', '.join(parts)


def append_or_replace_deck_line(path: str, kong_name: str, new_line: str) -> None:
    lines = []
    if os.path.exists(path):
        with open(path, 'r') as f:
            lines = [l.rstrip('\n') for l in f if l.strip()]

    lines = [l for l in lines if '_{}'.format(kong_name) not in l]
    lines.append(new_line)

    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

# ---------------------------------------------------------------------------
# Player metadata
# ---------------------------------------------------------------------------

PLAYERS_JSON = os.path.join(DATA_DIR, 'players.json')

def save_player_metadata(kong_name: str, tu_name: str, guild: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(PLAYERS_JSON, 'r', encoding='utf-8') as f:
            players = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        players = {}

    players[kong_name] = {
        'tu_name': tu_name or '',
        'guild':   guild   or '',
    }

    with open(PLAYERS_JSON, 'w', encoding='utf-8') as f:
        json.dump(players, f, indent=2)


def load_player_metadata() -> dict:
    try:
        with open(PLAYERS_JSON, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_for_user(kong_name: str, cards: dict, http: PoolManager) -> None:
    print("\n--- Exporting: {} ---".format(kong_name))

    cookie = load_cookie(kong_name)
    data   = fetch_init(http, cookie)

    user_cards   = data.get('user_cards', {})
    buyback_data = data.get('buyback_data', {})
    user_decks   = data.get('user_decks', {})
    tu_name      = (data.get('user_data') or {}).get('name', '')
    guild        = (data.get('faction')   or {}).get('name', '')

    save_player_metadata(kong_name, tu_name, guild)

    # --- Owned cards ---
    os.makedirs(OWNED_DIR, exist_ok=True)
    safe_name = kong_name.rstrip('.')

    # owned + restorable: user_cards merged with buyback_data
    merged_cards = _merge_buyback(copy.deepcopy(user_cards), buyback_data)
    owned_restore_path    = os.path.join(OWNED_DIR, 'ownedcards_{}.txt'.format(safe_name))
    owned_restore_content = build_owned_cards_file(merged_cards, cards)
    with open(owned_restore_path, 'w') as f:
        f.write(owned_restore_content)
    owned_restore_count = owned_restore_content.count('\n[')
    print("INFO: owned+restore cards written to {} ({} entries)".format(owned_restore_path, owned_restore_count))

    # --- Decks ---
    deck_map = {1: 'attack', 2: 'defence'}
    for deck_id, deck_type in deck_map.items():
        deck = user_decks.get(str(deck_id)) or user_decks.get(deck_id)
        if not deck:
            continue
        deck_str = build_deck_string(deck, cards)
        if not deck_str:
            continue

        line      = "gauntlet_{}_{}:{}".format(guild or 'noguild', kong_name, deck_str)
        deck_path = os.path.join(DATA_DIR, '{}_decks.txt'.format(deck_type))
        os.makedirs(DATA_DIR, exist_ok=True)
        append_or_replace_deck_line(deck_path, kong_name, line)
        print("INFO: {} deck written to {}".format(deck_type, deck_path))


def main():
    cards = load_all_cards()
    if not cards:
        print("ERROR: no cards loaded — check XML_DIR")
        sys.exit(1)

    if len(sys.argv) > 1:
        kong_names = sys.argv[1:]
    else:
        kong_names = list_kong_names()
        if not kong_names:
            print("ERROR: no cookie files found in {}".format(COOKIES_DIR))
            sys.exit(1)
        print("INFO: found {} user(s): {}".format(len(kong_names), ', '.join(kong_names)))

    with PoolManager(
        1,
        timeout    = Timeout(connect=15.0, read=20.0, total=30.0),
        retries    = Retry(total=3),
        cert_reqs  = 'CERT_REQUIRED',
        ca_certs   = certifi.where(),
    ) as http:
        for kong_name in kong_names:
            try:
                export_for_user(kong_name, cards, http)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print("ERROR: failed for {}: {}".format(kong_name, e))


if __name__ == '__main__':
    main()