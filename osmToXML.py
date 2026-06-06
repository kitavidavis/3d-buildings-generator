"""
osmToXML.py — Convert real OSM building data to BuildingInformation.xml

Fetches building footprints from OpenStreetMap for any Kenyan city and
converts them to the XML format that exportToOBJ.py and generateCityGML.py
understand.  No shapefile download needed — data comes live from the
Overpass API.

Usage:
    python osmToXML.py --city Nairobi --output nairobi.xml
    python osmToXML.py --city Mombasa --output mombasa.xml --radius 800
    python osmToXML.py --lat -1.2921 --lon 36.8219 --radius 500 --output custom.xml

Then export to 3D:
    python exportToOBJ.py  -i nairobi.xml -o nairobi.obj  --lod 2
    python exportToGLTF.py -i nairobi.xml -o nairobi.glb  --lod 2
    mkdir gml && python generateCityGML.py -i nairobi.xml -o gml/

Requirements:
    pip install requests lxml
"""

import argparse, math, random, re, sys, uuid
from lxml import etree

try:
    import requests
except ImportError:
    print("requests is required: pip install requests"); sys.exit(1)

# ── CLI ──────────────────────────────────────────────────────────────────────

PARSER = argparse.ArgumentParser(description="OSM buildings → BuildingInformation.xml (Kenya 3D Cadastre)")
PARSER.add_argument("--city",   help="Kenyan city name (Nairobi, Mombasa, Kisumu, Nakuru, Eldoret, Thika, Nyeri, Malindi)")
PARSER.add_argument("--lat",    type=float, help="Custom centre latitude  (overrides --city)")
PARSER.add_argument("--lon",    type=float, help="Custom centre longitude (overrides --city)")
PARSER.add_argument("--radius", type=int, default=600,
    help="Query radius in metres (default 600; practical max ~2000 for dense cities)")
PARSER.add_argument("--max-buildings", type=int, default=5000,
    help="Stop after this many buildings to avoid huge files (default 5000)")
PARSER.add_argument("--output", required=True, help="Output XML filename")
ARGS = PARSER.parse_args()

# ── Known Kenyan city centres (lat, lon) ─────────────────────────────────────

CITY_CENTRES = {
    "Nairobi":  (-1.2921,  36.8219),
    "Mombasa":  (-4.0435,  39.6682),
    "Kisumu":   (-0.1022,  34.7617),
    "Nakuru":   (-0.3031,  36.0800),
    "Eldoret":  ( 0.5143,  35.2698),
    "Thika":    (-1.0332,  37.0693),
    "Nyeri":    (-0.4167,  36.9500),
    "Malindi":  (-3.2138,  40.1169),
}

# ── UTM helpers (Arc 1960 / WGS84 → EPSG:21037 approx) ──────────────────────

def _deg2rad(d): return d * math.pi / 180.0

def latlon_to_utm37s(lat, lon):
    """
    Approximate conversion from geographic (Arc 1960 ≈ WGS84 for our purposes)
    to UTM Zone 37S (EPSG:21037).  Accurate to ~1 m for Kenya — good enough
    for procedural cadastral visualisation.
    """
    a     = 6378137.0          # WGS84 semi-major axis
    f     = 1 / 298.257223563
    b     = a * (1 - f)
    e2    = 1 - (b/a)**2
    e     = math.sqrt(e2)
    k0    = 0.9996
    lon0  = math.radians(39.0) # central meridian zone 37
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    N  = a / math.sqrt(1 - e2 * math.sin(lat_r)**2)
    T  = math.tan(lat_r)**2
    C  = e2 / (1 - e2) * math.cos(lat_r)**2
    A  = math.cos(lat_r) * (lon_r - lon0)
    e4 = e2**2; e6 = e2**3
    M  = a * (
        (1 - e2/4 - 3*e4/64 - 5*e6/256) * lat_r
        - (3*e2/8 + 3*e4/32 + 45*e6/1024) * math.sin(2*lat_r)
        + (15*e4/256 + 45*e6/1024) * math.sin(4*lat_r)
        - (35*e6/3072) * math.sin(6*lat_r)
    )

    easting = k0 * N * (
        A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*e2/(1-e2))*A**5/120
    ) + 500000.0

    northing = k0 * (M + N * math.tan(lat_r) * (
        A**2/2 + (5-T+9*C+4*C**2)*A**4/24
        + (61-58*T+T**2+600*C-330*e2/(1-e2))*A**6/720
    ))
    if lat < 0:
        northing += 10000000.0   # southern hemisphere false northing

    return easting, northing

