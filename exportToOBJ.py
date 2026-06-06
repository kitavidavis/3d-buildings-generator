"""
Export 3D buildings from the XML spec to Wavefront OBJ + MTL.
Reads the same BuildingInformation.xml produced by randomiseCity.py and
generates geometry that can be imported directly into Unity, Unreal Engine,
Blender, and any other DCC tool.

Usage:
    python exportToOBJ.py -i BuildingInformation.xml -o city.obj
    python exportToOBJ.py -i BuildingInformation.xml -o city.obj --lod 2
    python exportToOBJ.py -i BuildingInformation.xml -o city.obj --split

LOD levels:
    0  -- footprint polygon only (2D outline extruded flat)
    1  -- simple block (box with flat top, no roof detail)
    2  -- block + roof shape (Flat/Shed/Gabled/Hipped/Pyramidal)  [default]
    3  -- LOD2 + doors and wall windows as inset quads

--split  writes one .obj per building instead of a single combined file.
"""

import argparse
import math
import os
import sys
from lxml import etree

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

PARSER = argparse.ArgumentParser(description="Export buildings XML → OBJ for game engines")
PARSER.add_argument("-i", "--input",  required=True,  help="Input XML file (BuildingInformation.xml)")
PARSER.add_argument("-o", "--output", required=True,  help="Output .obj file (or directory when --split)")
PARSER.add_argument("--lod",   type=int, default=2, choices=[0, 1, 2, 3],
                    help="Level of detail: 0=footprint, 1=block, 2=roof, 3=openings (default: 2)")
PARSER.add_argument("--split", action="store_true",
                    help="Write one .obj per building rather than a single combined file")
ARGS = PARSER.parse_args()


# ---------------------------------------------------------------------------
# Material library
# ---------------------------------------------------------------------------

MATERIALS = {
    "Wall":     {"Kd": (0.80, 0.75, 0.68)},
    "Roof":     {"Kd": (0.55, 0.22, 0.18)},
    "Floor":    {"Kd": (0.60, 0.58, 0.55)},
    "Door":     {"Kd": (0.35, 0.22, 0.12)},
    "Window":   {"Kd": (0.55, 0.75, 0.90), "d": 0.6},
    "Road":     {"Kd": (0.25, 0.25, 0.25)},
    "Park":     {"Kd": (0.25, 0.60, 0.25)},
}


def write_mtl(path):
    with open(path, "w") as f:
        for name, props in MATERIALS.items():
            f.write(f"newmtl {name}\n")
            r, g, b = props["Kd"]
            f.write(f"Kd {r:.3f} {g:.3f} {b:.3f}\n")
            f.write(f"Ka 0.100 0.100 0.100\n")
            f.write(f"Ks 0.050 0.050 0.050\n")
            f.write(f"Ns 10.0\n")
            if "d" in props:
                f.write(f"d {props['d']}\n")
            f.write("\n")


# ---------------------------------------------------------------------------
# OBJ builder – accumulates vertices and faces
# ---------------------------------------------------------------------------

class OBJBuilder:
    def __init__(self):
        self.vertices = []   # list of (x, y, z)
        self.faces = []      # list of (mat_name, [v_indices 1-based])
        self.groups = []     # list of (group_name, face_start_idx)
        self._cur_group = None

    def group(self, name):
        self._cur_group = name
        self.groups.append((name, len(self.faces)))

    def add_polygon(self, points, mat):
        """points: list of (x,y,z). Returns face index."""
        base = len(self.vertices) + 1
        self.vertices.extend(points)
        indices = list(range(base, base + len(points)))
        self.faces.append((mat, indices, self._cur_group))
        return len(self.faces) - 1

    def write(self, obj_path, mtl_name):
        with open(obj_path, "w") as f:
            f.write(f"# 3D Buildings Generator\n")
            f.write(f"mtllib {mtl_name}\n\n")
            for x, y, z in self.vertices:
                f.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
            f.write("\n")
            current_mat = None
            current_group = None
            for mat, indices, grp in self.faces:
                if grp != current_group:
                    f.write(f"g {grp}\n")
                    current_group = grp
                if mat != current_mat:
                    f.write(f"usemtl {mat}\n")
                    current_mat = mat
                f.write("f " + " ".join(str(i) for i in indices) + "\n")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def rect_face(ox, oy, oz, dx, dy, dz, close=True):
    """Four corners of an axis-aligned quad given origin + two edge deltas."""
    pts = [
        (ox,      oy,      oz),
        (ox+dx,   oy+dy,   oz),
        (ox+dx+dz[0], oy+dy+dz[1], oz+dz[2]),
        (ox+dz[0],    oy+dz[1],    oz+dz[2]),
    ]
    return pts


