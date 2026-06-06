"""
build_kenya_dataset.py
======================
Master data pipeline that pre-builds the entire Kenya 3D Cadastre dataset.

This script is the core of the product: it downloads Microsoft AI building
footprints for every supported Kenyan city, merges them with OSM attributes,
adds derived metrics, and outputs a single GeoJSON FeatureCollection.

That GeoJSON is then converted to a PMTiles vector tile archive using tippecanoe,
uploaded to a CDN (Cloudflare R2 / S3), and served directly to the MapLibre
frontend — no backend query at runtime, just a fast static tile file.

RUN THIS ONCE (or monthly to refresh):
    python build_kenya_dataset.py
    python build_kenya_dataset.py --cities Nairobi Mombasa   # specific cities
    python build_kenya_dataset.py --radius 3000              # larger coverage

THEN CONVERT TO PMTILES (requires tippecanoe):
    tippecanoe -o kenya-buildings.pmtiles -zg --drop-densest-as-needed \\
               -l buildings kenya-buildings.geojson

THEN UPLOAD:
    # Cloudflare R2 via wrangler:
    wrangler r2 object put cadastre/kenya-buildings.pmtiles \\
              --file kenya-buildings.pmtiles
    # Or AWS S3:
    aws s3 cp kenya-buildings.pmtiles s3://your-bucket/kenya-buildings.pmtiles \\
             --content-type application/vnd.pmtiles

REQUIREMENTS:
    pip install requests lxml
    # tippecanoe: https://github.com/felt/tippecanoe (for tile generation)
"""

import argparse, json, math, os, sys, time, uuid
from pathlib import Path
from collections import defaultdict

# Add the project root to path so we can import from our scripts
sys.path.insert(0, str(Path(__file__).parent))

# kenya_counties is imported by PARSER section below

# ── CLI ──────────────────────────────────────────────────────────────────────

PARSER = argparse.ArgumentParser(description="Build the Kenya 3D Cadastre dataset")
from kenya_counties import ALL_COUNTIES, TOWN_ALIASES

PARSER.add_argument("--cities", nargs="*",
    default=list(ALL_COUNTIES.keys()),
    help="Counties to include (default: all 47). Example: --cities Nairobi Mombasa Kisumu")
PARSER.add_argument("--radius", type=int, default=2500,
    help="Coverage radius per city in metres (default 2500)")
PARSER.add_argument("--source", choices=["osm", "microsoft", "both"], default="microsoft",
    help="Data source: 'microsoft' (best shapes), 'osm' (best attributes), 'both' (merged)")
PARSER.add_argument("--output", default="kenya-buildings.geojson",
    help="Output GeoJSON filename (default: kenya-buildings.geojson)")
ARGS = PARSER.parse_args()

# ── City centres ─────────────────────────────────────────────────────────────

# Build city centres from all 47 counties
CITY_CENTRES = {
    name: {"lat": lat, "lon": lon}
    for name, (lat, lon) in ALL_COUNTIES.items()
}

# ── Derived metrics ───────────────────────────────────────────────────────────

def completeness_score(props: dict) -> int:
    """
    Rate how complete the data is for a building (1–5).
    5 = fully attributed (name, height, usage, year)
    1 = shape only (no attributes)
    This becomes a filter/sort key in the product.
    """
    score = 1
    if props.get("name"):                                     score += 1
    if props.get("heightM") and props["heightM"] > 6.4:      score += 1  # not just default
    if props.get("usage") and props["usage"] != "Unknown":    score += 1
    if props.get("yearBuilt") and props["yearBuilt"] != "Unknown": score += 1
    return score


def size_class(area_m2: float) -> str:
    """Classify building by footprint area."""
    if area_m2 > 5000:  return "Major"
    if area_m2 > 1000:  return "Large"
    if area_m2 > 300:   return "Medium"
    if area_m2 > 80:    return "Small"
    return "Micro"


