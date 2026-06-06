"""
importMicrosoftFootprints.py
Import AI-detected building footprints from Microsoft GlobalMLBuildingFootprints
and enrich them with OSM attributes (name, height, usage, year).

WHY USE THIS INSTEAD OF osmToXML.py?
  OSM contributors draw building outlines by hand from satellite imagery —
  shapes are often rectangles even for L-shaped or U-shaped buildings.
  Microsoft uses computer vision on the same imagery and produces much more
  accurate polygon footprints, including complex shapes.
  This script combines Microsoft's accurate SHAPES with OSM's rich ATTRIBUTES.

HOW IT WORKS:
  1. Identify which quadkey tile(s) cover the query area (zoom level 9)
  2. Download dataset-links.csv once to get tile URLs (cached)
  3. Download the relevant tile(s) once (cached in ~/.3dcadastre_ms/)
  4. Spatially filter buildings within the query radius
  5. Fetch OSM buildings for the same area via Overpass API
  6. Match each Microsoft building to its nearest OSM building (≤30 m)
     — if matched: use Microsoft shape + OSM name/height/usage/year
     — if unmatched: use Microsoft shape + heuristic estimates
  7. Write BuildingInformation.xml (same format as osmToXML.py)

USAGE:
  python importMicrosoftFootprints.py --city Nairobi --radius 500 --output nairobi.xml
  python importMicrosoftFootprints.py --lat -1.2921 --lon 36.8219 --radius 500 --output nairobi.xml
  python importMicrosoftFootprints.py --city Mombasa --radius 800 --output mombasa.xml

THEN:
  python exportToGeoJSON.py -i nairobi.xml -o nairobi.geojson --roads --parks
  python exportToGLTF.py    -i nairobi.xml -o nairobi.glb   --lod 2
  # drop nairobi.geojson onto viewer.html for the geographic view

FIRST-RUN NOTE:
  The Nairobi tile is ~124 MB (covers the greater Nairobi area).
  It is downloaded once and cached; subsequent queries are instant.
  Smaller cities (Thika, Nyeri, Malindi) have much smaller tiles.

REQUIREMENTS:
  pip install requests lxml
"""

import argparse, gzip, io, json, math, os, re, sys, uuid
from lxml import etree

try:
    import requests
except ImportError:
    print("requests is required: pip install requests"); sys.exit(1)

# ── CLI ──────────────────────────────────────────────────────────────────────

PARSER = argparse.ArgumentParser(
    description="Microsoft building footprints + OSM attributes -> BuildingInformation.xml")
PARSER.add_argument("--city",   help="Kenyan city (Nairobi/Mombasa/Kisumu/Nakuru/Eldoret/Thika/Nyeri/Malindi)")
PARSER.add_argument("--lat",    type=float, help="Custom centre latitude  (overrides --city)")
PARSER.add_argument("--lon",    type=float, help="Custom centre longitude (overrides --city)")
PARSER.add_argument("--radius", type=int,   default=600, help="Radius in metres (default 600)")
PARSER.add_argument("--output", required=True, help="Output XML filename")
PARSER.add_argument("--max-buildings", type=int, default=10000,
                    help="Cap on Microsoft buildings (default 10 000)")
PARSER.add_argument("--match-radius", type=float, default=30.0,
                    help="Max metres for OSM↔Microsoft centroid matching (default 30)")
ARGS = PARSER.parse_args()

# ── City centres (EPSG:4326) ──────────────────────────────────────────────────

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

# ── Microsoft dataset constants ───────────────────────────────────────────────

LINKS_URL  = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
CACHE_DIR  = os.path.join(os.path.expanduser("~"), ".3dcadastre_ms")
LINKS_CACHE = os.path.join(CACHE_DIR, "dataset-links.csv")
os.makedirs(CACHE_DIR, exist_ok=True)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# ── UTM Zone 37S helper (same as osmToXML.py) ────────────────────────────────

