"""
Export 3D buildings from the XML spec to Wavefront OBJ + MTL.

Improvements in this version:
  - UV texture coordinates on all faces (tile every 2.5 m so brick/roof
    textures tile naturally at any scale)
  - Per-building colour variation (30 wall × roof combos from a curated palette)
  - Road network as individual crossing strips with sidewalks and lane markings
  - Grass ground plane beneath the city
  - Supports taller buildings (up to 20 floors via randomiseCity.py)

Usage:
    python exportToOBJ.py -i city.xml -o city.obj
    python exportToOBJ.py -i city.xml -o city.obj --lod 3    # doors + windows
    python exportToOBJ.py -i city.xml -o buildings/ --split  # one .obj per building
"""

import argparse, math, os
from lxml import etree

# ── CLI ──────────────────────────────────────────────────────────────────────

PARSER = argparse.ArgumentParser(description="Export buildings XML → OBJ")
PARSER.add_argument("-i", "--input",  required=True)
PARSER.add_argument("-o", "--output", required=True)
PARSER.add_argument("--lod", type=int, default=2, choices=[0, 1, 2, 3],
                    help="0=footprint 1=block 2=roof(default) 3=openings")
PARSER.add_argument("--split", action="store_true",
                    help="Write one .obj per building")
ARGS = PARSER.parse_args()

# ── Colour palettes ──────────────────────────────────────────────────────────

UV_TILE = 2.5   # metres per UV unit

WALL_PALETTE = [
    (0.93, 0.89, 0.82),  # warm cream
    (0.82, 0.82, 0.80),  # cool grey
    (0.80, 0.72, 0.58),  # tan
    (0.88, 0.82, 0.68),  # light ochre
    (0.95, 0.93, 0.89),  # off-white
    (0.70, 0.72, 0.75),  # slate grey
]
ROOF_PALETTE = [
    (0.62, 0.22, 0.15),  # terracotta
    (0.30, 0.32, 0.35),  # dark slate
    (0.28, 0.48, 0.28),  # forest green
    (0.55, 0.28, 0.12),  # rust brown
    (0.20, 0.20, 0.22),  # charcoal
]

def building_mats(bid):
    """Return (wall_mat_name, roof_mat_name) for a building UUID."""
    h = sum(ord(c) * (i + 1) for i, c in enumerate(bid[:16]))
    return f"Wall_{h % len(WALL_PALETTE)}", f"Roof_{(h // 7) % len(ROOF_PALETTE)}"

def build_material_table():
    mats = {}
    for i, c in enumerate(WALL_PALETTE):
        mats[f"Wall_{i}"] = {"Kd": c, "Ka": (0.10,0.10,0.10), "Ks": (0.05,0.05,0.05), "Ns": 10}
    for i, c in enumerate(ROOF_PALETTE):
        mats[f"Roof_{i}"] = {"Kd": c, "Ka": (0.10,0.10,0.10), "Ks": (0.08,0.08,0.08), "Ns": 18}
    mats["Floor"]    = {"Kd": (0.55, 0.53, 0.50)}
    mats["Door"]     = {"Kd": (0.35, 0.22, 0.12), "Ks": (0.10,0.08,0.05), "Ns": 25}
    mats["Window"]   = {"Kd": (0.55, 0.75, 0.90), "d": 0.55, "Ks": (0.30,0.35,0.40), "Ns": 60}
    mats["Road"]     = {"Kd": (0.38, 0.38, 0.40)}
    mats["Sidewalk"] = {"Kd": (0.72, 0.70, 0.67)}
    mats["Marking"]  = {"Kd": (0.92, 0.92, 0.88)}
    mats["Ground"]   = {"Kd": (0.28, 0.45, 0.22)}
    mats["Park"]     = {"Kd": (0.28, 0.62, 0.26)}
    return mats

def write_mtl(path, mats):
    with open(path, "w") as f:
        for name, props in mats.items():
            f.write(f"newmtl {name}\n")
            r, g, b = props.get("Kd", (0.8, 0.8, 0.8))
            f.write(f"Kd {r:.3f} {g:.3f} {b:.3f}\n")
            ka = props.get("Ka", (0.10, 0.10, 0.10))
            f.write(f"Ka {ka[0]:.3f} {ka[1]:.3f} {ka[2]:.3f}\n")
            ks = props.get("Ks", (0.02, 0.02, 0.02))
            f.write(f"Ks {ks[0]:.3f} {ks[1]:.3f} {ks[2]:.3f}\n")
            f.write(f"Ns {props.get('Ns', 10):.1f}\n")
            if "d" in props:
                f.write(f"d {props['d']:.2f}\n")
            f.write("\n")

