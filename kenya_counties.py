"""
kenya_counties.py — All 47 Kenyan counties with headquarters coordinates.

Used by osmToXML.py, importMicrosoftFootprints.py, build_kenya_dataset.py
and randomiseCity.py.

Coordinates are WGS84 (lat, lon) of each county headquarters town.
UTM Zone 37S (EPSG:21037) values are computed on import for randomiseCity.py.

Sources: Kenya National Bureau of Statistics, Survey of Kenya.
"""

import math

# ── All 47 counties (lat, lon of county headquarters) ────────────────────────
# Organised by region for readability.

ALL_COUNTIES: dict[str, tuple[float, float]] = {

    # ── Nairobi ───────────────────────────────────────────────────────────────
    "Nairobi":          (-1.2884,  36.8218),   # Nairobi City

    # ── Central ──────────────────────────────────────────────────────────────
    "Kiambu":           (-1.0314,  36.8356),   # Kiambu town
    "Murang'a":         (-0.7243,  37.1529),   # Murang'a town
    "Kirinyaga":        (-0.4990,  37.2807),   # Kerugoya / Kutus
    "Nyeri":            (-0.4167,  36.9500),   # Nyeri town
    "Nyandarua":        (-0.2716,  36.3766),   # Ol Kalou

    # ── Eastern ──────────────────────────────────────────────────────────────
    "Machakos":         (-1.5218,  37.2695),   # Machakos town
    "Makueni":          (-1.7835,  37.6344),   # Wote
    "Kitui":            (-1.3666,  38.0099),   # Kitui town
    "Embu":             (-0.5309,  37.4581),   # Embu town
    "Tharaka-Nithi":    (-0.3381,  37.6524),   # Chuka
    "Meru":             ( 0.0473,  37.6494),   # Meru town
    "Isiolo":           ( 0.3541,  37.5822),   # Isiolo town
    "Marsabit":         ( 2.3342,  37.9947),   # Marsabit town

    # ── Rift Valley ───────────────────────────────────────────────────────────
    "Kajiado":          (-1.8511,  36.7763),   # Kajiado town
    "Narok":            (-1.0830,  35.8699),   # Narok town
    "Nakuru":           (-0.3031,  36.0800),   # Nakuru city
    "Laikipia":         ( 0.0172,  37.0744),   # Nanyuki
    "Baringo":          ( 0.4926,  35.7432),   # Kabarnet
    "Elgeyo-Marakwet":  ( 0.6699,  35.5110),   # Iten
    "Nandi":            ( 0.2023,  35.0985),   # Kapsabet
    "Uasin Gishu":      ( 0.5143,  35.2698),   # Eldoret city
    "Trans-Nzoia":      ( 1.0174,  35.0062),   # Kitale town
    "Kericho":          (-0.3697,  35.2836),   # Kericho town
    "Bomet":            (-0.7863,  35.3423),   # Bomet town
    "Samburu":          ( 1.0981,  36.6996),   # Maralal
    "West Pokot":       ( 1.2378,  35.1133),   # Kapenguria
    "Turkana":          ( 3.1191,  35.5970),   # Lodwar

    # ── Western ───────────────────────────────────────────────────────────────
    "Bungoma":          ( 0.5636,  34.5607),   # Bungoma town
    "Busia":            ( 0.4612,  34.1113),   # Busia town
    "Kakamega":         ( 0.2831,  34.7523),   # Kakamega town
    "Vihiga":           ( 0.0768,  34.7219),   # Vihiga town

    # ── Nyanza ────────────────────────────────────────────────────────────────
    "Kisumu":           (-0.1022,  34.7617),   # Kisumu city
    "Siaya":            (-0.0612,  34.2878),   # Siaya town
    "Homa Bay":         (-0.5273,  34.4570),   # Homa Bay town
    "Migori":           (-1.0634,  34.4731),   # Migori town
    "Kisii":            (-0.6816,  34.7667),   # Kisii town
    "Nyamira":          (-0.5632,  34.9352),   # Nyamira town

    # ── Coast ─────────────────────────────────────────────────────────────────
    "Mombasa":          (-4.0435,  39.6682),   # Mombasa city
    "Kwale":            (-4.1735,  39.4522),   # Kwale town
    "Kilifi":           (-3.6305,  39.8499),   # Kilifi town
    "Malindi":          (-3.2138,  40.1169),   # Malindi town
    "Taita-Taveta":     (-3.3960,  38.5566),   # Voi
    "Tana River":       (-1.4987,  40.0311),   # Hola
    "Lamu":             (-2.2696,  40.9023),   # Lamu town

    # ── North Eastern ─────────────────────────────────────────────────────────
    "Garissa":          (-0.4531,  39.6460),   # Garissa town
    "Wajir":            ( 1.7471,  40.0573),   # Wajir town
    "Mandera":          ( 3.9373,  41.8569),   # Mandera town
}

# ── Additional town aliases (used by importMicrosoftFootprints + osmToXML) ───
# Maps common town names to the county that contains them.
TOWN_ALIASES: dict[str, str] = {
    "Eldoret":   "Uasin Gishu",
    "Thika":     "Kiambu",
    "Kitale":    "Trans-Nzoia",
    "Nanyuki":   "Laikipia",
    "Chuka":     "Tharaka-Nithi",
    "Kerugoya":  "Kirinyaga",
    "Wote":      "Makueni",
    "Hola":      "Tana River",
    "Kapenguria":"West Pokot",
    "Maralal":   "Samburu",
    "Lodwar":    "Turkana",
    "Kabarnet":  "Baringo",
    "Kapsabet":  "Nandi",
    "Iten":      "Elgeyo-Marakwet",
    "Ol Kalou":  "Nyandarua",
    "Kajiado":   "Kajiado",
    "Narok":     "Narok",
    "Bomet":     "Bomet",
    "Migori":    "Migori",
    "Homa Bay":  "Homa Bay",
    "Siaya":     "Siaya",
    "Vihiga":    "Vihiga",
    "Busia":     "Busia",
    "Kwale":     "Kwale",
    "Kilifi":    "Kilifi",
    "Malindi":   "Kilifi",
    "Lamu":      "Lamu",
    "Garissa":   "Garissa",
    "Wajir":     "Wajir",
    "Mandera":   "Mandera",
    "Marsabit":  "Marsabit",
    "Isiolo":    "Isiolo",
    "Kakamega":  "Kakamega",
    "Bungoma":   "Bungoma",
}

# ── UTM Zone 37S conversion ───────────────────────────────────────────────────

def _latlon_to_utm37s(lat: float, lon: float) -> tuple[float, float]:
    """Convert WGS84 lat/lon to EPSG:21037 (Arc 1960 / UTM Zone 37S)."""
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
    return round(E, 0), round(Nv, 0)


# Pre-computed UTM Zone 37S origins for all counties.
# Note: counties west of 36°E (Kisumu, Kakamega, Busia, etc.) are
# technically in Zone 36S; the Zone 37S values below are used for
# procedural building generation in randomiseCity.py where the
# absolute accuracy of the offset does not matter.
COUNTY_UTM_ORIGINS: dict[str, tuple[float, float]] = {
    name: _latlon_to_utm37s(lat, lon)
    for name, (lat, lon) in ALL_COUNTIES.items()
}


if __name__ == "__main__":
    print(f"All 47 Kenyan counties loaded.")
    for name, (lat, lon) in ALL_COUNTIES.items():
        e, n = COUNTY_UTM_ORIGINS[name]
        print(f"  {name:<20}  lat={lat:8.4f}  lon={lon:8.4f}  E={e:,.0f}  N={n:,.0f}")