# ── Overpass API fetch ────────────────────────────────────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

ROAD_WIDTHS = {
    "motorway": 14.0, "trunk": 12.0, "primary": 10.0, "secondary": 8.0,
    "tertiary": 6.0,  "residential": 5.0, "service": 3.5,
    "unclassified": 5.0, "footway": 2.0, "path": 1.5, "cycleway": 2.0,
    "living_street": 4.0, "pedestrian": 4.0,
}

def fetch_city_data(lat, lon, radius):
    """Fetch buildings, roads, and green areas from OSM in one query."""
    # Scale the Overpass server-side timeout with the query radius
    api_timeout = min(180, max(60, radius // 10))
    query = f"""
[out:json][timeout:{api_timeout}];
(
  way["building"](around:{radius},{lat},{lon});
  relation["building"](around:{radius},{lat},{lon});
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|footway|path|cycleway|living_street|pedestrian)$"](around:{radius},{lat},{lon});
  way["leisure"~"^(park|garden|recreation_ground|pitch|playground)$"](around:{radius},{lat},{lon});
  way["landuse"~"^(park|grass|forest|recreation_ground|village_green|meadow)$"](around:{radius},{lat},{lon});
  way["natural"~"^(wood|grassland|scrub)$"](around:{radius},{lat},{lon});
);
out body;
>;
out skel qt;
"""
    print(f"Fetching buildings, roads and green areas within {radius} m of ({lat:.4f}, {lon:.4f}) ...")
    headers = {"User-Agent": "3D-Cadastre-Kenya/1.0 (github.com/kitavidavis/3d-buildings-generator)"}
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=headers,
                          timeout=api_timeout + 30)
    resp.raise_for_status()
    return resp.json()

def _chain_ways(member_ids, way_coords):
    """
    Chain multiple OSM way segments into a single closed ring.
    member_ids: list of way IDs (in member order)
    way_coords: dict of way_id → [(lat,lon), ...]
    Returns a list of (lat,lon) forming a closed polygon, or [] on failure.
    """
    segments = [list(way_coords.get(wid, [])) for wid in member_ids if wid in way_coords]
    segments = [s for s in segments if len(s) >= 2]
    if not segments:
        return []
    if len(segments) == 1:
        return segments[0]

    # Greedy chaining: always append the segment whose start or end matches
    # the current tail of the result ring.
    result = list(segments[0])
    remaining = segments[1:]
    while remaining:
        tail = result[-1]
        matched = False
        for i, seg in enumerate(remaining):
            if seg[0] == tail:
                result.extend(seg[1:])
                remaining.pop(i); matched = True; break
            if seg[-1] == tail:
                result.extend(reversed(seg[:-1]))
                remaining.pop(i); matched = True; break
        if not matched:
            # Can't chain cleanly — just concatenate the rest
            for seg in remaining:
                result.extend(seg)
            break
    return result


def parse_osm(data):
    """
    Parse Overpass JSON into buildings, roads, and parks.
    Handles both way-based and relation-based (multipolygon) building footprints
    so complex L/U-shaped buildings are represented with their actual outline.

    Returns: (buildings, roads, parks)
      buildings: [{id, nodes:[(lat,lon)], tags}]
      roads:     [{id, nodes:[(lat,lon)], tags, width}]
      parks:     [{id, nodes:[(lat,lon)], tags}]
    """
    # Index all nodes
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])

    # Index all way coordinate lists (needed for relation assembly)
    way_coords = {}
    for el in data["elements"]:
        if el["type"] == "way":
            coords = [nodes[n] for n in el.get("nodes", []) if n in nodes]
            way_coords[el["id"]] = coords

    buildings, roads, parks = [], [], []

    # ── Ways ──────────────────────────────────────────────────────────────────
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags   = el.get("tags", {})
        coords = way_coords.get(el["id"], [])

        if "building" in tags and len(coords) >= 4:
            buildings.append({"id": el["id"], "nodes": coords, "tags": tags})

        elif "highway" in tags:
            if len(coords) >= 2:
                hw    = tags["highway"]
                width = float(tags.get("width", ROAD_WIDTHS.get(hw, 4.0)))
                roads.append({"id": el["id"], "nodes": coords, "tags": tags,
                              "type": hw, "width": width})

        elif any(k in tags for k in ("leisure", "landuse", "natural")):
            if len(coords) >= 4:
                parks.append({"id": el["id"], "nodes": coords, "tags": tags})

    # ── Relations (complex / multi-part buildings) ────────────────────────────
    # Many large buildings in Nairobi are mapped as OSM relations with an
    # "outer" ring assembled from several way segments.
    seen_building_ids = {b["id"] for b in buildings}
    for el in data["elements"]:
        if el["type"] != "relation":
            continue
        tags = el.get("tags", {})
        if "building" not in tags:
            continue

        # Collect outer-ring way IDs
        outer_ids = [m["ref"] for m in el.get("members", [])
                     if m.get("type") == "way" and m.get("role") in ("outer", "")]
        if not outer_ids:
            continue

        ring = _chain_ways(outer_ids, way_coords)
        if len(ring) >= 4:
            buildings.append({"id": el["id"], "nodes": ring, "tags": tags})

    return buildings, roads, parks

