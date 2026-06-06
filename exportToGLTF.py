"""
Export 3D buildings from the XML spec to glTF 2.0 (.glb).

A single self-contained binary file importable in Unity, Unreal Engine,
Godot, Three.js, Babylon.js and any other glTF-compatible tool.

Advantages over OBJ:
  - Single .glb file (no separate .mtl)
  - PBR materials (metallic/roughness workflow)
  - Per-building colour variation via baseColorFactor
  - UV texture coordinates (TEXCOORD_0) ready for tiling textures
  - Compact binary encoding

Usage:
    python exportToGLTF.py -i city.xml -o city.glb
    python exportToGLTF.py -i city.xml -o city.glb --lod 3
    python exportToGLTF.py -i city.xml -o city.glb --split   # one .glb per building

Requirements:
    pip install pygltflib numpy lxml
"""

import argparse, math, os, struct, sys
from collections import defaultdict

try:
    import numpy as np
except ImportError:
    print("numpy is required: pip install numpy"); sys.exit(1)

try:
    from pygltflib import (
        GLTF2, Scene, Node, Mesh, Primitive, Accessor, BufferView, Buffer,
        Material, PbrMetallicRoughness, Asset,
        FLOAT, UNSIGNED_INT, SCALAR, VEC2, VEC3,
        ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER
    )
except ImportError:
    print("pygltflib is required: pip install pygltflib"); sys.exit(1)

from lxml import etree

# ── CLI ──────────────────────────────────────────────────────────────────────

PARSER = argparse.ArgumentParser(description="Export buildings XML → glTF (.glb)")
PARSER.add_argument("-i", "--input",  required=True)
PARSER.add_argument("-o", "--output", required=True)
PARSER.add_argument("--lod", type=int, default=2, choices=[0,1,2,3],
                    help="0=footprint 1=block 2=roof(default) 3=openings")
PARSER.add_argument("--split", action="store_true",
                    help="Write one .glb per building")
ARGS = PARSER.parse_args()

# ── Colour palettes ──────────────────────────────────────────────────────────

UV_TILE = 2.5

WALL_PALETTE = [
    (0.93, 0.89, 0.82),
    (0.82, 0.82, 0.80),
    (0.80, 0.72, 0.58),
    (0.88, 0.82, 0.68),
    (0.95, 0.93, 0.89),
    (0.70, 0.72, 0.75),
]
ROOF_PALETTE = [
    (0.62, 0.22, 0.15),
    (0.30, 0.32, 0.35),
    (0.28, 0.48, 0.28),
    (0.55, 0.28, 0.12),
    (0.20, 0.20, 0.22),
]

# Fixed material index map
_FIXED_MATS = {
    "Floor":   (0.55, 0.53, 0.50),
    "Door":    (0.35, 0.22, 0.12),
    "Window":  (0.55, 0.75, 0.90),
    "Road":    (0.38, 0.38, 0.40),
    "Sidewalk":(0.72, 0.70, 0.67),
    "Marking": (0.92, 0.92, 0.88),
    "Ground":  (0.28, 0.45, 0.22),
    "Park":    (0.28, 0.62, 0.26),
}

def _mat_color(name):
    """Return (r,g,b) for any material name."""
    if name in _FIXED_MATS:
        return _FIXED_MATS[name]
    if name.startswith("Wall_"):
        return WALL_PALETTE[int(name.split("_")[1]) % len(WALL_PALETTE)]
    if name.startswith("Roof_"):
        return ROOF_PALETTE[int(name.split("_")[1]) % len(ROOF_PALETTE)]
    return (0.8, 0.8, 0.8)

def _mat_roughness(name):
    if name == "Window": return 0.05
    if name.startswith("Roof"): return 0.70
    return 0.85

def _mat_metallic(name):
    if name == "Window": return 0.0
    return 0.0

def building_mats(bid):
    h = sum(ord(c) * (i + 1) for i, c in enumerate(bid[:16]))
    return f"Wall_{h % len(WALL_PALETTE)}", f"Roof_{(h // 7) % len(ROOF_PALETTE)}"

# ── Geometry accumulator ──────────────────────────────────────────────────────

