# 3D Buildings Generator

Procedurally generates entire city blocks with randomised buildings — roofs, windows, doors, garages — and exports them to **CityGML** (geospatial standard) or **Wavefront OBJ** (game engines / Blender).

Inspired by Esri's CityEngine. Supports 16 levels of detail (LOD 0–4).

---

## Quick start

```bash
pip install -r requirements.txt

# 1. Generate building specifications
python randomiseCity.py -n 100 -o city.xml

# 2a. Export to OBJ (game engines, Blender)
python exportToOBJ.py -i city.xml -o city.obj

# 2b. Export to CityGML (GIS tools, CesiumJS)
mkdir gml_output
python generateCityGML.py -i city.xml -o gml_output
```

---

## Step 1 — randomiseCity.py

Generates an XML file describing every building (dimensions, roof type, floors, windows, doors, etc.).

| Flag | Description |
|------|-------------|
| `-n` | Number of buildings (default 1000) |
| `-o` | Output filename (default `BuildingInformation.xml`) |
| `-r 1` | Enable random rotation |
| `-s 1` | Generate a road network |
| `-v 1` | Generate parks / vegetation |
| `-p 1` | Generate building parts (garages, alcoves) |
| `-c Nordoostpolder` | Use a real-world coordinate origin |

**Roof types:** Flat · Shed · Gabled · Hipped · Pyramidal

---

## Step 2a — exportToOBJ.py  *(game engine export)*

Converts the XML spec directly to a `.obj` + `.mtl` file pair, ready for Unity, Unreal Engine, Blender, or any 3D DCC tool.

```bash
python exportToOBJ.py -i city.xml -o city.obj
python exportToOBJ.py -i city.xml -o city.obj --lod 1   # simple boxes
python exportToOBJ.py -i city.xml -o city.obj --lod 3   # + doors & windows
python exportToOBJ.py -i city.xml -o buildings/ --split # one .obj per building
```

### LOD levels

| LOD | Contents |
|-----|----------|
| 0   | Footprint polygon only |
| 1   | Box / block with flat top |
| 2   | Box + shaped roof *(default)* |
| 3   | LOD 2 + doors, wall windows, building parts |

### Importing into game engines

**Blender:** File → Import → Wavefront (.obj)

**Unity:** Drag the `.obj` **and** `.mtl` into your Assets folder. Unity imports them together.

**Unreal Engine:** File → Import into Level → select the `.obj`.

---

## Step 2b — generateCityGML.py  *(GIS / CityGML export)*

Converts the XML spec to CityGML 2.0 files, compatible with CesiumJS, FME, QGIS, and other GIS platforms.

```bash
mkdir output
python generateCityGML.py -i city.xml -o output
python generateCityGML.py -i city.xml -o output -s 1 -v 1   # roads + vegetation
python generateCityGML.py -i city.xml -o output -gr 1 -ov 1 # all variants & solids
```

Output is a set of `.gml` files — one per LOD/variant combination (LOD0–LOD3, interior models, road network, plant cover).

---

## Requirements

- Python 3.8+
- `lxml` — XML handling
- `numpy` — matrix operations in the CityGML generator