def latlon_to_utm37s(lat, lon):
    a  = 6378137.0; f = 1/298.257223563; b = a*(1-f)
    e2 = 1-(b/a)**2; k0 = 0.9996; lon0 = math.radians(39)
    lat_r = math.radians(lat); lon_r = math.radians(lon)
    N  = a/math.sqrt(1-e2*math.sin(lat_r)**2)
    T  = math.tan(lat_r)**2; C = e2/(1-e2)*math.cos(lat_r)**2
    A  = math.cos(lat_r)*(lon_r-lon0)
    e4 = e2**2; e6 = e2**3
    M  = a*((1-e2/4-3*e4/64-5*e6/256)*lat_r
            -(3*e2/8+3*e4/32+45*e6/1024)*math.sin(2*lat_r)
            +(15*e4/256+45*e6/1024)*math.sin(4*lat_r)
            -(35*e6/3072)*math.sin(6*lat_r))
    E  = k0*N*(A+(1-T+C)*A**3/6+(5-18*T+T**2+72*C-58*e2/(1-e2))*A**5/120)+500000
    Nv = k0*(M+N*math.tan(lat_r)*(A**2/2+(5-T+9*C+4*C**2)*A**4/24
             +(61-58*T+T**2+600*C-330*e2/(1-e2))*A**6/720))
    if lat < 0: Nv += 10_000_000
    return round(E, 2), round(Nv, 2)

# ── Geometry helpers ──────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    d_lat = math.radians(lat2-lat1); d_lon = math.radians(lon2-lon1)
    a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(d_lon/2)**2
    return 2*R*math.asin(math.sqrt(a))

def centroid(coords_lonlat):
    """Mean centroid of a polygon ring [[lon,lat],...]."""
    lons = [c[0] for c in coords_lonlat[:-1]]
    lats = [c[1] for c in coords_lonlat[:-1]]
    return sum(lats)/len(lats), sum(lons)/len(lons)

def polygon_area_m2(coords_lonlat):
    """Approximate area in m² via shoelace on local Cartesian."""
    ring = coords_lonlat[:-1]
    if len(ring) < 3: return 0
    lat0, lon0 = ring[0][1], ring[0][0]
    mpl = 111320.0
    mpp = 111320.0 * math.cos(math.radians(lat0))
    xs = [(c[0]-lon0)*mpp for c in ring]
    ys = [(c[1]-lat0)*mpl for c in ring]
    n = len(xs)
    area = sum(xs[i]*ys[(i+1)%n] - xs[(i+1)%n]*ys[i] for i in range(n))
    return abs(area)/2

def bbox_from_centre(lat, lon, radius_m):
    """Bounding box (lat_min,lon_min,lat_max,lon_max) for a circle."""
    d_lat = math.degrees(radius_m / 6_371_000)
    d_lon = math.degrees(radius_m / (6_371_000 * math.cos(math.radians(lat))))
    return lat-d_lat, lon-d_lon, lat+d_lat, lon+d_lon

# ── Quadkey helpers ───────────────────────────────────────────────────────────

def _tile_xy(lat, lon, zoom):
    lat_r = math.radians(lat); n = 1 << zoom
    x = int((lon+180)/360*n)
    y = int((1 - math.log(math.tan(lat_r)+1/math.cos(lat_r))/math.pi)/2*n)
    # clamp
    y = max(0, min(n-1, y))
    return x, y

def _quadkey(x, y, zoom):
    qk = []
    for i in range(zoom, 0, -1):
        d = 0; mask = 1 << (i-1)
        if x & mask: d += 1
        if y & mask: d += 2
        qk.append(str(d))
    return "".join(qk)

def covering_quadkeys(lat_min, lon_min, lat_max, lon_max, zoom=9):
    """All zoom-9 quadkeys that cover the bounding box."""
    x0, y1 = _tile_xy(lat_max, lon_min, zoom)
    x1, y0 = _tile_xy(lat_min, lon_max, zoom)
    return {_quadkey(x, y, zoom) for x in range(x0, x1+1) for y in range(y0, y1+1)}

# ── Dataset index ─────────────────────────────────────────────────────────────

def load_tile_index(force_refresh=False):
    """
    Download and cache dataset-links.csv.
    Returns dict: quadkey -> url
    """
    if not os.path.exists(LINKS_CACHE) or force_refresh:
        print("Downloading Microsoft dataset index (~2 MB, one-time) …")
        headers = {"User-Agent": "3D-Cadastre-Kenya/1.0"}
        r = requests.get(LINKS_URL, headers=headers, timeout=60)
        r.raise_for_status()
        with open(LINKS_CACHE, "w", encoding="utf-8") as f:
            f.write(r.text)
        print("  Cached to", LINKS_CACHE)

    index = {}   # quadkey -> url
    with open(LINKS_CACHE, encoding="utf-8") as f:
        next(f)   # skip header
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 3:
                index[parts[1]] = parts[2]
    return index

# ── Tile download & parse ─────────────────────────────────────────────────────