def infer_usage_from_context(props: dict, city: str) -> str:
    """
    If usage is 'Unknown', try to infer from building size and city context.
    This is a simple heuristic — can be improved with ML later.
    """
    usage = props.get("usage", "Unknown")
    if usage != "Unknown":
        return usage

    area = props.get("footprintAreaM2", 0) or 0
    floors = props.get("floors", 1) or 1
    gfa = area * floors

    # Large buildings in CBD cities are almost certainly commercial
    if city in ("Nairobi", "Mombasa") and gfa > 2000:
        return "Commercial"
    # Very large single-floor buildings are likely industrial/warehouse
    if area > 3000 and floors <= 2:
        return "Industrial"
    return "Unknown"


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_city(city: str, lat: float, lon: float, radius: int, source: str) -> list[dict]:
    """
    Run the data pipeline for one city.
    Returns a list of GeoJSON feature dicts.
    """
    import tempfile
    from importMicrosoftFootprints import (
        load_tile_index, covering_quadkeys, download_tile,
        centroid, polygon_area_m2, haversine_m,
        fetch_osm_data, parse_osm_buildings,
        _floors, _height, _year, _usage,
        latlon_to_utm37s, _heuristic_floors, _adaptive_match_radius,
        _height_raw, building_to_xml,
    )
    from exportToGeoJSON import utm37s_to_latlon, to_geojson_coord, utm_poly_to_coords

    from lxml import etree as ET
    import math, uuid

    print(f"\n  [{city}] Fetching Microsoft footprints (radius {radius} m) …")

    # 1. Microsoft tile download
    lat_min = lat - math.degrees(radius / 6_371_000)
    lat_max = lat + math.degrees(radius / 6_371_000)
    lon_min = lon - math.degrees(radius / (6_371_000 * math.cos(math.radians(lat))))
    lon_max = lon + math.degrees(radius / (6_371_000 * math.cos(math.radians(lat))))

    index = load_tile_index()
    qkeys = covering_quadkeys(lat_min, lon_min, lat_max, lon_max)

    ms_features: list[dict] = []
    for qk in sorted(qkeys):
        url = index.get(qk)
        if not url:
            continue
        feats = download_tile(qk, url)
        for f in feats:
            geom = f.get("geometry", {})
            if geom.get("type") != "Polygon":
                continue
            coords = geom["coordinates"][0]
            clat, clon = centroid(coords)
            if haversine_m(lat, lon, clat, clon) <= radius:
                ms_features.append(f)

    print(f"  [{city}] {len(ms_features):,} buildings in radius")

    # 2. OSM attributes
    try:
        import requests, time as t
        from importMicrosoftFootprints import fetch_osm_data, parse_osm_buildings, _overpass_post
        osm_data   = fetch_osm_data(lat, lon, radius)
        osm_bldgs  = parse_osm_buildings(osm_data)
        print(f"  [{city}] {len(osm_bldgs)} OSM buildings for attribute matching")
    except Exception as e:
        print(f"  [{city}] OSM fetch failed ({e}), using heuristics")
        osm_bldgs = []

    # 3. Match MS → OSM
    BASE_MATCH_R = 30.0
    FALLBACK_R   = 200.0

    features: list[dict] = []

    for feat in ms_features:
        coords   = feat["geometry"]["coordinates"][0]
        ms_clat, ms_clon = centroid(coords)
        area_m2  = polygon_area_m2(coords)
        eff_r    = _adaptive_match_radius(area_m2, BASE_MATCH_R)

        best = None; best_d = float("inf")
        for ob in osm_bldgs:
            d = haversine_m(ms_clat, ms_clon, ob["centroid"][0], ob["centroid"][1])
            if d < best_d: best_d = d; best = ob

        if best and best_d <= eff_r:
            tags   = best["tags"]
            osm_id = best["id"]
            name   = tags.get("name") or tags.get("building:name")
            floors, _ = _floors(tags)
            zs     = _height(tags, floors)
            usage  = _usage(tags)
            year   = _year(tags)
        else:
            # Fallback: try buildings with height data within 200 m
            fb = None; fb_d = float("inf")
            for ob in osm_bldgs:
                t = ob["tags"]
                if not (_height_raw(t) or t.get("building:levels")):
                    continue
                d = haversine_m(ms_clat, ms_clon, ob["centroid"][0], ob["centroid"][1])
                if d < fb_d: fb_d = d; fb = ob
            if fb and fb_d <= FALLBACK_R:
                tags   = fb["tags"]
                osm_id = fb["id"]
                name   = tags.get("name") or tags.get("building:name")
                floors, _ = _floors(tags)
                zs     = _height(tags, floors)
                usage  = _usage(tags)
                year   = _year(tags)
            else:
                floors = _heuristic_floors(area_m2)
                zs = round(floors * 3.2, 1)
                usage = "Unknown"; year = None; name = None; osm_id = None

        # Build GeoJSON feature directly (skip XML round-trip)
        bid = str(uuid.uuid4())

        # Polygon in WGS84 [lon, lat]
        ring = [[round(c[0], 7), round(c[1], 7)] for c in coords]
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        # Infer usage from context when Unknown
        props_dict = {
            "buildingID":        bid,
            "city":              city,
            "name":              name,
            "floors":            floors,
            "heightM":           round(zs, 1),
            "footprintAreaM2":   round(area_m2, 1),
            "grossFloorAreaM2":  round(area_m2 * floors, 0),
            "usage":             usage,
            "yearBuilt":         str(year) if year else "Unknown",
            "osmID":             osm_id,
            "sizeClass":         size_class(area_m2),
        }
        props_dict["usage"]             = infer_usage_from_context(props_dict, city)
        props_dict["completenessScore"] = completeness_score(props_dict)

        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": props_dict,
        })

    return features