# ── Building attribute extraction ─────────────────────────────────────────────

# Default floors by OSM building type — used only when no tag is available.
# These are realistic medians, NOT random values.
_FLOORS_BY_TYPE = {
    "house": 1, "detached": 1, "semidetached_house": 2, "terrace": 2,
    "bungalow": 1, "cabin": 1, "hut": 1, "static_caravan": 1,
    "apartments": 4, "residential": 3, "dormitory": 3,
    "office": 5, "commercial": 3, "retail": 2, "supermarket": 1,
    "hotel": 6, "hospital": 4, "school": 2, "university": 3,
    "warehouse": 1, "industrial": 2, "garage": 1, "shed": 1,
    "church": 1, "mosque": 1, "cathedral": 2, "temple": 1,
    "yes": 2,
}

def _tag_floors(tags):
    """
    Return (floors, source) where source is 'osm', 'height', or 'type'.
    Never returns a random value — always based on real data or a type default.
    """
    # 1. Explicit floor count from OSM
    for key in ("building:levels", "levels", "building:floors",
                "building:storey", "building:storeys"):
        v = tags.get(key)
        if v:
            try:
                f = int(float(str(v).split(";")[0].strip()))
                if 1 <= f <= 200:
                    return f, "osm"
            except (ValueError, AttributeError):
                pass

    # 2. Estimate from height tag (÷ 3.2 m per floor average)
    h = _tag_height_raw(tags)
    if h:
        return max(1, round(h / 3.2)), "height"

    # 3. Default from building type
    btype = tags.get("building", "yes").lower()
    return _FLOORS_BY_TYPE.get(btype, 2), "type"


def _tag_height_raw(tags):
    """Return the raw OSM height value in metres, or None."""
    for key in ("height", "building:height", "max_height"):
        v = tags.get(key)
        if v:
            s = str(v).strip()
            try:
                h = float(re.sub(r"[^\d.]", "", s.replace("ft", "")))
                if "ft" in s.lower():
                    h *= 0.3048
                if 1.0 <= h <= 600.0:
                    return round(h, 1)
            except (ValueError, TypeError):
                pass
    return None


def _tag_height(tags, floors):
    """
    Return building height in metres.
    Priority: OSM height tag → floors × floor_height → type-based default.
    """
    h = _tag_height_raw(tags)
    if h:
        return h
    # Use a realistic per-floor height based on usage
    btype = tags.get("building", "yes").lower()
    floor_h = 4.5 if btype in ("warehouse", "industrial", "supermarket") else 3.2
    return round(floors * floor_h, 1)


def _tag_year(tags):
    """
    Extract year of construction from OSM tags.
    Returns int year or None — NEVER fabricates a value.
    """
    for key in ("start_date", "construction_date", "building:year",
                "year_of_construction", "opening_date", "building:start_date",
                "building:construction_year"):
        v = tags.get(key, "")
        if v:
            m = re.search(r'\b(1[5-9]\d{2}|20[012]\d)\b', str(v))
            if m:
                yr = int(m.group())
                if 1500 <= yr <= 2025:
                    return yr
    return None


