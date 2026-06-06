"""
Export 3D buildings from the XML spec to glTF 2.0 (.glb).

Each building is a separate glTF Node so viewers can pick/click individual
buildings and read their metadata (floors, height, usage, OSM ID, etc.)
from Node.extras — this powers the interactive viewer.html.

Usage:
    python exportToGLTF.py -i city.xml -o city.glb
    python exportToGLTF.py -i city.xml -o city.glb --lod 3
    python exportToGLTF.py -i city.xml -o city.glb --split

Requirements:
    pip install pygltflib numpy lxml
"""

import argparse, math, os, sys
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
PARSER.add_argument("--lod", type=int, default=2, choices=[0,1,2,3])
PARSER.add_argument("--split", action="store_true")
ARGS = PARSER.parse_args()

# ── Colour palettes ──────────────────────────────────────────────────────────

UV_TILE = 2.5

WALL_PALETTE = [
    (0.93, 0.89, 0.82), (0.82, 0.82, 0.80), (0.80, 0.72, 0.58),
    (0.88, 0.82, 0.68), (0.95, 0.93, 0.89), (0.70, 0.72, 0.75),
]
ROOF_PALETTE = [
    (0.62, 0.22, 0.15), (0.30, 0.32, 0.35), (0.28, 0.48, 0.28),
    (0.55, 0.28, 0.12), (0.20, 0.20, 0.22),
]
_FIXED_MATS = {
    "Floor":    (0.55, 0.53, 0.50),
    "Door":     (0.35, 0.22, 0.12),
    "Window":   (0.55, 0.75, 0.90),
    "Road":     (0.38, 0.38, 0.40),
    "Sidewalk": (0.72, 0.70, 0.67),
    "Marking":  (0.92, 0.92, 0.88),
    "Ground":   (0.28, 0.45, 0.22),
    "Park":     (0.28, 0.62, 0.26),
}

def _mat_color(name):
    if name in _FIXED_MATS: return _FIXED_MATS[name]
    if name.startswith("Wall_"): return WALL_PALETTE[int(name.split("_")[1]) % len(WALL_PALETTE)]
    if name.startswith("Roof_"): return ROOF_PALETTE[int(name.split("_")[1]) % len(ROOF_PALETTE)]
    return (0.8, 0.8, 0.8)

def _mat_roughness(n): return 0.05 if n=="Window" else (0.70 if "Roof" in n else 0.85)
def _mat_metallic(n):  return 0.0

def building_mats(bid):
    h = sum(ord(c)*(i+1) for i,c in enumerate(bid[:16]))
    return f"Wall_{h%len(WALL_PALETTE)}", f"Roof_{(h//7)%len(ROOF_PALETTE)}"

def build_gltf_materials():
    """Create all materials upfront and return (list, name→index dict)."""
    names = ([f"Wall_{i}" for i in range(len(WALL_PALETTE))] +
             [f"Roof_{i}" for i in range(len(ROOF_PALETTE))] +
             list(_FIXED_MATS.keys()))
    mats, idx = [], {}
    for name in names:
        r, g, b = _mat_color(name)
        alpha = 0.55 if name == "Window" else 1.0
        mats.append(Material(
            name=name,
            pbrMetallicRoughness=PbrMetallicRoughness(
                baseColorFactor=[r, g, b, alpha],
                metallicFactor=_mat_metallic(name),
                roughnessFactor=_mat_roughness(name),
            ),
            alphaMode="BLEND" if alpha < 1.0 else "OPAQUE",
            doubleSided=True,
        ))
        idx[name] = len(mats) - 1
    return mats, idx

# ── Geometry accumulator ──────────────────────────────────────────────────────

class GLTFGeom:
    def __init__(self):
        self.pos = defaultdict(list)
        self.uvs = defaultdict(list)
        self.idx = defaultdict(list)

    def poly(self, pts, uvcoords, mat):
        base = len(self.pos[mat])
        self.pos[mat].extend(list(p) for p in pts)
        self.uvs[mat].extend(list(u) for u in (uvcoords or [[0,0]]*len(pts)))
        for i in range(1, len(pts)-1):
            self.idx[mat].extend([base, base+i, base+i+1])

    def materials(self):
        return [m for m in self.pos if self.idx[m]]