def box_faces(ox, oy, oz, xs, ys, zs):
    """All 6 faces of an axis-aligned box as lists of (x,y,z) quads.
    Returns dict with keys: bottom, top, south, north, west, east."""
    return {
        "bottom": [(ox,    oy,    oz),    (ox+xs, oy,    oz),    (ox+xs, oy+ys, oz),    (ox,    oy+ys, oz)],
        "top":    [(ox,    oy,    oz+zs), (ox,    oy+ys, oz+zs), (ox+xs, oy+ys, oz+zs), (ox+xs, oy,    oz+zs)],
        "south":  [(ox,    oy,    oz),    (ox,    oy,    oz+zs), (ox+xs, oy,    oz+zs), (ox+xs, oy,    oz)],
        "north":  [(ox+xs, oy+ys, oz),    (ox+xs, oy+ys, oz+zs), (ox,    oy+ys, oz+zs), (ox,    oy+ys, oz)],
        "west":   [(ox,    oy+ys, oz),    (ox,    oy+ys, oz+zs), (ox,    oy,    oz+zs), (ox,    oy,    oz)],
        "east":   [(ox+xs, oy,    oz),    (ox+xs, oy,    oz+zs), (ox+xs, oy+ys, oz+zs), (ox+xs, oy+ys, oz)],
    }


# ---------------------------------------------------------------------------
# Roof generators  (return list of polygon point-lists)
# ---------------------------------------------------------------------------

def roof_flat(ox, oy, zs, xs, ys):
    return [
        [(ox, oy, zs), (ox+xs, oy, zs), (ox+xs, oy+ys, zs), (ox, oy+ys, zs)]
    ]


def roof_shed(ox, oy, zs, xs, ys, h):
    """Mono-pitch: west edge at zs+h, east edge at zs."""
    return [
        # roof face (quad)
        [(ox,    oy,    zs+h), (ox+xs, oy,    zs),
         (ox+xs, oy+ys, zs),   (ox,    oy+ys, zs+h)],
        # west gable triangle
        [(ox, oy, zs), (ox, oy, zs+h), (ox, oy+ys, zs+h), (ox, oy+ys, zs)],
    ]


def roof_gabled(ox, oy, zs, xs, ys, h):
    ridge_x = ox + xs * 0.5
    return [
        # south slope
        [(ox,      oy, zs),   (ox+xs, oy, zs),   (ridge_x, oy, zs+h)],
        # north slope
        [(ox+xs,   oy+ys, zs), (ox,   oy+ys, zs), (ridge_x, oy+ys, zs+h)],
        # east slope
        [(ox+xs, oy, zs),    (ox+xs, oy+ys, zs),  (ridge_x, oy+ys, zs+h), (ridge_x, oy, zs+h)],
        # west slope
        [(ox,    oy+ys, zs), (ox,    oy, zs),      (ridge_x, oy, zs+h),    (ridge_x, oy+ys, zs+h)],
        # south gable
        [(ox, oy, zs), (ridge_x, oy, zs+h), (ox+xs, oy, zs)],
        # north gable
        [(ox+xs, oy+ys, zs), (ridge_x, oy+ys, zs+h), (ox, oy+ys, zs)],
    ]


def roof_hipped(ox, oy, zs, xs, ys, h, r):
    """r = ridge half-width (distance from each end to ridge start)."""
    r = min(r, ys * 0.49)
    rx0 = ox + xs * 0.5
    ry0 = oy + r
    ry1 = oy + ys - r
    return [
        # south hip
        [(ox, oy, zs),    (ox+xs, oy, zs),  (rx0, ry0, zs+h)],
        # north hip
        [(ox+xs, oy+ys, zs), (ox, oy+ys, zs), (rx0, ry1, zs+h)],
        # east slope
        [(ox+xs, oy, zs),    (ox+xs, oy+ys, zs), (rx0, ry1, zs+h), (rx0, ry0, zs+h)],
        # west slope
        [(ox,    oy+ys, zs), (ox,    oy, zs),    (rx0, ry0, zs+h), (rx0, ry1, zs+h)],
    ]