# ── OBJ builder ──────────────────────────────────────────────────────────────

class OBJBuilder:
    def __init__(self):
        self.verts = []   # (x, y, z)
        self.uvs   = []   # (u, v)
        self.faces = []   # (mat, [(v_idx, uv_idx), ...], group)
        self._grp  = "default"

    def group(self, name):
        self._grp = name

    def poly(self, pts, uvcoords, mat):
        vb  = len(self.verts)  + 1
        uvb = len(self.uvs)    + 1
        self.verts.extend(pts)
        self.uvs.extend(uvcoords)
        pairs = [(vb + i, uvb + i) for i in range(len(pts))]
        self.faces.append((mat, pairs, self._grp))

    def write(self, obj_path, mtl_name):
        with open(obj_path, "w") as f:
            f.write(f"# 3D Buildings Generator\nmtllib {mtl_name}\n\n")
            for x, y, z in self.verts:
                f.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
            f.write("\n")
            for u, v in self.uvs:
                f.write(f"vt {u:.4f} {v:.4f}\n")
            f.write("\n")
            cur_mat, cur_grp = None, None
            for mat, pairs, grp in self.faces:
                if grp != cur_grp:
                    f.write(f"g {grp}\n")
                    cur_grp = grp
                if mat != cur_mat:
                    f.write(f"usemtl {mat}\n")
                    cur_mat = mat
                f.write("f " + " ".join(f"{v}/{u}" for v, u in pairs) + "\n")

# ── UV helpers ───────────────────────────────────────────────────────────────

def _d(a, b):
    return math.sqrt(sum((a[i]-b[i])**2 for i in range(3)))

def wall_uv(length, height):
    u1, v1 = length / UV_TILE, height / UV_TILE
    return [(0, 0), (u1, 0), (u1, v1), (0, v1)]

def flat_uv(xs, ys):
    return [(0, 0), (xs/UV_TILE, 0), (xs/UV_TILE, ys/UV_TILE), (0, ys/UV_TILE)]

def tri_uv():
    return [(0, 0), (1, 0), (0.5, 1)]

def edge_uv(pts):
    """Planar UV for a quad based on actual edge lengths."""
    u1 = _d(pts[0], pts[1]) / UV_TILE
    v1 = _d(pts[0], pts[-1]) / UV_TILE
    return [(0, 0), (u1, 0), (u1, v1), (0, v1)]

# ── Building geometry ─────────────────────────────────────────────────────────

def add_box(builder, ox, oy, oz, xs, ys, zs, wall_mat, floor_mat):
    """Box with UV coords on every face."""
    builder.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox,oy+ys,oz)],
                 flat_uv(xs, ys), floor_mat)
    # south (y=oy, runs +x)
    builder.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy,oz+zs),(ox,oy,oz+zs)],
                 wall_uv(xs, zs), wall_mat)
    # north (y=oy+ys, runs -x)
    builder.poly([(ox+xs,oy+ys,oz),(ox,oy+ys,oz),(ox,oy+ys,oz+zs),(ox+xs,oy+ys,oz+zs)],
                 wall_uv(xs, zs), wall_mat)
    # west (x=ox, runs -y)
    builder.poly([(ox,oy+ys,oz),(ox,oy,oz),(ox,oy,oz+zs),(ox,oy+ys,oz+zs)],
                 wall_uv(ys, zs), wall_mat)
    # east (x=ox+xs, runs +y)
    builder.poly([(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox+xs,oy+ys,oz+zs),(ox+xs,oy,oz+zs)],
                 wall_uv(ys, zs), wall_mat)

