"""
exportToGeoJSON.py — Convert BuildingInformation.xml → GeoJSON

Outputs real WGS84 coordinates so buildings align perfectly with
OpenStreetMap basemap tiles in viewer.html (MapLibre GL mode).

For OSM-sourced XML (from osmToXML.py) the actual building polygon
footprint is used. For procedural XML (from randomiseCity.py) the
rectangular bounding box is converted from EPSG:21037 (UTM Zone 37S).

Usage:
    python exportToGeoJSON.py -i nairobi.xml -o nairobi.geojson
    python exportToGeoJSON.py -i nairobi.xml -o nairobi.geojson --roads --parks

Then open viewer.html and drop the .geojson file — the map view
activates automatically and aligns buildings with the OSM basemap.
"""

import argparse, json, math
from lxml import etree

PARSER = argparse.ArgumentParser(description="Export buildings XML → GeoJSON (WGS84)")
PARSER.add_argument("-i", "--input",  required=True,  help="Input XML (BuildingInformation.xml)")
PARSER.add_argument("-o", "--output", required=True,  help="Output .geojson file")
PARSER.add_argument("--roads",  action="store_true", help="Include road centrelines")
PARSER.add_argument("--parks",  action="store_true", help="Include park polygons")
ARGS = PARSER.parse_args()

# ── UTM Zone 37S (EPSG:21037) → WGS84 ───────────────────────────────────────
# Accurate to ~1 m across Kenya. Same formula as viewer.html (JS version).

def utm37s_to_latlon(easting, northing):
    a  = 6378137.0
    f  = 1 / 298.257223563
    b  = a * (1 - f)
    e2 = 1 - (b / a) ** 2
    k0 = 0.9996
    lon0 = math.radians(39)

    E = easting  - 500000.0
    N = northing - 10000000.0   # southern hemisphere false northing

    e1  = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    M   = N / k0
    mu  = M / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))

    phi1 = (mu
            + (3*e1/2   - 27*e1**3/32)  * math.sin(2*mu)
            + (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
            + (151*e1**3/96)              * math.sin(6*mu)
            + (1097*e1**4/512)            * math.sin(8*mu))

    N1  = a / math.sqrt(1 - e2 * math.sin(phi1)**2)
    T1  = math.tan(phi1) ** 2
    C1  = e2 / (1 - e2) * math.cos(phi1) ** 2
    R1  = a * (1 - e2) / (1 - e2 * math.sin(phi1)**2) ** 1.5
    D   = E / (N1 * k0)

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D**2/2
        - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*e2/(1-e2)) * D**4/24
        + (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*e2/(1-e2) - 3*C1**2) * D**6/720
    )
    lon = lon0 + (
        D
        - (1 + 2*T1 + C1) * D**3/6
        + (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*e2/(1-e2) + 24*T1**2) * D**5/120
    ) / math.cos(phi1)

    return math.degrees(lat), math.degrees(lon)


def to_geojson_coord(e, n):
    """Return [lon, lat] (GeoJSON convention: longitude first)."""
    lat, lon = utm37s_to_latlon(e, n)
    return [round(lon, 7), round(lat, 7)]


# ── Polygon builders ──────────────────────────────────────────────────────────

def utm_poly_to_coords(poly_txt):
    """Parse stored <utmPolygon> text → closed GeoJSON ring [[lon,lat],...]."""
    vals   = [float(v) for v in poly_txt.split()]
    ring   = [to_geojson_coord(vals[i], vals[i+1]) for i in range(0, len(vals)-1, 2)]
    ring.append(ring[0])   # close the ring
    return ring


def bbox_to_coords(ox, oy, xs, ys):
    """Rectangular bounding box in UTM → closed GeoJSON ring."""
    corners = [(ox, oy), (ox+xs, oy), (ox+xs, oy+ys), (ox, oy+ys)]
    ring    = [to_geojson_coord(e, n) for e, n in corners]
    ring.append(ring[0])
    return ring