def roof_pyramidal(ox, oy, zs, xs, ys, h):
    apex = (ox + xs * 0.5, oy + ys * 0.5, zs + h)
    return [
        [(ox,    oy,    zs), (ox+xs, oy,    zs), apex],
        [(ox+xs, oy,    zs), (ox+xs, oy+ys, zs), apex],
        [(ox+xs, oy+ys, zs), (ox,    oy+ys, zs), apex],
        [(ox,    oy+ys, zs), (ox,    oy,    zs), apex],
    ]


# ---------------------------------------------------------------------------
# Opening helpers (LOD 3)
# ---------------------------------------------------------------------------

def _wall_opening(ox, oy, oz, xs, ys, zs, wall, w_orig_x, w_orig_y, w_w, w_h):
    """Return a quad for a door/window on the given wall face.
    wall: 0=south (y=oy), 1=east (x=ox+xs), 2=north (y=oy+ys), 3=west (x=ox)
    Origin along the wall is measured from the building's SW corner along that wall.
    """
    z0 = oz + w_orig_y
    z1 = oz + w_orig_y + w_h
    if wall == 0:   # south face, y=oy, runs along x
        x0, x1 = ox + w_orig_x, ox + w_orig_x + w_w
        return [(x0, oy, z0), (x1, oy, z0), (x1, oy, z1), (x0, oy, z1)]
    elif wall == 1: # east face, x=ox+xs, runs along y
        y0, y1 = oy + w_orig_x, oy + w_orig_x + w_w
        return [(ox+xs, y0, z0), (ox+xs, y1, z0), (ox+xs, y1, z1), (ox+xs, y0, z1)]
    elif wall == 2: # north face, y=oy+ys, runs along -x
        x0, x1 = ox + xs - w_orig_x - w_w, ox + xs - w_orig_x
        return [(x1, oy+ys, z0), (x0, oy+ys, z0), (x0, oy+ys, z1), (x1, oy+ys, z1)]
    else:           # west face, x=ox, runs along -y
        y0, y1 = oy + ys - w_orig_x - w_w, oy + ys - w_orig_x
        return [(ox, y1, z0), (ox, y0, z0), (ox, y0, z1), (ox, y1, z1)]


# ---------------------------------------------------------------------------
# Per-building export
# ---------------------------------------------------------------------------

def export_building(b, builder, lod):
    bid = b.attrib.get("ID", "building")
    builder.group(f"building_{bid[:8]}")

    ox, oy, oz = [float(v) for v in b.findtext("origin").split()]
    xs = float(b.findtext("xSize"))
    ys = float(b.findtext("ySize"))
    zs = float(b.findtext("zSize"))

    roof_el   = b.find("roof")
    roof_type = roof_el.findtext("roofType") if roof_el is not None else "Flat"
    h_el      = roof_el.find("h") if roof_el is not None else None
    r_el      = roof_el.find("r") if roof_el is not None else None
    h = float(h_el.text) if h_el is not None else 0.0
    r = float(r_el.text) if r_el is not None else ys * 0.5

    # LOD 0 – footprint only
    if lod == 0:
        builder.add_polygon([(ox, oy, oz), (ox+xs, oy, oz),
                              (ox+xs, oy+ys, oz), (ox, oy+ys, oz)], "Floor")
        return

    # LOD 1+ – box walls
    faces = box_faces(ox, oy, oz, xs, ys, zs)
    builder.add_polygon(faces["bottom"], "Floor")
    for side in ("south", "north", "west", "east"):
        builder.add_polygon(faces[side], "Wall")

    if lod == 1:
        builder.add_polygon(faces["top"], "Roof")
        return

    # LOD 2+ – shaped roof
    if roof_type == "Flat":
        polys = roof_flat(ox, oy, oz+zs, xs, ys)
    elif roof_type == "Shed":
        polys = roof_shed(ox, oy, oz+zs, xs, ys, h)
    elif roof_type == "Gabled":
        polys = roof_gabled(ox, oy, oz+zs, xs, ys, h)
    elif roof_type == "Hipped":
        polys = roof_hipped(ox, oy, oz+zs, xs, ys, h, r)
    elif roof_type == "Pyramidal":
        polys = roof_pyramidal(ox, oy, oz+zs, xs, ys, h)
    else:
        polys = roof_flat(ox, oy, oz+zs, xs, ys)

    for poly in polys:
        builder.add_polygon(poly, "Roof")

    if lod < 3:
        return

    # LOD 3 – add door and windows as surface quads
    door_el = b.find("door")
    if door_el is not None:
        wall  = int(door_el.findtext("wall"))
        dox   = float(door_el.find("origin/x").text)
        doy   = float(door_el.find("origin/y").text)
        dw    = float(door_el.find("size/width").text)
        dh    = float(door_el.find("size/height").text)
        pts   = _wall_opening(ox, oy, oz, xs, ys, zs, wall, dox, doy, dw, dh)
        builder.add_polygon(pts, "Door")

    windows_el = b.find("windows")
    if windows_el is not None:
        for win in windows_el.findall("window"):
            wall  = int(win.findtext("wall"))
            wox   = float(win.find("origin/x").text)
            woy   = float(win.find("origin/y").text)
            ww    = float(win.find("size/width").text)
            wh    = float(win.find("size/height").text)
            pts   = _wall_opening(ox, oy, oz, xs, ys, zs, wall, wox, woy, ww, wh)
            builder.add_polygon(pts, "Window")

    # Building parts (garage / alcove)
    bp = b.find("buildingPart")
    if bp is not None:
        p_orig = float(bp.findtext("partOrigin"))
        pw  = float(bp.findtext("width"))
        pl  = float(bp.findtext("length"))
        ph  = float(bp.findtext("height"))
        # Part is attached on east side (wall 1, x = ox+xs)
        pox, poy, poz = ox + xs, oy + p_orig, oz
        pfaces = box_faces(pox, poy, poz, pw, pl, ph)
        for side in ("south", "north", "west", "east", "top"):
            builder.add_polygon(pfaces[side], "Wall")
        builder.add_polygon(pfaces["bottom"], "Floor")


