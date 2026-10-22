"""Single source of truth for barangay names.

Why this exists
---------------
A BHW sees an elder only when the elder's address contains the BHW's
`barangay_assigned`. Both used to be typed by hand (and the family side was
also typed in the mobile app), so "Sudlon 2", "Brgy. Sudlon II" and "Sudlon II"
never matched. Now:

  * Every registration form picks from CEBU_CITY_BARANGAYS (a dropdown).
  * canonical() / same_barangay() still tolerate older typed values and the
    mobile app's free text, so nobody drops out of a BHW's list.

The match is done when a page loads, not when someone registers. So the order
of registration does not matter: if the device and family registered first, the
BHW who registers (and is approved) later sees those residents right away.
"""
import re

# All 80 Cebu City barangays, taken from the same boundary data the Pi uses
# (alisto-iot/pi/data/cebu_city_barangays.json, ADM4_EN field).
CEBU_CITY_BARANGAYS = [
    'Adlaon', 'Agsungot', 'Apas', 'Babag', 'Bacayan', 'Banilad', 'Basak Pardo',
    'Basak San Nicolas', 'Binaliw', 'Bonbon', 'Budla-an', 'Buhisan', 'Bulacao',
    'Buot-Taup Pardo', 'Busay', 'Calamba', 'Cambinocot', 'Camputhaw', 'Capitol Site',
    'Carreta', 'Central', 'Cogon Pardo', 'Cogon Ramos', 'Day-as', 'Duljo', 'Ermita',
    'Guadalupe', 'Guba', 'Hippodromo', 'Inayawan', 'Kalubihan', 'Kalunasan',
    'Kamagayan', 'Kasambagan', 'Kinasang-an Pardo', 'Labangon', 'Lahug',
    'Lorega (Lorega San Miguel)', 'Lusaran', 'Luz', 'Mabini', 'Mabolo', 'Malubog',
    'Mambaling', 'Pahina Central', 'Pahina San Nicolas', 'Pamutan', 'Pardo',
    'Pari-an', 'Paril', 'Pasil', 'Pit-os', 'Pulangbato', 'Pung-ol-Sibugay',
    'Punta Princesa', 'Quiot Pardo', 'Sambag I', 'Sambag II', 'San Antonio',
    'San Jose', 'San Nicolas Central', 'San Roque (Ciudad)', 'Santa Cruz',
    'Sapangdaku', 'Sawang Calero', 'Sinsin', 'Sirao', 'Suba Pob. (Suba San Nicolas)',
    'Sudlon I', 'Sudlon II', 'T. Padilla', 'Tabunan', 'Tagbao', 'Talamban', 'Taptap',
    'Tejero (Villa Gonzalo)', 'Tinago', 'Tisa', 'To-ong Pardo', 'Zapatera',
]

_ROMAN = {'1': 'i', '2': 'ii', '3': 'iii', '4': 'iv', '5': 'v'}


def _key(text):
    """Loose comparison key: 'Brgy. Sudlon 2' -> 'sudlon ii'."""
    s = str(text or '').casefold()
    s = re.sub(r'\b(barangay|brgy|bgy)\b\.?', ' ', s)
    s = re.sub(r'\b(pob|poblacion)\b\.?', ' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\b([1-5])\b', lambda m: _ROMAN[m.group(1)], s)
    return ' '.join(s.split())


def _build_index():
    index = {}
    for name in CEBU_CITY_BARANGAYS:
        main = re.sub(r'\(.*?\)', ' ', name)
        inner = re.findall(r'\((.*?)\)', name)
        for variant in [name, main, *inner]:
            index.setdefault(_key(variant), name)
    return index


_INDEX = _build_index()


def canonical(text):
    """The dropdown spelling for [text], or None if it is not a Cebu City barangay."""
    return _INDEX.get(_key(text))


def same_barangay(a, b):
    """True when both texts mean the same barangay. 'Sudlon I' never equals 'Sudlon II'."""
    ca, cb = canonical(a), canonical(b)
    if ca and cb:
        return ca == cb
    ka, kb = _key(a), _key(b)
    return bool(ka) and ka == kb


def find_in_parts(parts):
    """First address part that is a known barangay (canonical name), else ''."""
    for part in parts or []:
        name = canonical(part)
        if name:
            return name
    return ''