def add_roof(builder, ox, oy, zt, xs, ys, h, r, rtype, roof_mat):
    """Shaped roof at height zt, ridge height h, hip-width r."""
    if rtype == "Flat" or h == 0:
        builder.poly([(ox,oy,zt),(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(ox,oy+ys,zt)],
                     flat_uv(xs, ys), roof_mat)

    elif rtype == "Shed":
        pts = [(ox,oy,zt+h),(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(ox,oy+ys,zt+h)]
        builder.poly(pts, edge_uv(pts), roof_mat)
        builder.poly([(ox,oy,zt),(ox,oy,zt+h),(ox,oy+ys,zt+h),(ox,oy+ys,zt)],
                     wall_uv(ys, h), roof_mat)
        builder.poly([(ox+xs,oy+ys,zt),(ox+xs,oy,zt),(ox+xs,oy,zt),(ox+xs,oy+ys,zt)],
                     wall_uv(ys, 0.01), roof_mat)

    elif rtype == "Gabled":
        rx = ox + xs * 0.5
        # slopes
        pts_e = [(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(rx,oy,zt+h)]
        pts_w = [(ox,oy+ys,zt),(ox,oy,zt),(rx,oy,zt+h),(rx,oy+ys,zt+h)]
        builder.poly(pts_e, edge_uv(pts_e), roof_mat)
        builder.poly(pts_w, edge_uv(pts_w), roof_mat)
        # gable triangles
        builder.poly([(ox,oy,zt),(rx,oy,zt+h),(ox+xs,oy,zt)], tri_uv(), roof_mat)
        builder.poly([(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(ox,oy+ys,zt)], tri_uv(), roof_mat)

    elif rtype == "Hipped":
        r = min(r, ys * 0.49)
        rx = ox + xs * 0.5
        ry0, ry1 = oy + r, oy + ys - r
        # hip triangles
        builder.poly([(ox,oy,zt),(ox+xs,oy,zt),(rx,ry0,zt+h)], tri_uv(), roof_mat)
        builder.poly([(ox+xs,oy+ys,zt),(ox,oy+ys,zt),(rx,ry1,zt+h)], tri_uv(), roof_mat)
        # slopes
        pts_e = [(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(rx,ry1,zt+h),(rx,ry0,zt+h)]
        pts_w = [(ox,oy+ys,zt),(ox,oy,zt),(rx,ry0,zt+h),(rx,ry1,zt+h)]
        builder.poly(pts_e, edge_uv(pts_e), roof_mat)
        builder.poly(pts_w, edge_uv(pts_w), roof_mat)

    elif rtype == "Pyramidal":
        apex = (ox + xs*0.5, oy + ys*0.5, zt + h)
        builder.poly([(ox,oy,zt),(ox+xs,oy,zt),apex], tri_uv(), roof_mat)
        builder.poly([(ox+xs,oy,zt),(ox+xs,oy+ys,zt),apex], tri_uv(), roof_mat)
        builder.poly([(ox+xs,oy+ys,zt),(ox,oy+ys,zt),apex], tri_uv(), roof_mat)
        builder.poly([(ox,oy+ys,zt),(ox,oy,zt),apex], tri_uv(), roof_mat)

def _opening_pts(ox, oy, oz, xs, ys, wall, wx, wy, ww, wh):
    z0, z1 = oz + wy, oz + wy + wh
    if wall == 0:
        x0, x1 = ox+wx, ox+wx+ww
        return [(x0,oy,z0),(x1,oy,z0),(x1,oy,z1),(x0,oy,z1)]
    elif wall == 1:
        y0, y1 = oy+wx, oy+wx+ww
        return [(ox+xs,y0,z0),(ox+xs,y1,z0),(ox+xs,y1,z1),(ox+xs,y0,z1)]
    elif wall == 2:
        x0, x1 = ox+xs-wx-ww, ox+xs-wx
        return [(x1,oy+ys,z0),(x0,oy+ys,z0),(x0,oy+ys,z1),(x1,oy+ys,z1)]
    else:
        y0, y1 = oy+ys-wx-ww, oy+ys-wx
        return [(ox,y1,z0),(ox,y0,z0),(ox,y0,z1),(ox,y1,z1)]

def export_building(b, builder, lod, offset=(0.0, 0.0)):
    bid      = b.attrib.get("ID", "bldg")
    wall_mat, roof_mat = building_mats(bid)
    builder.group(f"b_{bid[:8]}")

    ox, oy, oz = [float(v) for v in b.findtext("origin").split()]
    ox -= offset[0]
    oy -= offset[1]
    xs = float(b.findtext("xSize"))
    ys = float(b.findtext("ySize"))
    zs = float(b.findtext("zSize"))

    roof_el = b.find("roof")
    rtype   = roof_el.findtext("roofType") if roof_el is not None else "Flat"
    h_el    = roof_el.find("h") if roof_el is not None else None
    r_el    = roof_el.find("r") if roof_el is not None else None
    h = float(h_el.text) if h_el is not None else 0.0
    r = float(r_el.text) if r_el is not None else ys * 0.5

    if lod == 0:
        builder.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox,oy+ys,oz)],
                     flat_uv(xs, ys), "Floor")
        return

    add_box(builder, ox, oy, oz, xs, ys, zs, wall_mat, "Floor")

    if lod == 1:
        builder.poly([(ox,oy,oz+zs),(ox,oy+ys,oz+zs),(ox+xs,oy+ys,oz+zs),(ox+xs,oy,oz+zs)],
                     flat_uv(xs, ys), roof_mat)
        return

    add_roof(builder, ox, oy, oz + zs, xs, ys, h, r, rtype, roof_mat)

    if lod < 3:
        return

    door_el = b.find("door")
    if door_el is not None:
        wall = int(door_el.findtext("wall"))
        dox  = float(door_el.find("origin/x").text)
        doy  = float(door_el.find("origin/y").text)
        dw   = float(door_el.find("size/width").text)
        dh   = float(door_el.find("size/height").text)
        pts  = _opening_pts(ox, oy, oz, xs, ys, wall, dox, doy, dw, dh)
        builder.poly(pts, wall_uv(dw, dh), "Door")

    wins_el = b.find("windows")
    if wins_el is not None:
        for w in wins_el.findall("window"):
            wall = int(w.findtext("wall"))
            wox  = float(w.find("origin/x").text)
            woy  = float(w.find("origin/y").text)
            ww   = float(w.find("size/width").text)
            wh   = float(w.find("size/height").text)
            pts  = _opening_pts(ox, oy, oz, xs, ys, wall, wox, woy, ww, wh)
            builder.poly(pts, wall_uv(ww, wh), "Window")

    bp = b.find("buildingPart")
    if bp is not None:
        p_orig = float(bp.findtext("partOrigin"))
        pw = float(bp.findtext("width"))
        pl = float(bp.findtext("length"))
        ph = float(bp.findtext("height"))
        pox, poy, poz = ox + xs, oy + p_orig, oz
        add_box(builder, pox, poy, poz, pw, pl, ph, wall_mat, "Floor")
        builder.poly([(pox,poy,poz+ph),(pox,poy+pl,poz+ph),(pox+pw,poy+pl,poz+ph),(pox+pw,poy,poz+ph)],
                     flat_uv(pw, pl), roof_mat)

# ── Road network ─────────────────────────────────────────────────────────────
# Geometry constants matching randomiseCity.py defaults.
CELLSIZE = 20.0
SKIP     = 2          # roads every SKIP cells (matches streetgenerator skipx/skipy=2)
ROAD_W   = 5.0
SEP      = 1.0        # gap between road edge and building block

def _vstrip(builder, x0, x1, y0, y1, mat):
    w, l = x1-x0, y1-y0
    builder.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)], wall_uv(w, l), mat)