# ---------------------------------------------------------------------------
# Road / vegetation helpers
# ---------------------------------------------------------------------------

def export_road(outline_coords, builder):
    x0, y0, x1, y1 = outline_coords
    builder.add_polygon([(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)], "Road")


def export_park(outline_coords, builder):
    x0, y0, x1, y1 = outline_coords
    builder.add_polygon([(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)], "Park")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    lod = ARGS.lod
    print(f"Parsing {ARGS.input} ...")
    tree = etree.parse(ARGS.input)
    root = tree.getroot()

    buildings = root.findall("building")
    print(f"Found {len(buildings)} building(s). Exporting at LOD {lod}...")

    if ARGS.split:
        out_dir = ARGS.output
        os.makedirs(out_dir, exist_ok=True)
        mtl_name = "city.mtl"
        write_mtl(os.path.join(out_dir, mtl_name))
        for i, b in enumerate(buildings):
            bld = OBJBuilder()
            export_building(b, bld, lod)
            bid = b.attrib.get("ID", f"building_{i:04d}")
            fname = os.path.join(out_dir, f"{bid[:8]}_{i:04d}.obj")
            bld.write(fname, mtl_name)
        print(f"Written {len(buildings)} .obj files to {out_dir}/")
    else:
        out_path = ARGS.output
        mtl_name = os.path.splitext(os.path.basename(out_path))[0] + ".mtl"
        mtl_path = os.path.join(os.path.dirname(out_path) or ".", mtl_name)
        write_mtl(mtl_path)

        builder = OBJBuilder()

        # Buildings
        for b in buildings:
            export_building(b, builder, lod)

        # Roads
        streets_el = root.find("Streets")
        if streets_el is not None:
            builder.group("roads")
            outline_text = streets_el.findtext("outline")
            if outline_text:
                coords = [float(v) for v in outline_text.split()]
                export_road(coords, builder)

        # Vegetation / parks
        parks_el = root.find("parks")
        if parks_el is not None:
            builder.group("parks")
            for park in parks_el.findall("park"):
                outline_text = park.findtext("outline")
                if outline_text:
                    coords = [float(v) for v in outline_text.split()]
                    export_park(coords, builder)

        builder.write(out_path, mtl_name)
        print(f"Written {out_path}  ({len(builder.vertices)} vertices, {len(builder.faces)} faces)")
        print(f"Material library: {mtl_path}")
        print()
        print("Import into your engine:")
        print("  Blender  : File > Import > Wavefront (.obj)")
        print("  Unity    : drag the .obj and .mtl into your Assets folder")
        print("  Unreal   : File > Import into Level  (select the .obj)")


if __name__ == "__main__":
    main()