class GLTFGeom:
    """Accumulates triangle geometry per material, ready to pack into glTF."""
    def __init__(self):
        self.pos = defaultdict(list)   # mat → list of [x,y,z]
        self.uvs = defaultdict(list)   # mat → list of [u,v]
        self.idx = defaultdict(list)   # mat → list of int (triangle indices)

    def poly(self, pts, uvcoords, mat):
        """Fan-triangulate a convex polygon and accumulate."""
        base = len(self.pos[mat])
        self.pos[mat].extend(list(p) for p in pts)
        if uvcoords:
            self.uvs[mat].extend(list(u) for u in uvcoords)
        else:
            self.uvs[mat].extend([[0.0, 0.0]] * len(pts))
        for i in range(1, len(pts) - 1):
            self.idx[mat].extend([base, base + i, base + i + 1])

    def materials(self):
        return list(self.pos.keys())

# ── UV helpers ───────────────────────────────────────────────────────────────

def _d(a, b):
    return math.sqrt(sum((a[i]-b[i])**2 for i in range(3)))

def wall_uv(length, height):
    u1, v1 = length / UV_TILE, height / UV_TILE
    return [(0,0),(u1,0),(u1,v1),(0,v1)]

def flat_uv(xs, ys):
    return [(0,0),(xs/UV_TILE,0),(xs/UV_TILE,ys/UV_TILE),(0,ys/UV_TILE)]

def tri_uv():
    return [(0,0),(1,0),(0.5,1)]

def edge_uv(pts):
    u1 = _d(pts[0], pts[1]) / UV_TILE
    v1 = _d(pts[0], pts[-1]) / UV_TILE
    return [(0,0),(u1,0),(u1,v1),(0,v1)]

# ── Building geometry (shared with exportToOBJ) ───────────────────────────────

def add_box(geom, ox, oy, oz, xs, ys, zs, wall_mat, floor_mat):
    geom.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox,oy+ys,oz)],
              flat_uv(xs,ys), floor_mat)
    geom.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy,oz+zs),(ox,oy,oz+zs)],
              wall_uv(xs,zs), wall_mat)
    geom.poly([(ox+xs,oy+ys,oz),(ox,oy+ys,oz),(ox,oy+ys,oz+zs),(ox+xs,oy+ys,oz+zs)],
              wall_uv(xs,zs), wall_mat)
    geom.poly([(ox,oy+ys,oz),(ox,oy,oz),(ox,oy,oz+zs),(ox,oy+ys,oz+zs)],
              wall_uv(ys,zs), wall_mat)
    geom.poly([(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox+xs,oy+ys,oz+zs),(ox+xs,oy,oz+zs)],
              wall_uv(ys,zs), wall_mat)