def node_txt_to_linecoords(nodes_txt):
    """Parse <nodes> centrelinetext → [[lon,lat],...]."""
    vals = [float(v) for v in nodes_txt.split()]
    return [to_geojson_coord(vals[i], vals[i+1]) for i in range(0, len(vals)-1, 2)]


# ── Colour helpers ────────────────────────────────────────────────────────────

_WALL_PALETTE = [
    "#ede7d0", "#d1d0cc", "#ccb894", "#e0d2ad", "#f2ede5", "#b3b9bf",
]
_ROOF_PALETTE = [
    "#9e3826", "#4d5259", "#477a47", "#8c4720", "#333337",
]

def _building_color(bid, usage):
    h = sum(ord(c) * (i+1) for i, c in enumerate(bid[:16]))
    wall = _WALL_PALETTE[h % len(_WALL_PALETTE)]
    roof = _ROOF_PALETTE[(h // 7) % len(_ROOF_PALETTE)]
    return wall, roof


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    root      = etree.parse(ARGS.input).getroot()
    buildings = root.findall("building")
    print(f"Converting {len(buildings)} buildings to GeoJSON ...")

    features = []

    for b in buildings:
        bid    = b.attrib.get("ID", "")
        origin = b.findtext("origin", "0 0 0").split()
        ox, oy = float(origin[0]), float(origin[1])
        xs     = float(b.findtext("xSize") or 5)
        ys     = float(b.findtext("ySize") or 5)
        zs     = float(b.findtext("zSize") or 3)
        floors = int(b.findtext("floors") or 1)

        # Prefer real polygon from OSM; fall back to bounding box
        poly_txt = b.findtext("utmPolygon")
        ring = utm_poly_to_coords(poly_txt) if poly_txt else bbox_to_coords(ox, oy, xs, ys)

        props_el = b.find("properties")
        props    = props_el if props_el is not None else etree.Element("_")
        usage    = props.findtext("usage") or "-"
        wall_col, roof_col = _building_color(bid, usage)

        osm_id = props.findtext("osmID")

        roof_el = b.find("roof")
        rtype   = roof_el.findtext("roofType", "Flat") if roof_el is not None else "Flat"

        feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "buildingID":       bid,
                "floors":           floors,
                "heightM":          round(zs, 1),
                "footprintAreaM2":  round(xs * ys, 1),
                "grossFloorAreaM2": round(xs * ys * floors, 1),
                "usage":            usage,
                "roofType":         rtype,
                "yearBuilt":        props.findtext("yearOfConstruction") or "-",
                "valuation":        props.findtext("valuation") or "-",
                "osmID":            int(osm_id) if osm_id else None,
                "wallColor":        wall_col,
                "roofColor":        roof_col,
            },
        }
        features.append(feature)

    # Roads (optional)
    roads_el = root.find("roads")
    if ARGS.roads and roads_el is not None:
        for rd in roads_el.findall("road"):
            nodes_txt = rd.findtext("nodes")
            if not nodes_txt:
                continue
            coords = node_txt_to_linecoords(nodes_txt)
            if len(coords) < 2:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "featureType": "road",
                    "roadType":    rd.attrib.get("type", "road"),
                    "widthM":      float(rd.attrib.get("width", 5)),
                },
            })

    # Parks (optional)
    parks_el = root.find("parks")
    if ARGS.parks and parks_el is not None:
        for pk in parks_el.findall("park"):
            poly_txt = pk.findtext("polygon")
            if not poly_txt:
                continue
            vals  = [float(v) for v in poly_txt.split()]
            ring  = [to_geojson_coord(vals[i], vals[i+1]) for i in range(0, len(vals)-1, 2)]
            if len(ring) < 3:
                continue
            ring.append(ring[0])
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"featureType": "park"},
            })

    geojson = {"type": "FeatureCollection", "features": features}

    with open(ARGS.output, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Written {ARGS.output}  ({len(features)} features)")
    print()
    print("Open in the geographic viewer:")
    print(f"  python -m http.server 8000")
    print(f"  then drag {ARGS.output} onto viewer.html")


if __name__ == "__main__":
    main()