# ── UV helpers ────────────────────────────────────────────────────────────────

def _d(a,b): return math.sqrt(sum((a[i]-b[i])**2 for i in range(3)))
def wall_uv(l,h): return [(0,0),(l/UV_TILE,0),(l/UV_TILE,h/UV_TILE),(0,h/UV_TILE)]
def flat_uv(xs,ys): return [(0,0),(xs/UV_TILE,0),(xs/UV_TILE,ys/UV_TILE),(0,ys/UV_TILE)]
def tri_uv(): return [(0,0),(1,0),(0.5,1)]
def edge_uv(pts):
    return [(0,0),(_d(pts[0],pts[1])/UV_TILE,0),
            (_d(pts[0],pts[1])/UV_TILE,_d(pts[0],pts[-1])/UV_TILE),
            (0,_d(pts[0],pts[-1])/UV_TILE)]

def planar_uv(pts):
    """Per-vertex UV by planar XY projection — works for any polygon."""
    mn_x = min(p[0] for p in pts); mn_y = min(p[1] for p in pts)
    return [((p[0]-mn_x)/UV_TILE, (p[1]-mn_y)/UV_TILE) for p in pts]

# ── Building geometry ─────────────────────────────────────────────────────────

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
        rx = ox+xs*0.5
        pts_e = [(ox+xs,oy,zt),(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(rx,oy,zt+h)]
        pts_w = [(ox,oy+ys,zt),(ox,oy,zt),(rx,oy,zt+h),(rx,oy+ys,zt+h)]
        geom.poly(pts_e, edge_uv(pts_e), roof_mat)
        geom.poly(pts_w, edge_uv(pts_w), roof_mat)
        geom.poly([(ox,oy,zt),(rx,oy,zt+h),(ox+xs,oy,zt)], tri_uv(), roof_mat)
        geom.poly([(ox+xs,oy+ys,zt),(rx,oy+ys,zt+h),(ox,oy+ys,zt)], tri_uv(), roof_mat)
    elif rtype == "Hipped":
        r = min(r, ys*0.49)
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
    if wall==0:   x0,x1=ox+wx,ox+wx+ww;  return[(x0,oy,z0),(x1,oy,z0),(x1,oy,z1),(x0,oy,z1)]
    elif wall==1: y0,y1=oy+wx,oy+wx+ww;  return[(ox+xs,y0,z0),(ox+xs,y1,z0),(ox+xs,y1,z1),(ox+xs,y0,z1)]
    elif wall==2: x0,x1=ox+xs-wx-ww,ox+xs-wx; return[(x1,oy+ys,z0),(x0,oy+ys,z0),(x0,oy+ys,z1),(x1,oy+ys,z1)]
    else:         y0,y1=oy+ys-wx-ww,oy+ys-wx; return[(ox,y1,z0),(ox,y0,z0),(ox,y0,z1),(ox,y1,z1)]

def export_building(b, geom, lod, offset=(0.0, 0.0)):
    bid = b.attrib.get("ID","bldg")
    wall_mat, roof_mat = building_mats(bid)

    ox, oy, oz = [float(v) for v in b.findtext("origin").split()]
    ox -= offset[0]; oy -= offset[1]
    xs = float(b.findtext("xSize"))
    ys = float(b.findtext("ySize"))
    zs = float(b.findtext("zSize"))

    roof_el = b.find("roof")
    rtype = roof_el.findtext("roofType") if roof_el is not None else "Flat"
    h_el  = roof_el.find("h") if roof_el is not None else None
    r_el  = roof_el.find("r") if roof_el is not None else None
    h = float(h_el.text) if h_el is not None else 0.0
    r = float(r_el.text) if r_el is not None else ys*0.5

    if lod == 0:
        geom.poly([(ox,oy,oz),(ox+xs,oy,oz),(ox+xs,oy+ys,oz),(ox,oy+ys,oz)],
                  flat_uv(xs,ys), "Floor"); return

    add_box(geom, ox, oy, oz, xs, ys, zs, wall_mat, "Floor")

    if lod == 1:
        geom.poly([(ox,oy,oz+zs),(ox,oy+ys,oz+zs),(ox+xs,oy+ys,oz+zs),(ox+xs,oy,oz+zs)],
                  flat_uv(xs,ys), roof_mat); return

    add_roof(geom, ox, oy, oz+zs, xs, ys, h, r, rtype, roof_mat)

    if lod < 3: return

    door_el = b.find("door")
    if door_el is not None:
        wall=int(door_el.findtext("wall"))
        dox=float(door_el.find("origin/x").text); doy=float(door_el.find("origin/y").text)
        dw=float(door_el.find("size/width").text); dh=float(door_el.find("size/height").text)
        geom.poly(_opening_pts(ox,oy,oz,xs,ys,wall,dox,doy,dw,dh), wall_uv(dw,dh), "Door")

    wins_el = b.find("windows")
    if wins_el is not None:
        for w in wins_el.findall("window"):
            wall=int(w.findtext("wall"))
            wox=float(w.find("origin/x").text); woy=float(w.find("origin/y").text)
            ww=float(w.find("size/width").text); wh=float(w.find("size/height").text)
            geom.poly(_opening_pts(ox,oy,oz,xs,ys,wall,wox,woy,ww,wh), wall_uv(ww,wh), "Window")

    bp = b.find("buildingPart")
    if bp is not None:
        p_orig=float(bp.findtext("partOrigin")); pw=float(bp.findtext("width"))
        pl=float(bp.findtext("length")); ph=float(bp.findtext("height"))
        pox,poy,poz = ox+xs, oy+p_orig, oz
        add_box(geom, pox, poy, poz, pw, pl, ph, wall_mat, "Floor")
        geom.poly([(pox,poy,poz+ph),(pox,poy+pl,poz+ph),(pox+pw,poy+pl,poz+ph),(pox+pw,poy,poz+ph)],
                  flat_uv(pw,pl), roof_mat)

# ── Road / park geometry ──────────────────────────────────────────────────────

CELLSIZE=20.0; SKIP=2; ROAD_W=5.0; SEP=1.0

def _vstrip(g, x0,x1,y0,y1,m): g.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)],flat_uv(x1-x0,y1-y0),m)
def _hstrip(g, y0,y1,x0,x1,m): g.poly([(x0,y0,0),(x1,y0,0),(x1,y1,0),(x0,y1,0)],flat_uv(x1-x0,y1-y0),m)