def add_roof(geom, ox, oy, zt, xs, ys, h, r, rtype, roof_mat):
    if rtype == "Flat" or h == 0:
        geom.poly([(ox,oy,zt),(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(ox,oy+ys,zt)],
                  flat_uv(xs,ys), roof_mat)
    elif rtype == "Shed":
        pts = [(ox,oy,zt+h),(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(ox,oy+ys,zt+h)]
        geom.poly(pts, edge_uv(pts), roof_mat)
        geom.poly([(ox,oy,zt),(ox,oy,zt+h),(ox,oy+ys,zt+h),(ox,oy+ys,zt)],
                  wall_uv(ys,h), roof_mat)
    elif rtype == "Gabled":
        rx = ox + xs * 0.5
        pts_e = [(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(rx,oy,zt+h)]
        pts_w = [(ox,oy+ys,zt),(ox,oy,zt),(rx,oy,zt+h),(rx,oy+ys,zt+h)]
        geom.poly(pts_e, edge_uv(pts_e), roof_mat)
        geom.poly(pts_w, edge_uv(pts_w), roof_mat)
        geom.poly([(ox,oy,zt),(rx,oy,zt+h),(ox+xs,oy,zt)], tri_uv(), roof_mat)
        geom.poly([(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(ox,oy+ys,zt)], tri_uv(), roof_mat)
    elif rtype == "Hipped":
        r = min(r, ys * 0.49)
        rx, ry0, ry1 = ox+xs*0.5, oy+r, oy+ys-r
        geom.poly([(ox,oy,zt),(ox+xs,oy,zt),(rx,ry0,zt+h)], tri_uv(), roof_mat)
        geom.poly([(ox+xs,oy+ys,zt),(ox,oy+ys,zt),(rx,ry1,zt+h)], tri_uv(), roof_mat)
        pts_e = [(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(rx,ry1,zt+h),(rx,ry0,zt+h)]
        pts_w = [(ox,oy+ys,zt),(ox,oy,zt),(rx,ry0,zt+h),(rx,ry1,zt+h)]
        geom.poly(pts_e, edge_uv(pts_e), roof_mat)
        geom.poly(pts_w, edge_uv(pts_w), roof_mat)
    elif rtype == "Pyramidal":
        apex = (ox+xs*0.5, oy+ys*0.5, zt+h)
        geom.poly([(ox,oy,zt),(ox+xs,oy,zt),apex], tri_uv(), roof_mat)
        geom.poly([(ox+xs,oy,zt),(ox+xs,oy+ys,zt),apex], tri_uv(), roof_mat)
        geom.poly([(ox+xs,oy+ys,zt),(ox,oy+ys,zt),apex], tri_uv(), roof_mat)
        geom.poly([(ox,oy+ys,zt),(ox,oy,zt),apex], tri_uv(), roof_mat)

def _opening_pts(ox, oy, oz, xs, ys, wall, wx, wy, ww, wh):
    z0, z1 = oz+wy, oz+wy+wh
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

def export_building(b, geom, lod, offset=(0.0, 0.0)):
    bid = b.attrib.get("ID", "bldg")
    wall_mat, roof_mat = building_mats(bid)

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
        geom.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox,oy+ys,oz)],
                  flat_uv(xs,ys), "Floor")
        return

    add_box(geom, ox, oy, oz, xs, ys, zs, wall_mat, "Floor")

    if lod == 1:
        geom.poly([(ox,oy,oz+zs),(ox,oy+ys,oz+zs),(ox+xs,oy+ys,oz+zs),(ox+xs,oy,oz+zs)],
                  flat_uv(xs,ys), roof_mat)
        return

    add_roof(geom, ox, oy, oz+zs, xs, ys, h, r, rtype, roof_mat)

    if lod < 3:
        return

    door_el = b.find("door")
    if door_el is not None:
        wall = int(door_el.findtext("wall"))
        dox  = float(door_el.find("origin/x").text)
        doy  = float(door_el.find("origin/y").text)
        dw   = float(door_el.find("size/width").text)
        dh   = float(door_el.find("size/height").text)
        geom.poly(_opening_pts(ox,oy,oz,xs,ys,wall,dox,doy,dw,dh), wall_uv(dw,dh), "Door")

    wins_el = b.find("windows")
    if wins_el is not None:
        for w in wins_el.findall("window"):
            wall = int(w.findtext("wall"))
            wox  = float(w.find("origin/x").text)
            woy  = float(w.find("origin/y").text)
            ww   = float(w.find("size/width").text)
            wh   = float(w.find("size/height").text)
            geom.poly(_opening_pts(ox,oy,oz,xs,ys,wall,wox,woy,ww,wh), wall_uv(ww,wh), "Window")

    bp = b.find("buildingPart")
    if bp is not None:
        p_orig = float(bp.findtext("partOrigin"))
        pw = float(bp.findtext("width"))
        pl = float(bp.findtext("length"))
        ph = float(bp.findtext("height"))
        pox, poy, poz = ox+xs, oy+p_orig, oz
        add_box(geom, pox, poy, poz, pw, pl, ph, wall_mat, "Floor")
        geom.poly([(pox,poy,poz+ph),(pox,poy+pl,poz+ph),(pox+pw,poy+pl,poz+ph),(pox+pw,poy,poz+ph)],
                  flat_uv(pw,pl), roof_mat)

# ── Road network ──────────────────────────────────────────────────────────────

CELLSIZE = 20.0
SKIP, ROAD_W, SEP = 2, 5.0, 1.0

def _vstrip(geom, x0, x1, y0, y1, mat):
    w, l = x1-x0, y1-y0
    geom.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)], wall_uv(w,l), mat)

def _hstrip(geom, y0, y1, x0, x1, mat):
    l, w = x1-x0, y1-y0
    geom.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)], wall_uv(l,w), mat)