def _tag_roof(tags):
    mapping = {
        "flat": "Flat", "gabled": "Gabled", "hipped": "Hipped",
        "pyramidal": "Pyramidal", "shed": "Shed", "dome": "Pyramidal",
        "onion": "Pyramidal", "mansard": "Hipped", "gambrel": "Gabled",
        "skillion": "Shed", "saltbox": "Gabled",
    }
    shape = tags.get("roof:shape", "").lower()
    if shape in mapping:
        return mapping[shape]
    # Default by building type — flat for commercial/industrial, varied for residential
    btype = tags.get("building", "yes").lower()
    if btype in ("warehouse", "industrial", "supermarket", "garage", "office",
                 "commercial", "retail", "hospital", "school"):
        return "Flat"
    return random.choice(["Flat", "Flat", "Gabled", "Hipped"])


def _tag_usage(tags):
    b       = tags.get("building", "yes").lower()
    amenity = tags.get("amenity", "").lower()
    landuse = tags.get("landuse", "").lower()
    shop    = tags.get("shop", "")
    if b in ("residential", "apartments", "house", "detached",
             "semidetached_house", "terrace", "bungalow", "dormitory"):
        return "Residential"
    if b in ("commercial", "retail", "office", "supermarket",
             "hotel", "bank") or shop:
        return "Commercial"
    if b in ("industrial", "warehouse", "factory"):
        return "Industrial"
    if amenity in ("school", "hospital", "clinic", "university", "college",
                   "place_of_worship", "community_centre"):
        return "Civic"
    return "Residential"  # default for unknown

# ── Footprint → bounding box ──────────────────────────────────────────────────

def footprint_bbox_utm(nodes):
    """
    Convert node list (lat,lon) to UTM eastings/northings and return
    (origin_e, origin_n, width_e, width_n) — the bounding box.
    Only rectangular approximation; sufficient for LOD1/LOD2 generation.
    """
    pts = [latlon_to_utm37s(lat, lon) for lat, lon in nodes[:-1]]  # drop closing node
    if not pts:
        return None
    es = [p[0] for p in pts]
    ns = [p[1] for p in pts]
    e0, e1 = min(es), max(es)
    n0, n1 = min(ns), max(ns)
    xs = round(e1 - e0, 2)
    ys = round(n1 - n0, 2)
    if xs < 1.0 or ys < 1.0:   # skip tiny slivers
        return None
    return round(e0, 2), round(n0, 2), xs, ys

# ── XML writer ────────────────────────────────────────────────────────────────

