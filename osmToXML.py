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

import argparse, math, random, sys, uuid
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
PARSER.add_argument("--radius", type=int,   default=600, help="Query radius in metres (default 600)")
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

def fetch_buildings(lat, lon, radius):
    """Fetch OSM building ways within radius metres of (lat, lon)."""
    query = f"""
[out:json][timeout:60];
(
  way["building"](around:{radius},{lat},{lon});
  relation["building"](around:{radius},{lat},{lon});
);
out body;
>;
out skel qt;
"""
    print(f"Fetching OSM buildings within {radius} m of ({lat:.4f}, {lon:.4f}) ...")
    headers = {"User-Agent": "3D-Cadastre-Kenya/1.0 (github.com/kitavidavis/3d-buildings-generator)"}
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=90)
    resp.raise_for_status()
    return resp.json()

def parse_osm(data):
    """
    Parse Overpass JSON into a list of buildings.
    Each building: {id, nodes: [(lat,lon),...], tags: {}}
    """
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])

    buildings = []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        if "building" not in el.get("tags", {}):
            continue
        coords = []
        for nid in el.get("nodes", []):
            if nid in nodes:
                coords.append(nodes[nid])
        if len(coords) < 4:   # need at least a triangle + closing node
            continue
        buildings.append({"id": el["id"], "nodes": coords, "tags": el.get("tags", {})})
    return buildings

# ── Building attribute extraction ─────────────────────────────────────────────

def _tag_floors(tags):
    for key in ("building:levels", "levels", "building:floors"):
        v = tags.get(key)
        if v:
            try: return max(1, int(float(v)))
            except ValueError: pass
    return random.randint(1, 8)

def _tag_height(tags, floors):
    v = tags.get("height") or tags.get("building:height")
    if v:
        try: return float(str(v).replace("m","").strip())
        except ValueError: pass
    floor_h = round(random.uniform(3.0, 3.5), 2)
    return round(floors * floor_h, 2)

def _tag_roof(tags):
    mapping = {
        "flat":      "Flat",
        "gabled":    "Gabled",
        "hipped":    "Hipped",
        "pyramidal": "Pyramidal",
        "shed":      "Shed",
        "dome":      "Pyramidal",
        "onion":     "Pyramidal",
        "mansard":   "Hipped",
        "gambrel":   "Gabled",
    }
    shape = tags.get("roof:shape", "").lower()
    return mapping.get(shape, random.choice(["Flat","Flat","Gabled","Hipped","Pyramidal","Shed"]))

def _tag_usage(tags):
    b = tags.get("building","yes").lower()
    amenity = tags.get("amenity","").lower()
    if b in ("residential","apartments","house","detached","terrace"):
        return "Residential"
    if b in ("commercial","retail","office","shop"):
        return "industrial"
    if amenity in ("school","hospital","clinic"):
        return "Residential"
    return "Residential"

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
                    rtype, usage, bid):
    b = etree.SubElement(parent, "building")
    b.attrib["ID"] = bid

    etree.SubElement(b, "footprint").text = "Rectangular"
    etree.SubElement(b, "origin").text    = f"{ox} {oy} {oz}"
    etree.SubElement(b, "order").text     = "0 0"   # positional order not needed for real data
    etree.SubElement(b, "rotation").text  = "0"
    etree.SubElement(b, "xSize").text     = str(xs)
    etree.SubElement(b, "ySize").text     = str(ys)
    etree.SubElement(b, "zSize").text     = str(round(zs, 2))
    etree.SubElement(b, "floors").text    = str(floors)
    etree.SubElement(b, "floorHeight").text = str(round(floor_h, 2))
    etree.SubElement(b, "embrasure").text   = str(round(random.uniform(0.05, 0.15), 2))
    etree.SubElement(b, "WallThickness").text = "0.20"
    etree.SubElement(b, "joist").text         = str(round(random.uniform(0.2, 0.3), 2))

    roof = etree.SubElement(b, "roof")
    etree.SubElement(roof, "roofType").text = rtype
    if rtype != "Flat":
        h_val = round(random.uniform(2.0, min(3.8, zs * 0.4)), 2)
        etree.SubElement(roof, "h").text = str(h_val)
        if rtype in ("Hipped", "Pyramidal"):
            etree.SubElement(roof, "r").text = str(round(ys * random.uniform(0.3, 0.45), 2))
    ovh = etree.SubElement(roof, "overhangs")
    etree.SubElement(ovh, "xlength").text = str(round(random.uniform(0.1, 0.5), 2))
    etree.SubElement(ovh, "ylength").text = str(round(random.uniform(0.1, 0.5), 2))

    props = etree.SubElement(b, "properties")
    etree.SubElement(props, "roofType").text = rtype
    etree.SubElement(props, "usage").text    = usage
    current_year = 2024
    age = random.randint(1, 60)
    etree.SubElement(props, "age").text              = str(age)
    etree.SubElement(props, "yearOfConstruction").text = str(current_year - age)
    etree.SubElement(props, "roofClearance").text    = random.choice(["yes","no"])
    etree.SubElement(props, "valuation").text        = str(random.randint(1, 5))

    # OSM source ID for traceability
    src = etree.SubElement(props, "osmID")
    src.text = str(bldg["id"])

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

    data = fetch_buildings(centre_lat, centre_lon, ARGS.radius)
    osm_buildings = parse_osm(data)
    print(f"Found {len(osm_buildings)} OSM building ways.")

    if not osm_buildings:
        print("No buildings found. Try increasing --radius or choosing a different location.")
        sys.exit(0)

    root = etree.Element("specifications")
    count = 0

    for bldg in osm_buildings:
        bbox = footprint_bbox_utm(bldg["nodes"])
        if bbox is None:
            continue

        ox, oy, xs, ys = bbox
        tags   = bldg["tags"]
        floors = _tag_floors(tags)
        zs     = _tag_height(tags, floors)
        floor_h = round(zs / floors, 2)
        rtype  = _tag_roof(tags)
        usage  = _tag_usage(tags)
        bid    = str(uuid.uuid4())

        building_to_xml(root, bldg, ox, oy, 0.0, xs, ys, zs,
                        floors, floor_h, rtype, usage, bid)
        count += 1

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