def download_tile(quadkey_str, url):
    """
    Download (and cache) a single tile. Returns list of GeoJSON features.
    Files are gzipped line-delimited GeoJSON.
    """
    cache_path = os.path.join(CACHE_DIR, f"{quadkey_str}.geojsonl.gz")
    if not os.path.exists(cache_path):
        print(f"  Downloading tile {quadkey_str} from Microsoft …")
        headers = {"User-Agent": "3D-Cadastre-Kenya/1.0"}
        r = requests.get(url, headers=headers, timeout=300, stream=True)
        r.raise_for_status()
        downloaded = 0
        with open(cache_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024*1024):
                f.write(chunk)
                downloaded += len(chunk)
                print(f"\r    {downloaded/1024/1024:.1f} MB", end="", flush=True)
        print(f"\r    Done ({downloaded/1024/1024:.1f} MB cached)")
    else:
        print(f"  Using cached tile {quadkey_str}")

    features = []
    with gzip.open(cache_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                features.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return features

# ── OSM attribute enrichment ──────────────────────────────────────────────────

ROAD_WIDTHS = {
    "motorway":14,"trunk":12,"primary":10,"secondary":8,"tertiary":6,
    "residential":5,"service":3.5,"unclassified":5,"footway":2,
    "path":1.5,"cycleway":2,"living_street":4,"pedestrian":4,
}

def fetch_osm_data(lat, lon, radius):
    """Fetch OSM buildings + roads + parks for the area."""
    api_timeout = min(180, max(60, radius//10))
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
out body;>;out skel qt;
"""
    print(f"  Fetching OSM attributes within {radius} m …")
    headers = {"User-Agent": "3D-Cadastre-Kenya/1.0"}
    r = requests.post(OVERPASS_URL, data={"data": query}, headers=headers,
                      timeout=api_timeout+30)
    r.raise_for_status()
    return r.json()

def parse_osm_buildings(data):
    """
    Parse Overpass response -> list of OSM buildings.
    Each entry: {centroid:(lat,lon), tags:{}, nodes:[(lat,lon)]}
    """
    nodes = {el["id"]: (el["lat"],el["lon"])
             for el in data["elements"] if el["type"]=="node"}
    way_coords = {}
    for el in data["elements"]:
        if el["type"] == "way":
            coords = [nodes[n] for n in el.get("nodes",[]) if n in nodes]
            way_coords[el["id"]] = coords

    buildings = []
    for el in data["elements"]:
        tags = el.get("tags",{})
        if "building" not in tags: continue
        if el["type"] == "way":
            coords = way_coords.get(el["id"],[])
        elif el["type"] == "relation":
            outer = [m["ref"] for m in el.get("members",[])
                     if m.get("type")=="way" and m.get("role") in ("outer","")]
            coords = []
            for wid in outer:
                coords.extend(way_coords.get(wid,[]))
        else:
            continue
        if len(coords) < 3: continue
        lats = [c[0] for c in coords]; lons = [c[1] for c in coords]
        cen  = (sum(lats)/len(lats), sum(lons)/len(lons))
        buildings.append({"id": el["id"], "centroid": cen,
                          "tags": tags, "nodes": coords})
    return buildings

def parse_osm_roads_parks(data):
    """Parse roads and parks from Overpass response."""
    nodes = {el["id"]: (el["lat"],el["lon"])
             for el in data["elements"] if el["type"]=="node"}
    roads, parks = [], []
    for el in data["elements"]:
        if el["type"] != "way": continue
        tags   = el.get("tags",{})
        coords = [nodes[n] for n in el.get("nodes",[]) if n in nodes]
        if "highway" in tags and len(coords)>=2:
            hw = tags["highway"]
            roads.append({"id":el["id"],"nodes":coords,"tags":tags,
                          "type":hw,"width":float(tags.get("width",ROAD_WIDTHS.get(hw,4.0)))})
        elif any(k in tags for k in ("leisure","landuse","natural")) and len(coords)>=4:
            parks.append({"id":el["id"],"nodes":coords,"tags":tags})
    return roads, parks

# ── Attribute helpers (mirrors osmToXML.py) ───────────────────────────────────

_FLOORS_BY_TYPE = {
    "house":1,"detached":1,"semidetached_house":2,"terrace":2,"bungalow":1,
    "apartments":4,"residential":3,"dormitory":3,"office":5,"commercial":3,
    "retail":2,"supermarket":1,"hotel":6,"hospital":4,"school":2,
    "university":3,"warehouse":1,"industrial":2,"garage":1,"yes":2,
}

def _height_raw(tags):
    for k in ("height","building:height","max_height"):
        v = tags.get(k)
        if v:
            s = str(v).strip()
            try:
                h = float(re.sub(r"[^\d.]","",s.replace("ft","")))
                if "ft" in s.lower(): h *= 0.3048
                if 1<=h<=600: return round(h,1)
            except (ValueError,TypeError): pass
    return None

def _floors(tags):
    for k in ("building:levels","levels","building:floors","building:storey"):
        v = tags.get(k)
        if v:
            try:
                f = int(float(str(v).split(";")[0].strip()))
                if 1<=f<=200: return f,"osm"
            except (ValueError,AttributeError): pass
    h = _height_raw(tags)
    if h: return max(1,round(h/3.2)),"height"
    return _FLOORS_BY_TYPE.get(tags.get("building","yes").lower(),2),"type"

def _height(tags, floors):
    h = _height_raw(tags)
    if h: return h
    floor_h = 4.5 if tags.get("building","").lower() in ("warehouse","industrial") else 3.2
    return round(floors*floor_h,1)

def _year(tags):
    for k in ("start_date","construction_date","building:year",
              "year_of_construction","opening_date","building:start_date"):
        v = tags.get(k,"")
        if v:
            m = re.search(r'\b(1[5-9]\d{2}|20[012]\d)\b',str(v))
            if m:
                yr = int(m.group())
                if 1500<=yr<=2025: return yr
    return None

def _usage(tags):
    b       = tags.get("building","yes").lower()
    amenity = tags.get("amenity","").lower()
    office  = tags.get("office","").lower()
    shop    = tags.get("shop","").lower()
    tourism = tags.get("tourism","").lower()
    landuse = tags.get("landuse","").lower()
    if b in ("residential","apartments","house","detached",
             "semidetached_house","terrace","bungalow","dormitory"):
        return "Residential"
    if b in ("commercial","retail","office","supermarket","hotel","bank",
             "shopping_centre","kiosk","service") or office or shop:
        return "Commercial"
    if tourism in ("hotel","hostel","motel","apartment","guest_house"):
        return "Commercial"
    if amenity in ("restaurant","cafe","fast_food","bar","pub","bank",
                   "pharmacy","marketplace","fuel","parking"): return "Commercial"
    if b in ("industrial","warehouse","factory","hangar"): return "Industrial"
    if landuse in ("industrial","port"): return "Industrial"
    if b in ("church","mosque","cathedral","temple","hospital","school",
             "university","college","government","civic"): return "Civic"
    if amenity in ("school","hospital","clinic","university","college",
                   "place_of_worship","community_centre","police","courthouse",
                   "embassy","town_hall","library","theatre","cinema"): return "Civic"
    return "Unknown"

def _heuristic_floors(area_m2):
    """Estimate floors from Microsoft footprint area when no OSM match."""
    if area_m2 > 5000: return 8
    if area_m2 > 2000: return 5
    if area_m2 > 1000: return 4
    if area_m2 >  500: return 3
    if area_m2 >  200: return 2
    return 1

# ── XML writer ────────────────────────────────────────────────────────────────

def building_to_xml(parent, osm_id, ox, oy, xs, ys, zs, floors,
                    rtype, usage, year, name, utm_polygon_txt):
    bid = str(uuid.uuid4())
    b = etree.SubElement(parent, "building")
    b.attrib["ID"] = bid

    etree.SubElement(b, "footprint").text    = "Polygon"   # Microsoft = real polygon
    etree.SubElement(b, "origin").text       = f"{ox} {oy} 0"
    etree.SubElement(b, "order").text        = "0 0"
    etree.SubElement(b, "rotation").text     = "0"
    etree.SubElement(b, "xSize").text        = str(xs)
    etree.SubElement(b, "ySize").text        = str(ys)
    etree.SubElement(b, "zSize").text        = str(round(zs,2))
    etree.SubElement(b, "floors").text       = str(floors)
    floor_h = round(zs/floors,2) if floors else 3.2
    etree.SubElement(b, "floorHeight").text  = str(floor_h)
    etree.SubElement(b, "embrasure").text    = "0.10"
    etree.SubElement(b, "WallThickness").text = "0.20"
    etree.SubElement(b, "joist").text        = "0.25"

    roof = etree.SubElement(b, "roof")
    etree.SubElement(roof, "roofType").text = rtype
    if rtype != "Flat":
        etree.SubElement(roof, "h").text = str(round(min(3.5, zs*0.35),2))
        if rtype in ("Hipped","Pyramidal"):
            etree.SubElement(roof, "r").text = str(round(ys*0.35,2))
    ovh = etree.SubElement(roof, "overhangs")
    etree.SubElement(ovh, "xlength").text = "0.30"
    etree.SubElement(ovh, "ylength").text = "0.30"

    props = etree.SubElement(b, "properties")
    etree.SubElement(props, "roofType").text = rtype
    etree.SubElement(props, "usage").text    = usage
    etree.SubElement(props, "yearOfConstruction").text = str(year) if year else "Unknown"
    etree.SubElement(props, "valuation").text = "—"
    etree.SubElement(props, "source").text    = "Microsoft GlobalMLBuildingFootprints"
    if osm_id:
        etree.SubElement(props, "osmID").text = str(osm_id)
    if name:
        etree.SubElement(props, "name").text  = name

    # Store the actual Microsoft polygon (accurate shape for GeoJSON export)
    poly_el = etree.SubElement(b, "utmPolygon")
    poly_el.text = utm_polygon_txt

    return b

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Resolve centre
    if ARGS.lat is not None and ARGS.lon is not None:
        clat, clon = ARGS.lat, ARGS.lon
        label = f"({clat:.4f}, {clon:.4f})"
    elif ARGS.city:
        city = ARGS.city.strip().title()
        if city not in CITY_CENTRES:
            print(f"Unknown city. Supported: {', '.join(CITY_CENTRES)}")
            sys.exit(1)
        clat, clon = CITY_CENTRES[city]
        label = city
    else:
        print("Provide --city or --lat/--lon")
        sys.exit(1)

    radius = ARGS.radius
    print(f"\n3D Cadastre Kenya — Microsoft Building Footprints")
    print(f"Location : {label}  |  Radius : {radius} m\n")

    # ── Step 1: identify tiles ────────────────────────────────────────────────
    lat_min, lon_min, lat_max, lon_max = bbox_from_centre(clat, clon, radius)
    qkeys = covering_quadkeys(lat_min, lon_min, lat_max, lon_max, zoom=9)
    print(f"Quadkey tiles needed: {qkeys}")

    # ── Step 2: load tile index ───────────────────────────────────────────────
    print("\n[1/4] Loading Microsoft tile index …")
    index = load_tile_index()

    # ── Step 3: download & filter tiles ──────────────────────────────────────
    print(f"\n[2/4] Downloading tile data (cached after first run) …")
    ms_features = []
    for qk in sorted(qkeys):
        url = index.get(qk)
        if not url:
            print(f"  Tile {qk} not found in index (area may have no MS data)")
            continue
        features = download_tile(qk, url)
        print(f"  Tile {qk}: {len(features):,} buildings in tile")

        # Spatial filter: keep only buildings within radius
        for feat in features:
            geom = feat.get("geometry",{})
            if geom.get("type") != "Polygon": continue
            coords = geom["coordinates"][0]   # outer ring [[lon,lat],...]
            if not coords: continue
            clat_b, clon_b = centroid(coords)
            if haversine_m(clat, clon, clat_b, clon_b) <= radius:
                ms_features.append(feat)

    print(f"  -> {len(ms_features):,} buildings within {radius} m radius")

    if not ms_features:
        print("No Microsoft buildings found. Try increasing --radius.")
        sys.exit(0)

    if len(ms_features) > ARGS.max_buildings:
        print(f"  Capping at {ARGS.max_buildings} (use --max-buildings N to raise)")
        ms_features = ms_features[:ARGS.max_buildings]

    # ── Step 4: OSM attributes ────────────────────────────────────────────────
    print(f"\n[3/4] Fetching OSM attributes (names, heights, usage) …")
    try:
        osm_data  = fetch_osm_data(clat, clon, radius)
        osm_bldgs = parse_osm_buildings(osm_data)
        osm_roads, osm_parks = parse_osm_roads_parks(osm_data)
        print(f"  OSM: {len(osm_bldgs)} buildings, {len(osm_roads)} roads, {len(osm_parks)} parks")
    except Exception as e:
        print(f"  OSM fetch failed ({e}) — using heuristics only")
        osm_bldgs, osm_roads, osm_parks = [], [], []

    # ── Step 5: match Microsoft ↔ OSM ─────────────────────────────────────────
    print(f"\n[4/4] Matching footprints to OSM attributes …")
    match_r = ARGS.match_radius

    matched = 0
    root = etree.Element("specifications")

    for feat in ms_features:
        coords   = feat["geometry"]["coordinates"][0]   # [[lon,lat],...]
        ms_clat, ms_clon = centroid(coords)
        area_m2  = polygon_area_m2(coords)

        # Find nearest OSM building centroid
        best_osm  = None
        best_dist = float("inf")
        for ob in osm_bldgs:
            d = haversine_m(ms_clat, ms_clon, ob["centroid"][0], ob["centroid"][1])
            if d < best_dist:
                best_dist = d
                best_osm  = ob

        if best_osm and best_dist <= match_r:
            # Matched — use OSM attributes
            tags   = best_osm["tags"]
            osm_id = best_osm["id"]
            name   = tags.get("name") or tags.get("building:name") or None
            floors, _ = _floors(tags)
            zs     = _height(tags, floors)
            usage  = _usage(tags)
            year   = _year(tags)
            rtype  = ("Flat" if tags.get("building","").lower() in
                      ("warehouse","industrial","supermarket","office","commercial")
                      else "Flat")   # simple default; roof shape not in MS data
            matched += 1
        else:
            # Unmatched — heuristics from footprint size
            floors  = _heuristic_floors(area_m2)
            zs      = round(floors * 3.2, 1)
            usage   = "Unknown"
            year    = None
            name    = None
            osm_id  = None
            rtype   = "Flat"

        # Bounding box in UTM (for origin/xSize/ySize in XML)
        utm_pts = [latlon_to_utm37s(lat, lon) for lon, lat in coords]
        es = [p[0] for p in utm_pts]; ns = [p[1] for p in utm_pts]
        e0, e1 = min(es), max(es)
        n0, n1 = min(ns), max(ns)
        xs = round(e1-e0, 2); ys = round(n1-n0, 2)
        if xs < 0.5 or ys < 0.5: continue

        # Actual UTM polygon text (used by exportToGeoJSON.py for real shapes)
        utm_poly_txt = " ".join(f"{e:.2f} {n:.2f}" for e, n in utm_pts)

        building_to_xml(root, osm_id, round(e0,2), round(n0,2), xs, ys,
                        zs, floors, rtype, usage, year, name, utm_poly_txt)

    # ── Roads & parks ─────────────────────────────────────────────────────────
    if osm_roads:
        roads_el = etree.SubElement(root, "roads")
        for rd in osm_roads:
            utm_pts  = [latlon_to_utm37s(lat,lon) for lat,lon in rd["nodes"]]
            flat     = " ".join(f"{e:.2f} {n:.2f}" for e,n in utm_pts)
            road_el  = etree.SubElement(roads_el, "road")
            road_el.attrib["type"]  = rd["type"]
            road_el.attrib["width"] = str(rd["width"])
            etree.SubElement(road_el, "nodes").text = flat

    if osm_parks:
        parks_el = etree.SubElement(root, "parks")
        for pk in osm_parks:
            utm_pts = [latlon_to_utm37s(lat,lon) for lat,lon in pk["nodes"][:-1]]
            if len(utm_pts) < 3: continue
            flat     = " ".join(f"{e:.2f} {n:.2f}" for e,n in utm_pts)
            park_el  = etree.SubElement(parks_el, "park")
            etree.SubElement(park_el, "polygon").text = flat

    # ── Write XML ─────────────────────────────────────────────────────────────
    xml_bytes = etree.tostring(root, pretty_print=True)
    with open(ARGS.output, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write(f'<!-- 3D Cadastre Kenya — Microsoft footprints + OSM attributes\n')
        f.write(f'     Location: {label}  Radius: {radius} m\n')
        f.write(f'     Buildings: {len(root.findall("building"))} '
                f'({matched} with OSM match) -->\n')
        f.write(xml_bytes.decode("utf-8"))

    total = len(root.findall("building"))
    pct   = round(matched/total*100) if total else 0
    print(f"\nDone.")
    print(f"  Buildings written : {total:,}")
    print(f"  OSM-matched       : {matched:,} ({pct}%) — name/height/usage from OSM")
    print(f"  Heuristic only    : {total-matched:,} ({100-pct}%) — shape from MS, attributes estimated")
    print(f"  Output file       : {ARGS.output}")
    print()
    print("Next steps:")
    print(f"  python exportToGeoJSON.py -i {ARGS.output} -o city.geojson --roads --parks")
    print(f"  python exportToGLTF.py    -i {ARGS.output} -o city.glb   --lod 2")
    print(f"  # drop city.geojson onto viewer.html for geographic view")


if __name__ == "__main__":
    main()