def generate_road_network(geom, max_col, max_row):
    cx0=-(SEP+ROAD_W); cx1=(max_col+1)*CELLSIZE+ROAD_W
    cy0=-(SEP+ROAD_W); cy1=(max_row+1)*CELLSIZE+ROAD_W; mg=10.0
    geom.poly([(cx0-mg,cy0-mg,-0.05),(cx1+mg,cy0-mg,-0.05),
               (cx1+mg,cy1+mg,-0.05),(cx0-mg,cy1+mg,-0.05)],
              flat_uv(cx1-cx0+2*mg,cy1-cy0+2*mg),"Ground")
    road_xs=[(cx0,cx0+ROAD_W)]
    for k in range(max_col//SKIP+1):
        x0=(k+1)*SKIP*CELLSIZE-SEP-ROAD_W
        if cx0<x0<cx1: road_xs.append((x0,x0+ROAD_W))
    road_xs.append((cx1-ROAD_W,cx1))
    road_ys=[(cy0,cy0+ROAD_W)]
    for k in range(max_row//SKIP+1):
        y0=(k+1)*SKIP*CELLSIZE-SEP-ROAD_W
        if cy0<y0<cy1: road_ys.append((y0,y0+ROAD_W))
    road_ys.append((cy1-ROAD_W,cy1))
    for x0,x1 in road_xs: _vstrip(geom,x0,x1,cy0,cy1,"Road")
    for y0,y1 in road_ys: _hstrip(geom,y0,y1,cx0,cx1,"Road")
    sw=1.5
    for x0,x1 in road_xs:
        if x0-sw>cx0: _vstrip(geom,x0-sw,x0,cy0,cy1,"Sidewalk")
        if x1+sw<cx1: _vstrip(geom,x1,x1+sw,cy0,cy1,"Sidewalk")
    for y0,y1 in road_ys:
        if y0-sw>cy0: _hstrip(geom,y0-sw,y0,cx0,cx1,"Sidewalk")
        if y1+sw<cy1: _hstrip(geom,y1,y1+sw,cx0,cx1,"Sidewalk")
    mw,ml,mg2=0.12,3.0,3.0
    for x0,x1 in road_xs:
        cx=(x0+x1)/2; y=cy0+1.0
        while y+ml<cy1: _vstrip(geom,cx-mw/2,cx+mw/2,y,y+ml,"Marking"); y+=ml+mg2
    for y0,y1 in road_ys:
        cy=(y0+y1)/2; x=cx0+1.0
        while x+ml<cx1: _hstrip(geom,cy-mw/2,cy+mw/2,x,x+ml,"Marking"); x+=ml+mg2

def add_osm_roads(geom, roads_el, offset):
    for rd in roads_el.findall("road"):
        nodes_txt = rd.findtext("nodes")
        if not nodes_txt: continue
        hw = float(rd.attrib.get("width", 5.0)) / 2.0
        vals = [float(v) for v in nodes_txt.split()]
        pts = [(vals[i]-offset[0], vals[i+1]-offset[1]) for i in range(0,len(vals)-1,2)]
        for i in range(len(pts)-1):
            p1,p2 = pts[i], pts[i+1]
            dx,dy = p2[0]-p1[0], p2[1]-p1[1]
            seg = math.sqrt(dx*dx+dy*dy)
            if seg < 0.1: continue
            nx,ny = -dy/seg*hw, dx/seg*hw
            geom.poly([(p1[0]-nx,p1[1]-ny,0.01),(p1[0]+nx,p1[1]+ny,0.01),
                       (p2[0]+nx,p2[1]+ny,0.01),(p2[0]-nx,p2[1]-ny,0.01)],
                      flat_uv(hw*2,seg), "Road")

def add_osm_parks(geom, parks_el, offset):
    for park in parks_el.findall("park"):
        poly_txt = park.findtext("polygon")
        if poly_txt:
            vals = [float(v) for v in poly_txt.split()]
            pts = [(vals[i]-offset[0], vals[i+1]-offset[1], 0.02)
                   for i in range(0,len(vals)-1,2)]
            if len(pts) >= 3:
                geom.poly(pts, planar_uv(pts), "Park")
        else:
            txt = park.findtext("outline")
            if txt:
                c = [float(v) for v in txt.split()]
                px0,py0 = c[0]-offset[0], c[1]-offset[1]
                px1,py1 = c[2]-offset[0], c[3]-offset[1]
                geom.poly([(px0,py0,0.02),(px1,py0,0.02),(px1,py1,0.02),(px0,py1,0.02)],
                          flat_uv(abs(px1-px0),abs(py1-py0)), "Park")

# ── glTF packer ───────────────────────────────────────────────────────────────

def pack_gltf_scene(scene_items, out_path):
    """
    Pack a list of {'name', 'geom', 'extras'} items into one .glb.
    Each item becomes a separate glTF Node so viewers can pick it by name
    and read its extras (building metadata for click-to-inspect).
    """
    gltf_materials, mat_index = build_gltf_materials()

    blob = b""
    buffer_views, accessors, meshes, nodes = [], [], [], []

    def pad4(n): return (4 - n % 4) % 4

    def append_data(data, target):
        nonlocal blob
        off = len(blob)
        blob += data
        blob += b"\x00" * pad4(len(data))   # alignment padding
        bv = BufferView(buffer=0, byteOffset=off,
                        byteLength=len(data), target=target)  # actual size
        bv_i = len(buffer_views)
        buffer_views.append(bv)
        return bv_i

    for item in scene_items:
        geom   = item["geom"]
        name   = item["name"]
        extras = item.get("extras")
        primitives = []

        for mat_name in geom.materials():
            pos_arr = np.array(geom.pos[mat_name], dtype=np.float32)
            uv_arr  = np.array(geom.uvs[mat_name],  dtype=np.float32)
            idx_arr = np.array(geom.idx[mat_name],   dtype=np.uint32)
            if len(idx_arr) == 0: continue

            bv_pos = append_data(pos_arr.tobytes(), ARRAY_BUFFER)
            acc_pos_i = len(accessors)
            accessors.append(Accessor(bufferView=bv_pos, byteOffset=0,
                componentType=FLOAT, count=len(pos_arr), type=VEC3,
                min=pos_arr.min(axis=0).tolist(), max=pos_arr.max(axis=0).tolist()))

            bv_uv = append_data(uv_arr.tobytes(), ARRAY_BUFFER)
            acc_uv_i = len(accessors)
            accessors.append(Accessor(bufferView=bv_uv, byteOffset=0,
                componentType=FLOAT, count=len(uv_arr), type=VEC2))

            bv_idx = append_data(idx_arr.tobytes(), ELEMENT_ARRAY_BUFFER)
            acc_idx_i = len(accessors)
            accessors.append(Accessor(bufferView=bv_idx, byteOffset=0,
                componentType=UNSIGNED_INT, count=len(idx_arr), type=SCALAR))

            primitives.append(Primitive(
                attributes={"POSITION": acc_pos_i, "TEXCOORD_0": acc_uv_i},
                indices=acc_idx_i,
                material=mat_index.get(mat_name, 0)
            ))

        if not primitives: continue

        mesh_i = len(meshes)
        meshes.append(Mesh(name=name, primitives=primitives))
        node = Node(mesh=mesh_i, name=name)
        if extras:
            node.extras = extras
        nodes.append(node)

    gltf = GLTF2()
    gltf.asset      = Asset(version="2.0", generator="3D Cadastre Kenya")
    gltf.scene      = 0
    gltf.scenes     = [Scene(nodes=list(range(len(nodes))))]
    gltf.nodes      = nodes
    gltf.meshes     = meshes
    gltf.materials  = gltf_materials
    gltf.accessors  = accessors
    gltf.bufferViews = buffer_views
    gltf.buffers    = [Buffer(byteLength=len(blob))]
    gltf.set_binary_blob(blob)
    gltf.save_binary(out_path)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    lod = ARGS.lod
    print(f"Parsing {ARGS.input} ...")
    root      = etree.parse(ARGS.input).getroot()
    buildings = root.findall("building")
    print(f"Found {len(buildings)} building(s). Exporting at LOD {lod} ...")

    # Coordinate normalisation
    all_ox = [float(b.findtext("origin").split()[0]) for b in buildings]
    all_oy = [float(b.findtext("origin").split()[1]) for b in buildings]
    offset = (min(all_ox), min(all_oy))
    if offset != (0.0, 0.0):
        print(f"Normalising: subtracting offset ({offset[0]:.1f}, {offset[1]:.1f})")

    # Grid vs OSM detection
    orders = set(b.findtext("order","").strip() for b in buildings)
    is_grid = len(orders) > 1
    max_col = max_row = 0
    for b in buildings:
        o = b.findtext("order")
        if o:
            c,r = int(o.split()[0]), int(o.split()[1])
            max_col=max(max_col,c); max_row=max(max_row,r)

    all_xs = [float(b.findtext("xSize")) for b in buildings]
    all_ys = [float(b.findtext("ySize")) for b in buildings]
    city_w = max((ox-offset[0])+xs for ox,xs in zip(all_ox,all_xs))
    city_h = max((oy-offset[1])+ys for oy,ys in zip(all_oy,all_ys))

    if ARGS.split:
        out_dir = ARGS.output
        os.makedirs(out_dir, exist_ok=True)
        for i, b in enumerate(buildings):
            g = GLTFGeom()
            export_building(b, g, lod, offset)
            bid   = b.attrib.get("ID", f"b{i:04d}")
            pack_gltf_scene([{"name": f"building_{bid[:8]}", "geom": g,
                               "extras": _building_extras(b)}],
                            os.path.join(out_dir, f"{bid[:8]}_{i:04d}.glb"))
        print(f"Written {len(buildings)} .glb files to {out_dir}/")
        return

    # ── Combined scene ────────────────────────────────────────────────────────
    scene_items = []

    # Environment (ground + roads + parks)
    env = GLTFGeom()
    if is_grid:
        generate_road_network(env, max_col, max_row)
    else:
        mg = 20.0
        env.poly([(-mg,-mg,-0.05),(city_w+mg,-mg,-0.05),
                  (city_w+mg,city_h+mg,-0.05),(-mg,city_h+mg,-0.05)],
                 flat_uv(city_w+2*mg, city_h+2*mg), "Ground")
        roads_el = root.find("roads")
        if roads_el is not None:
            print(f"  Adding {len(roads_el)} road segments ...")
            add_osm_roads(env, roads_el, offset)

    parks_el = root.find("parks")
    if parks_el is not None:
        print(f"  Adding {len(parks_el)} green areas ...")
        add_osm_parks(env, parks_el, offset)

    scene_items.append({
        "name": "environment",
        "geom": env,
        "extras": {
            "utmOffsetE": round(offset[0], 2),
            "utmOffsetN": round(offset[1], 2),
            "crs": "EPSG:21037",
        },
    })

    # Individual buildings — each as its own Node for click-to-inspect
    for b in buildings:
        g = GLTFGeom()
        export_building(b, g, lod, offset)
        bid = b.attrib.get("ID", "bldg")
        scene_items.append({
            "name":   f"building_{bid[:8]}",
            "geom":   g,
            "extras": _building_extras(b),
        })

    pack_gltf_scene(scene_items, ARGS.output)
    print(f"Written {ARGS.output}  ({len(scene_items)-1} buildings + environment)")
    print()
    print("Open in the interactive viewer:")
    print("  python -m http.server 8000  then open viewer.html?model=" +
          os.path.basename(ARGS.output))
    print()
    print("Or import into your engine:")
    print("  Blender  : File > Import > glTF 2.0 (.glb)")
    print("  Unity    : drag the .glb into Assets")
    print("  Godot    : drag into FileSystem, then into scene")

def _building_extras(b):
    """Extract all displayable metadata from a building XML element."""
    props_el = b.find("properties")
    props    = props_el if props_el is not None else etree.Element("properties")
    xs = float(b.findtext("xSize") or 0)
    ys = float(b.findtext("ySize") or 0)
    floors = int(b.findtext("floors") or 1)
    zs = float(b.findtext("zSize") or 0)
    fp_area = round(xs * ys, 1)
    gfa     = round(fp_area * floors, 1)

    roof_el  = b.find("roof")
    rtype    = roof_el.findtext("roofType", "Flat") if roof_el is not None else "Flat"

    extras = {
        "buildingID":       b.attrib.get("ID", ""),
        "floors":           floors,
        "heightM":          round(zs, 1),
        "footprintAreaM2":  fp_area,
        "grossFloorAreaM2": gfa,
        "roofType":         rtype,
        "usage":            props.findtext("usage") or "-",
        "yearBuilt":        props.findtext("yearOfConstruction") or "-",
        "valuation":        props.findtext("valuation") or "-",
    }
    osm_id = props.findtext("osmID")
    if osm_id:
        extras["osmID"] = int(osm_id)
    name_tag = props.findtext("name")
    if name_tag:
        extras["name"] = name_tag
    return extras

if __name__ == "__main__":
    main()