def building_to_xml(parent, bldg, ox, oy, oz, xs, ys, zs, floors, floor_h,
                    rtype, usage, bid, year=None, name=None):
    b = etree.SubElement(parent, "building")
    b.attrib["ID"] = bid

    etree.SubElement(b, "footprint").text    = "Rectangular"
    etree.SubElement(b, "origin").text       = f"{ox} {oy} {oz}"
    etree.SubElement(b, "order").text        = "0 0"
    etree.SubElement(b, "rotation").text     = "0"
    etree.SubElement(b, "xSize").text        = str(xs)
    etree.SubElement(b, "ySize").text        = str(ys)
    etree.SubElement(b, "zSize").text        = str(round(zs, 2))
    etree.SubElement(b, "floors").text       = str(floors)
    etree.SubElement(b, "floorHeight").text  = str(round(floor_h, 2))
    etree.SubElement(b, "embrasure").text    = "0.10"
    etree.SubElement(b, "WallThickness").text = "0.20"
    etree.SubElement(b, "joist").text        = "0.25"

    roof = etree.SubElement(b, "roof")
    etree.SubElement(roof, "roofType").text = rtype
    if rtype != "Flat":
        h_val = round(min(3.5, zs * 0.35), 2)
        etree.SubElement(roof, "h").text = str(h_val)
        if rtype in ("Hipped", "Pyramidal"):
            etree.SubElement(roof, "r").text = str(round(ys * 0.35, 2))
    ovh = etree.SubElement(roof, "overhangs")
    etree.SubElement(ovh, "xlength").text = "0.30"
    etree.SubElement(ovh, "ylength").text = "0.30"

    props = etree.SubElement(b, "properties")
    etree.SubElement(props, "roofType").text = rtype
    etree.SubElement(props, "usage").text    = usage
    # Year of construction — only if confirmed from OSM tags; never fabricated
    etree.SubElement(props, "yearOfConstruction").text = str(year) if year else "Unknown"
    etree.SubElement(props, "valuation").text = "—"   # not available from OSM

    # OSM metadata
    etree.SubElement(props, "osmID").text = str(bldg["id"])
    if name:
        etree.SubElement(props, "name").text = name

    return b

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Resolve centre coordinates
    if ARGS.lat is not None and ARGS.lon is not None:
        centre_lat, centre_lon = ARGS.lat, ARGS.lon
        label = f"({centre_lat:.4f}, {centre_lon:.4f})"
    elif ARGS.city:
        city = ARGS.city.strip().title()
        if city not in CITY_CENTRES:
            known = ", ".join(CITY_CENTRES.keys())
            print(f"Unknown city '{city}'. Supported: {known}")
            sys.exit(1)
        centre_lat, centre_lon = CITY_CENTRES[city]
        label = city
    else:
        print("Provide --city or both --lat and --lon")
        sys.exit(1)

    data = fetch_city_data(centre_lat, centre_lon, ARGS.radius)
    osm_buildings, osm_roads, osm_parks = parse_osm(data)
    print(f"Found {len(osm_buildings)} buildings, {len(osm_roads)} roads, "
          f"{len(osm_parks)} green areas.")

    if not osm_buildings:
        print("No buildings found. Try increasing --radius or a different location.")
        sys.exit(0)

    root = etree.Element("specifications")
    count = 0

    max_bldgs = ARGS.max_buildings
    if len(osm_buildings) > max_bldgs:
        print(f"Capping at {max_bldgs} buildings (found {len(osm_buildings)}). "
              f"Use --max-buildings N to raise the limit.")
        osm_buildings = osm_buildings[:max_bldgs]

    # ── Buildings ────────────────────────────────────────────────────────────
    for bldg in osm_buildings:
        bbox = footprint_bbox_utm(bldg["nodes"])
        if bbox is None:
            continue
        ox, oy, xs, ys = bbox
        tags    = bldg["tags"]
        floors, _src = _tag_floors(tags)
        zs      = _tag_height(tags, floors)
        floor_h = round(zs / floors, 2)
        rtype   = _tag_roof(tags)
        usage   = _tag_usage(tags)
        year    = _tag_year(tags)
        name    = tags.get("name") or tags.get("building:name") or None
        bid     = str(uuid.uuid4())
        b_el    = building_to_xml(root, bldg, ox, oy, 0.0, xs, ys, zs,
                                  floors, floor_h, rtype, usage, bid,
                                  year=year, name=name)

        # Store the actual polygon nodes in UTM so exportToGeoJSON.py can
        # reconstruct the real building footprint (not just the bounding box).
        utm_pts = [latlon_to_utm37s(lat, lon) for lat, lon in bldg["nodes"][:-1]]
        poly_el = etree.SubElement(b_el, "utmPolygon")
        poly_el.text = " ".join(f"{e:.2f} {n:.2f}" for e, n in utm_pts)

        count += 1

    # ── Roads ────────────────────────────────────────────────────────────────
    if osm_roads:
        roads_el = etree.SubElement(root, "roads")
        for rd in osm_roads:
            utm_pts = [latlon_to_utm37s(lat, lon) for lat, lon in rd["nodes"]]
            flat = " ".join(f"{e:.2f} {n:.2f}" for e, n in utm_pts)
            road_el = etree.SubElement(roads_el, "road")
            road_el.attrib["type"]  = rd["type"]
            road_el.attrib["width"] = str(rd["width"])
            etree.SubElement(road_el, "nodes").text = flat

    # ── Parks / green areas ──────────────────────────────────────────────────
    if osm_parks:
        parks_el = etree.SubElement(root, "parks")
        for pk in osm_parks:
            utm_pts = [latlon_to_utm37s(lat, lon) for lat, lon in pk["nodes"][:-1]]
            if len(utm_pts) < 3:
                continue
            flat = " ".join(f"{e:.2f} {n:.2f}" for e, n in utm_pts)
            park_el = etree.SubElement(parks_el, "park")
            etree.SubElement(park_el, "polygon").text = flat

    xml_bytes = etree.tostring(root, pretty_print=True)
    with open(ARGS.output, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write(f'<!-- 3D Cadastre Kenya — {label}, radius {ARGS.radius} m -->\n')
        f.write(xml_bytes.decode("utf-8"))

    print(f"Written {count} buildings to {ARGS.output}")
    print()
    print("Next steps:")
    print(f"  python exportToOBJ.py  -i {ARGS.output} -o city.obj  --lod 2")
    print(f"  python exportToGLTF.py -i {ARGS.output} -o city.glb  --lod 2")
    print(f"  mkdir gml && python generateCityGML.py -i {ARGS.output} -o gml/")

if __name__ == "__main__":
    main()
