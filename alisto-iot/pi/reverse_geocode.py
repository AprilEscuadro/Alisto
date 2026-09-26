"""Offline reverse geocoding: given a (lat, lon) fix, finds which Cebu City
barangay it falls inside, so the emergency SMS can say something a person
can actually recognize without internet or a maps app -- "Lahug, Cebu City"
instead of raw numbers like "10.322417,123.902479".

NOT hardcoded to one address: this does a real point-in-polygon lookup
against Cebu City's 80 barangay boundaries, so it stays correct even if the
device is ever moved to a different barangay. The only thing bundled ahead
of time is the boundary shapes themselves (data/cebu_city_barangays.json,
source: faeldon/philippines-json-maps, 2019 PSGC boundaries, lowres) --
downloading that dataset was the only step that ever needed internet.
Nothing at lookup time (barangay_for()) touches the network.

Covers Cebu City only. A fix that falls outside all 80 polygons (device
moved to a different city, or GPS drift right at a boundary) returns None
-- callers fall back to plain coordinates rather than guessing a name.
"""

import json
import os

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "cebu_city_barangays.json")

_polygons = None  # loaded once: list of (rings, barangay_name) tuples


def _load():
    global _polygons
    if _polygons is not None:
        return
    _polygons = []
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
        for feat in data["features"]:
            geom = feat["geometry"]
            name = feat["properties"].get("ADM4_EN", "Unknown")
            name = name.replace(" (Pob.)", "")  # "(Pob.)" = poblacion district marker, noisy for an SMS
            if geom["type"] == "Polygon":
                polys = [geom["coordinates"]]
            elif geom["type"] == "MultiPolygon":
                polys = geom["coordinates"]
            else:
                continue
            for rings in polys:
                _polygons.append((rings, name))
        print(f"reverse_geocode: loaded {len(_polygons)} barangay boundaries")
    except Exception as e:
        print(f"reverse_geocode: failed to load barangay data ({e})")
        _polygons = []


def _point_in_ring(x, y, ring):
    """Standard ray-casting point-in-polygon test."""
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_rings(x, y, rings):
    # rings[0] is the outer boundary, any further rings are holes cut out of it
    if not _point_in_ring(x, y, rings[0]):
        return False
    for hole in rings[1:]:
        if _point_in_ring(x, y, hole):
            return False
    return True


def barangay_for(lat, lon):
    """Returns the barangay name containing (lat, lon), or None if it isn't
    inside any known Cebu City barangay."""
    _load()
    for rings, name in _polygons:
        if _point_in_rings(lon, lat, rings):
            return name
    return None