def generate_road_network(geom, max_col, max_row):
    cx0 = -(SEP + ROAD_W);  cx1 = (max_col+1)*CELLSIZE + ROAD_W
    cy0 = -(SEP + ROAD_W);  cy1 = (max_row+1)*CELLSIZE + ROAD_W
    mg  = 10.0

    geom.poly([(cx0-mg,cy0-mg,-0.05),(cx1+mg,cy0-mg,-0.05),
               (cx1+mg,cy1+mg,-0.05),(cx0-mg,cy1+mg,-0.05)],
              flat_uv(cx1-cx0+2*mg, cy1-cy0+2*mg), "Ground")

    road_xs = [(cx0, cx0+ROAD_W)]
    for k in range(max_col // SKIP + 1):
        x0 = (k+1)*SKIP*CELLSIZE - SEP - ROAD_W
        if cx0 < x0 < cx1: road_xs.append((x0, x0+ROAD_W))
    road_xs.append((cx1-ROAD_W, cx1))

    road_ys = [(cy0, cy0+ROAD_W)]
    for k in range(max_row // SKIP + 1):
        y0 = (k+1)*SKIP*CELLSIZE - SEP - ROAD_W
        if cy0 < y0 < cy1: road_ys.append((y0, y0+ROAD_W))
    road_ys.append((cy1-ROAD_W, cy1))

    for x0, x1 in road_xs: _vstrip(geom, x0, x1, cy0, cy1, "Road")
    for y0, y1 in road_ys: _hstrip(geom, y0, y1, cx0, cx1, "Road")

    sw = 1.5
    for x0, x1 in road_xs:
        if x0-sw > cx0: _vstrip(geom, x0-sw, x0, cy0, cy1, "Sidewalk")
        if x1+sw < cx1: _vstrip(geom, x1, x1+sw, cy0, cy1, "Sidewalk")
    for y0, y1 in road_ys:
        if y0-sw > cy0: _hstrip(geom, y0-sw, y0, cx0, cx1, "Sidewalk")
        if y1+sw < cy1: _hstrip(geom, y1, y1+sw, cx0, cx1, "Sidewalk")

    mw, ml, mg2 = 0.12, 3.0, 3.0
    for x0, x1 in road_xs:
        cx = (x0+x1)/2
        y  = cy0 + 1.0
        while y+ml < cy1:
            _vstrip(geom, cx-mw/2, cx+mw/2, y, y+ml, "Marking"); y += ml+mg2
    for y0, y1 in road_ys:
        cy = (y0+y1)/2
        x  = cx0 + 1.0
        while x+ml < cx1:
            _hstrip(geom, cy-mw/2, cy+mw/2, x, x+ml, "Marking"); x += ml+mg2

# ── glTF packer ──────────────────────────────────────────────────────────────

def pack_gltf(geom, out_path):
    """Pack GLTFGeom into a .glb file."""
    gltf = GLTF2()
    gltf.asset = Asset(version="2.0", generator="3D Buildings Generator")
    gltf.scene = 0
    gltf.scenes = [Scene(nodes=[0])]
    gltf.nodes  = [Node(mesh=0)]

    blob         = b""
    buffer_views = []
    accessors    = []
    primitives   = []
    materials    = []

    def _pad4(n):
        """Return number of padding bytes needed to align n to 4 bytes."""
        return (4 - n % 4) % 4

    def _append(data):
        """Append data to blob, pad to 4-byte boundary, return (offset, length)."""
        nonlocal blob
        off = len(blob)
        blob += data
        blob += b"\x00" * _pad4(len(data))   # alignment padding
        return off, len(data)                  # byteLength = ACTUAL size, not padded

    for mat_name in geom.materials():
        pos_arr = np.array(geom.pos[mat_name], dtype=np.float32)
        uv_arr  = np.array(geom.uvs[mat_name],  dtype=np.float32)
        idx_arr = np.array(geom.idx[mat_name],   dtype=np.uint32)

        if len(idx_arr) == 0:
            continue

        # Positions
        bv_pos_off, bv_pos_len = _append(pos_arr.tobytes())
        bv_pos = BufferView(buffer=0, byteOffset=bv_pos_off,
                            byteLength=bv_pos_len, target=ARRAY_BUFFER)
        acc_pos = Accessor(bufferView=len(buffer_views), byteOffset=0,
                           componentType=FLOAT, count=len(pos_arr),
                           type=VEC3,
                           min=pos_arr.min(axis=0).tolist(),
                           max=pos_arr.max(axis=0).tolist())
        acc_pos_i = len(accessors)
        buffer_views.append(bv_pos)
        accessors.append(acc_pos)

        # UVs
        bv_uv_off, bv_uv_len = _append(uv_arr.tobytes())
        bv_uv = BufferView(buffer=0, byteOffset=bv_uv_off,
                           byteLength=bv_uv_len, target=ARRAY_BUFFER)
        acc_uv = Accessor(bufferView=len(buffer_views), byteOffset=0,
                          componentType=FLOAT, count=len(uv_arr), type=VEC2)
        acc_uv_i  = len(accessors)
        buffer_views.append(bv_uv)
        accessors.append(acc_uv)

        # Indices
        bv_idx_off, bv_idx_len = _append(idx_arr.tobytes())
        bv_idx = BufferView(buffer=0, byteOffset=bv_idx_off,
                            byteLength=bv_idx_len, target=ELEMENT_ARRAY_BUFFER)
        acc_idx = Accessor(bufferView=len(buffer_views), byteOffset=0,
                           componentType=UNSIGNED_INT, count=len(idx_arr), type=SCALAR)
        acc_idx_i = len(accessors)
        buffer_views.append(bv_idx)
        accessors.append(acc_idx)

        # Material
        r, g, b = _mat_color(mat_name)
        alpha   = 0.55 if mat_name == "Window" else 1.0
        mat = Material(
            name=mat_name,
            pbrMetallicRoughness=PbrMetallicRoughness(
                baseColorFactor=[r, g, b, alpha],
                metallicFactor=_mat_metallic(mat_name),
                roughnessFactor=_mat_roughness(mat_name),
            ),
            alphaMode="BLEND" if alpha < 1.0 else "OPAQUE",
            doubleSided=True,
        )
        mat_i = len(materials)
        materials.append(mat)

        primitives.append(Primitive(
            attributes={"POSITION": acc_pos_i, "TEXCOORD_0": acc_uv_i},
            indices=acc_idx_i,
            material=mat_i,
        ))

    gltf.bufferViews = buffer_views
    gltf.accessors   = accessors
    gltf.meshes      = [Mesh(name="city", primitives=primitives)]
    gltf.materials   = materials
    gltf.buffers     = [Buffer(byteLength=len(blob))]
    gltf.set_binary_blob(blob)
    gltf.save_binary(out_path)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    lod = ARGS.lod
    print(f"Parsing {ARGS.input} ...")
    root = etree.parse(ARGS.input).getroot()
    buildings = root.findall("building")
    print(f"Found {len(buildings)} building(s). Exporting at LOD {lod} ...")

    # ── Scene analysis (same logic as exportToOBJ.py) ────────────────────────
    all_ox = [float(b.findtext("origin").split()[0]) for b in buildings]
    all_oy = [float(b.findtext("origin").split()[1]) for b in buildings]
    offset = (min(all_ox), min(all_oy))
    if offset != (0.0, 0.0):
        print(f"Normalising coordinates: subtracting offset ({offset[0]:.1f}, {offset[1]:.1f})")

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

    all_xs = [float(b.findtext("xSize")) for b in buildings]
    all_ys = [float(b.findtext("ySize")) for b in buildings]
    city_w = max((ox - offset[0]) + xs for ox, xs in zip(all_ox, all_xs))
    city_h = max((oy - offset[1]) + ys for oy, ys in zip(all_oy, all_ys))

    if ARGS.split:
        out_dir = ARGS.output
        os.makedirs(out_dir, exist_ok=True)
        for i, b in enumerate(buildings):
            g = GLTFGeom()
            export_building(b, g, lod, offset)
            bid   = b.attrib.get("ID", f"b{i:04d}")
            fname = os.path.join(out_dir, f"{bid[:8]}_{i:04d}.glb")
            pack_gltf(g, fname)
        print(f"Written {len(buildings)} .glb files to {out_dir}/")
    else:
        geom = GLTFGeom()

        if is_grid:
            generate_road_network(geom, max_col, max_row)
        else:
            margin = 20.0
            geom.poly([(-margin, -margin, -0.05),
                       (city_w+margin, -margin, -0.05),
                       (city_w+margin, city_h+margin, -0.05),
                       (-margin, city_h+margin, -0.05)],
                      flat_uv(city_w+2*margin, city_h+2*margin), "Ground")

        for b in buildings:
            export_building(b, geom, lod, offset)

        parks_el = root.find("parks")
        if parks_el is not None:
            for park in parks_el.findall("park"):
                txt = park.findtext("outline")
                if txt:
                    coords = [float(v) for v in txt.split()]
                    px0 = coords[0] - offset[0]; py0 = coords[1] - offset[1]
                    px1 = coords[2] - offset[0]; py1 = coords[3] - offset[1]
                    geom.poly([(px0,py0,0.02),(px1,py0,0.02),(px1,py1,0.02),(px0,py1,0.02)],
                              flat_uv(abs(px1-px0), abs(py1-py0)), "Park")

        pack_gltf(geom, ARGS.output)
        print(f"Written {ARGS.output}")
        print()
        print("Import into your engine:")
        print("  Blender     : File > Import > glTF 2.0 (.glb)")
        print("  Unity       : drag the .glb into your Assets folder")
        print("  Unreal      : File > Import into Level  (select the .glb)")
        print("  Godot       : drag into FileSystem panel, then into scene")
        print("  Web (Three) : GLTFLoader.load('city.glb', ...)")

if __name__ == "__main__":
    main()