def _hstrip(builder, y0, y1, x0, x1, mat):
    l, w = x1-x0, y1-y0
    builder.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)], wall_uv(l, w), mat)

def generate_road_network(builder, max_col, max_row):
    # City bounding box (including outer road strips)
    cx0 = -(SEP + ROAD_W)
    cx1 = (max_col + 1) * CELLSIZE + ROAD_W
    cy0 = -(SEP + ROAD_W)
    cy1 = (max_row + 1) * CELLSIZE + ROAD_W
    margin = 10.0

    # Grass ground base
    builder.group("ground")
    gx0, gx1 = cx0 - margin, cx1 + margin
    gy0, gy1 = cy0 - margin, cy1 + margin
    builder.poly([(gx0,gy0,-0.05),(gx1,gy0,-0.05),(gx1,gy1,-0.05),(gx0,gy1,-0.05)],
                 flat_uv(gx1-gx0, gy1-gy0), "Ground")

    # Collect road strip x-positions (vertical roads)
    road_xs = []
    road_xs.append((cx0, cx0 + ROAD_W))                          # left edge
    for k in range(max_col // SKIP + 1):                          # internal
        x0 = (k + 1) * SKIP * CELLSIZE - SEP - ROAD_W
        x1 = x0 + ROAD_W
        if cx0 < x0 < cx1:
            road_xs.append((x0, x1))
    road_xs.append((cx1 - ROAD_W, cx1))                          # right edge

    # Collect road strip y-positions (horizontal roads)
    road_ys = []
    road_ys.append((cy0, cy0 + ROAD_W))
    for k in range(max_row // SKIP + 1):
        y0 = (k + 1) * SKIP * CELLSIZE - SEP - ROAD_W
        y1 = y0 + ROAD_W
        if cy0 < y0 < cy1:
            road_ys.append((y0, y1))
    road_ys.append((cy1 - ROAD_W, cy1))

    # Road asphalt strips
    builder.group("roads")
    for x0, x1 in road_xs:
        _vstrip(builder, x0, x1, cy0, cy1, "Road")
    for y0, y1 in road_ys:
        _hstrip(builder, y0, y1, cx0, cx1, "Road")

    # Sidewalks (1.5 m wide, on both sides of every road strip)
    builder.group("sidewalks")
    sw = 1.5
    for x0, x1 in road_xs:
        if x0 - sw > cx0:
            _vstrip(builder, x0 - sw, x0, cy0, cy1, "Sidewalk")
        if x1 + sw < cx1:
            _vstrip(builder, x1, x1 + sw, cy0, cy1, "Sidewalk")
    for y0, y1 in road_ys:
        if y0 - sw > cy0:
            _hstrip(builder, y0 - sw, y0, cx0, cx1, "Sidewalk")
        if y1 + sw < cy1:
            _hstrip(builder, y1, y1 + sw, cx0, cx1, "Sidewalk")

    # Dashed centre-line lane markings
    builder.group("lane_markings")
    mw  = 0.12   # marking width
    ml  = 3.0    # dash length
    mg  = 3.0    # gap between dashes
    for x0, x1 in road_xs:
        cx = (x0 + x1) / 2
        y  = cy0 + 1.0
        while y + ml < cy1:
            _vstrip(builder, cx - mw/2, cx + mw/2, y, y + ml, "Marking")
            y += ml + mg
    for y0, y1 in road_ys:
        cy = (y0 + y1) / 2
        x  = cx0 + 1.0
        while x + ml < cx1:
            _hstrip(builder, cy - mw/2, cy + mw/2, x, x + ml, "Marking")
            x += ml + mg

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    lod = ARGS.lod
    print(f"Parsing {ARGS.input} ...")
    root = etree.parse(ARGS.input).getroot()
    buildings = root.findall("building")
    print(f"Found {len(buildings)} building(s). Exporting at LOD {lod} ...")

    mats = build_material_table()

    # ── Scene analysis ────────────────────────────────────────────────────────
    # 1. Compute origin offset so every viewer sees the city near (0,0,0).
    #    UTM coordinates (e.g. 257 000, 9 855 000) would place the scene
    #    hundreds of km from the camera — this is the #1 cause of blank viewports.
    all_ox = [float(b.findtext("origin").split()[0]) for b in buildings]
    all_oy = [float(b.findtext("origin").split()[1]) for b in buildings]
    offset = (min(all_ox), min(all_oy))
    if offset != (0.0, 0.0):
        print(f"Normalising coordinates: subtracting offset ({offset[0]:.1f}, {offset[1]:.1f})")

    # 2. Detect whether buildings come from randomiseCity.py (regular grid,
    #    varied order values) or from osmToXML.py (arbitrary positions, all
    #    order="0 0").  Grid data gets the full road-strip network; OSM data
    #    gets a simple ground plane so roads don't land in the wrong place.
    orders = set()
    max_col, max_row = 0, 0
    for b in buildings:
        order = b.findtext("order")
        if order:
            orders.add(order.strip())
            col, row = int(order.split()[0]), int(order.split()[1])
            max_col = max(max_col, col)
            max_row = max(max_row, row)
    is_grid = len(orders) > 1

    # City bounding box in local (normalised) coordinates
    all_xs = [float(b.findtext("xSize")) for b in buildings]
    all_ys = [float(b.findtext("ySize")) for b in buildings]
    city_w = max((ox - offset[0]) + xs for ox, xs in zip(all_ox, all_xs))
    city_h = max((oy - offset[1]) + ys for oy, ys in zip(all_oy, all_ys))

    if ARGS.split:
        out_dir = ARGS.output
        os.makedirs(out_dir, exist_ok=True)
        mtl_name = "city.mtl"
        write_mtl(os.path.join(out_dir, mtl_name), mats)
        for i, b in enumerate(buildings):
            bld = OBJBuilder()
            export_building(b, bld, lod, offset)
            bid   = b.attrib.get("ID", f"b{i:04d}")
            fname = os.path.join(out_dir, f"{bid[:8]}_{i:04d}.obj")
            bld.write(fname, mtl_name)
        print(f"Written {len(buildings)} .obj files to {out_dir}/")
    else:
        out_path = ARGS.output
        mtl_stem = os.path.splitext(os.path.basename(out_path))[0]
        mtl_name = mtl_stem + ".mtl"
        mtl_path = os.path.join(os.path.dirname(out_path) or ".", mtl_name)
        write_mtl(mtl_path, mats)

        builder = OBJBuilder()

        if is_grid:
            generate_road_network(builder, max_col, max_row)
        else:
            # OSM data: just a ground plane sized to the actual city footprint
            margin = 20.0
            builder.group("ground")
            builder.poly([(-margin, -margin, -0.05),
                          (city_w+margin, -margin, -0.05),
                          (city_w+margin, city_h+margin, -0.05),
                          (-margin, city_h+margin, -0.05)],
                         flat_uv(city_w+2*margin, city_h+2*margin), "Ground")

        for b in buildings:
            export_building(b, builder, lod, offset)

        # ── Roads from OSM (centerline → buffered quads) ──────────────────────
        roads_el = root.find("roads")
        if roads_el is not None:
            builder.group("osm_roads")
            for rd in roads_el.findall("road"):
                nodes_txt = rd.findtext("nodes")
                if not nodes_txt:
                    continue
                hw = float(rd.attrib.get("width", 5.0))
                vals = [float(v) for v in nodes_txt.split()]
                pts = [(vals[i] - offset[0], vals[i+1] - offset[1])
                       for i in range(0, len(vals)-1, 2)]
                hh = hw / 2.0
                for i in range(len(pts) - 1):
                    p1, p2 = pts[i], pts[i+1]
                    dx, dy = p2[0]-p1[0], p2[1]-p1[1]
                    seg_len = math.sqrt(dx*dx + dy*dy)
                    if seg_len < 0.1:
                        continue
                    nx, ny = -dy/seg_len * hh, dx/seg_len * hh
                    quad = [(p1[0]-nx, p1[1]-ny, 0.01),
                            (p1[0]+nx, p1[1]+ny, 0.01),
                            (p2[0]+nx, p2[1]+ny, 0.01),
                            (p2[0]-nx, p2[1]-ny, 0.01)]
                    builder.poly(quad, flat_uv(hw, seg_len), "Road")

        # ── Parks from OSM (real polygon) ─────────────────────────────────────
        parks_el = root.find("parks")
        if parks_el is not None:
            builder.group("parks")
            for park in parks_el.findall("park"):
                # New format: <polygon> from osmToXML.py
                poly_txt = park.findtext("polygon")
                if poly_txt:
                    vals = [float(v) for v in poly_txt.split()]
                    pts = [(vals[i]-offset[0], vals[i+1]-offset[1], 0.02)
                           for i in range(0, len(vals)-1, 2)]
                    if len(pts) >= 3:
                        # Planar UV per vertex
                        min_x = min(p[0] for p in pts)
                        min_y = min(p[1] for p in pts)
                        uvs = [((p[0]-min_x)/UV_TILE, (p[1]-min_y)/UV_TILE) for p in pts]
                        builder.poly(pts, uvs, "Park")
                else:
                    # Legacy format: <outline> from randomiseCity.py
                    txt = park.findtext("outline")
                    if txt:
                        coords = [float(v) for v in txt.split()]
                        px0 = coords[0]-offset[0]; py0 = coords[1]-offset[1]
                        px1 = coords[2]-offset[0]; py1 = coords[3]-offset[1]
                        builder.poly([(px0,py0,0.02),(px1,py0,0.02),
                                      (px1,py1,0.02),(px0,py1,0.02)],
                                     flat_uv(abs(px1-px0), abs(py1-py0)), "Park")

        builder.write(out_path, mtl_name)
        print(f"Written {out_path}  ({len(builder.verts):,} vertices, {len(builder.faces):,} faces)")
        print(f"Material library: {mtl_path}")
        print()
        print("Import into your engine:")
        print("  Blender  : File > Import > Wavefront (.obj)")
        print("  Unity    : drag the .obj and .mtl into your Assets folder")
        print("  Unreal   : File > Import into Level  (select the .obj)")

if __name__ == "__main__":
    main()