def print_stats(features: list[dict]):
    from collections import Counter
    usages = Counter(f["properties"].get("usage", "?") for f in features)
    cities = Counter(f["properties"].get("city", "?")  for f in features)
    scores = Counter(f["properties"].get("completenessScore", 0) for f in features)
    total_gfa = sum(f["properties"].get("grossFloorAreaM2", 0) or 0 for f in features)

    print("\n" + "="*60)
    print(f"  KENYA 3D CADASTRE DATASET STATISTICS")
    print("="*60)
    print(f"  Total buildings : {len(features):,}")
    print(f"  Total GFA       : {total_gfa/1_000_000:.1f} million m²")
    print()
    print("  By city:")
    for city, cnt in sorted(cities.items(), key=lambda x: -x[1]):
        print(f"    {city:<12} {cnt:>7,}")
    print()
    print("  By usage:")
    for usage, cnt in sorted(usages.items(), key=lambda x: -x[1]):
        pct = cnt / len(features) * 100
        print(f"    {usage:<14} {cnt:>7,}  ({pct:.0f}%)")
    print()
    print("  Data completeness (1=shape only, 5=fully attributed):")
    for score in range(5, 0, -1):
        cnt = scores.get(score, 0)
        pct = cnt / len(features) * 100
        bar = "█" * int(pct / 2)
        print(f"    {score}★  {cnt:>7,}  {bar}")
    print("="*60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    all_features: list[dict] = []
    cities = [c.strip().title() for c in ARGS.cities]
    radius = ARGS.radius

    print(f"\nKenya 3D Cadastre — Dataset Builder")
    print(f"Cities  : {', '.join(cities)}")
    print(f"Radius  : {radius} m per city")
    print(f"Source  : {ARGS.source}")
    print(f"Output  : {ARGS.output}")
    print()

    for city in cities:
        if city not in CITY_CENTRES:
            print(f"  Unknown city '{city}' — skipping")
            continue
        info = CITY_CENTRES.get(city) or CITY_CENTRES.get(
            next((k for k in CITY_CENTRES if k.lower() == city.lower()), ""))
        if not info:
            print(f"  Unknown county '{city}' — skipping")
            continue
        try:
            features = run_city(city, info["lat"], info["lon"], radius, ARGS.source)
            all_features.extend(features)
            print(f"  [{city}] -> {len(features):,} buildings added")
        except Exception as e:
            print(f"  [{city}] FAILED: {e}")

    # De-duplicate by building centroid (cities with overlapping radii)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in all_features:
        coords = f["geometry"]["coordinates"][0]
        if len(coords) < 4:
            continue
        lons = [c[0] for c in coords[:-1]]
        lats = [c[1] for c in coords[:-1]]
        key = (round(sum(lons)/len(lons), 5), round(sum(lats)/len(lats), 5))
        if key not in seen:
            seen.add(key)
            unique.append(f)

    print(f"\nTotal unique buildings: {len(unique):,} "
          f"({len(all_features)-len(unique):,} duplicates removed)")

    # Write GeoJSON
    geojson = {
        "type": "FeatureCollection",
        "features": unique,
        "properties": {
            "generated":    __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "cities":       cities,
            "total":        len(unique),
            "source":       ARGS.source,
        }
    }
    out = Path(ARGS.output)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, separators=(",", ":"))
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"Written {out}  ({size_mb:.1f} MB)")

    print_stats(unique)

    print(f"""
Next steps:
  1. Convert to PMTiles (install tippecanoe first):
       tippecanoe -o kenya-buildings.pmtiles -zg \\
                  --drop-densest-as-needed --extend-zooms-if-still-dropping \\
                  -l buildings {out}

  2. Upload to Cloudflare R2:
       wrangler r2 object put cadastre/kenya-buildings.pmtiles \\
                --file kenya-buildings.pmtiles --content-type application/vnd.pmtiles

  3. Set NEXT_PUBLIC_PMTILES_URL in your .env.local:
       NEXT_PUBLIC_PMTILES_URL=https://pub-XXXX.r2.dev/kenya-buildings.pmtiles

  4. Run the Next.js app — buildings will be visible across all of Kenya
     without any queries.
""")


if __name__ == "__main__":
    main()
