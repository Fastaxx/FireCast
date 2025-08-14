from flask import Flask, request, jsonify, render_template, Response
from shapely.geometry import shape, mapping, Polygon, MultiPolygon, Point
from shapely import affinity
from shapely.ops import transform as shp_transform, unary_union
from shapely.validation import make_valid
from shapely import set_precision
import math, io, base64, datetime, os

import numpy as np
import requests
from zoneinfo import ZoneInfo
import datetime

app = Flask(__name__)

# --- Config externes ---
OPENTOPO_URL = "https://api.opentopodata.org/v1/eudem25m"   # pente (DEM)
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"     # météo

# --- Web Mercator <-> WGS84 (sans pyproj) ---
R = 6378137.0
MAX_LAT = 85.05112878

def _to_mercator_xy(lon, lat):
    x = R * math.radians(lon)
    lat_c = max(min(lat, MAX_LAT), -MAX_LAT)
    y = R * math.log(math.tan(math.pi/4 + math.radians(lat_c)/2))
    return x, y

def _to_lonlat_xy(x, y):
    lon = math.degrees(x / R)
    lat = math.degrees(2 * math.atan(math.exp(y / R)) - math.pi/2)
    return lon, lat

def to_m(x, y, z=None):
    if hasattr(x, "__iter__"):
        xs, ys = [], []
        for xx, yy in zip(x, y):
            X, Y = _to_mercator_xy(xx, yy)
            xs.append(X); ys.append(Y)
        return xs, ys
    else:
        return _to_mercator_xy(x, y)

def to_deg(x, y, z=None):
    if hasattr(x, "__iter__"):
        lons, lats = [], []
        for xx, yy in zip(x, y):
            LON, LAT = _to_lonlat_xy(xx, yy)
            lons.append(LON); lats.append(LAT)
        return lons, lats
    else:
        return _to_lonlat_xy(x, y)

# --- Conventions vent ---
def wind_from_to_towards_deg(wind_from_deg: float) -> float:
    return (wind_from_deg + 180.0) % 360.0

def compass_to_math_deg(compass_deg: float) -> float:
    return 90.0 - compass_deg

# --- Robustesse géométrique ---
def normalize_input(g):
    g = make_valid(g)
    if isinstance(g, MultiPolygon):
        parts = [p for p in g.geoms if not p.is_empty]
        if len(parts) == 0:
            raise ValueError("Empty MultiPolygon")
        g = unary_union(parts)
    return g

def clean_geom(g, grid=0.2, min_area=5.0, simplify_tol=0.0):
    g = make_valid(g)
    try:
        g = set_precision(g, grid)  # Shapely 2+
    except Exception:
        pass
    g = g.buffer(0)
    if simplify_tol > 0:
        g = g.simplify(simplify_tol, preserve_topology=True)
    if isinstance(g, MultiPolygon):
        parts = [p for p in g.geoms if p.area >= min_area]
        if not parts:
            parts = [max(list(g.geoms), key=lambda p: p.area)]
        g = unary_union(parts)
    return g

# --- Minkowski ellipse ---
def elliptic_minkowski_sum(front_m, a_m, b_m, angle_deg):
    if a_m <= 0 or b_m <= 0:
        return front_m
    cx, cy = front_m.centroid.x, front_m.centroid.y
    g = affinity.rotate(front_m, angle_deg, origin=(cx, cy))
    g = affinity.scale(g, xfact=1.0/max(a_m, 1e-9), yfact=1.0/max(b_m, 1e-9), origin=(cx, cy))
    g = g.buffer(1.0, resolution=16, cap_style=1, join_style=1)
    g = affinity.scale(g, xfact=a_m, yfact=b_m, origin=(cx, cy))
    g = affinity.rotate(g, -angle_deg, origin=(cx, cy))
    return make_valid(g).buffer(0)

def compute_base_params(dt_h, wind_ms, wind_from_deg, base_ros_ms, slope_tan, k_w=0.6, k_s=0.4):
    ros = base_ros_ms * (1.0 + k_w*(max(min(wind_ms, 25.0),0.0)/10.0)) * (1.0 + k_s*max(slope_tan,0.0))
    dist = ros * dt_h * 3600.0  # m
    a = max(dist * 1.7, 0.5)
    b = max(dist * 0.7, 0.5)
    towards = wind_from_to_towards_deg(wind_from_deg)
    angle_math = compass_to_math_deg(towards)
    return a, b, angle_math

# --- Aux : degrés -> mètres approximatifs (pour OpenTopoData mask) ---
def _meters_per_degree(lat_deg: float):
    lat_rad = math.radians(lat_deg)
    m_per_deg_lat = 111132.92 - 559.82*math.cos(2*lat_rad) + 1.175*math.cos(4*lat_rad)
    m_per_deg_lon = 111412.84*math.cos(lat_rad) - 93.5*math.cos(3*lat_rad)
    return m_per_deg_lon, m_per_deg_lat

# --- Pente via OpenTopoData (EU-DEM 25 m) ---
def slope_tan_from_opentopo(poly_wgs,
                            target_spacing_m: float = 300.0,
                            max_points: int = 400,
                            batch_size: int = 100):
    minx, miny, maxx, maxy = poly_wgs.bounds
    lat_c = (miny + maxy) / 2.0
    m_per_deg_lon, m_per_deg_lat = _meters_per_degree(lat_c)

    width_m  = max((maxx - minx) * m_per_deg_lon, 1.0)
    height_m = max((maxy - miny) * m_per_deg_lat, 1.0)

    nx = max(3, int(round(width_m  / target_spacing_m)) + 1)
    ny = max(3, int(round(height_m / target_spacing_m)) + 1)
    while nx * ny > max_points:
        nx = max(3, int(nx * 0.9))
        ny = max(3, int(ny * 0.9))

    lons = np.linspace(minx, maxx, nx)
    lats = np.linspace(miny, maxy, ny)
    LON, LAT = np.meshgrid(lons, lats)
    pts = np.column_stack((LAT.ravel(), LON.ravel()))  # (lat, lon)

    elevations = np.full((pts.shape[0],), np.nan, dtype=float)
    for start in range(0, pts.shape[0], batch_size):
        chunk = pts[start:start+batch_size]
        loc_str = "|".join([f"{lat:.6f},{lon:.6f}" for lat, lon in chunk])
        r = requests.post(OPENTOPO_URL, json={"locations": loc_str, "interpolation": "bilinear"}, timeout=20)
        r.raise_for_status()
        js = r.json()
        if js.get("status") != "OK":
            raise RuntimeError(f"OpenTopoData status {js.get('status')}")
        for i, res in enumerate(js.get("results", [])):
            elevations[start+i] = float(res.get("elevation")) if res.get("elevation") is not None else np.nan

    Z = elevations.reshape((ny, nx))
    dx_m = (lons[1] - lons[0]) * m_per_deg_lon if nx > 1 else 1.0
    dy_m = (lats[1] - lats[0]) * m_per_deg_lat if ny > 1 else 1.0
    dz_dy, dz_dx = np.gradient(Z, dy_m, dx_m)
    slope_tan = np.sqrt(dz_dx**2 + dz_dy**2)

    mask_inside = np.zeros_like(Z, dtype=bool)
    for j in range(ny):
        for i in range(nx):
            mask_inside[j, i] = poly_wgs.contains(Point(lons[i], lats[j]))

    valid = mask_inside & np.isfinite(slope_tan) & np.isfinite(Z)
    vals = slope_tan[valid]
    if vals.size == 0:
        raise ValueError("No valid DEM points inside polygon")

    mean_tan = float(np.nanmean(vals))
    p90_tan  = float(np.nanpercentile(vals, 90))
    return {"mean": mean_tan, "p90": p90_tan, "n_points": int(nx*ny), "grid": [int(ny), int(nx)]}

# --- Météo horaire via Open-Meteo (vent 10 m) ---
def fetch_open_meteo(lat: float, lon: float, hours: int, tz: str = "Europe/Paris"):
    """
    Retourne (ws, wd, preview), où:
      - ws: liste m/s par heure (len==hours)
      - wd: liste degrés 'from' par heure (len==hours)
      - preview: quelques lignes pour affichage
    Normalise toutes les dates en timezone-aware (tz) pour éviter
    "can't compare offset-naive and offset-aware datetimes".
    """
    tzinfo = ZoneInfo(tz)

    params = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
        "timezone": tz  # l'API renvoie les heures dans ce fuseau
    }
    r = requests.get(OPENMETEO_URL, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    hourly = js.get("hourly", {})
    times = hourly.get("time", [])
    ws = hourly.get("wind_speed_10m", [])
    wd = hourly.get("wind_direction_10m", [])
    if not times or not ws or not wd:
        raise RuntimeError("Open-Meteo hourly data missing")

    # heure actuelle, arrondie à l'heure, en TZ souhaitée
    now = datetime.datetime.now(tzinfo).replace(minute=0, second=0, microsecond=0)

    # Normaliser toutes les timestamps -> timezone-aware dans tzinfo
    dt_list = []
    for t in times:
        # compatibilité: si la chaîne finit par 'Z', fromisoformat ne l'accepte pas avant Py3.11
        ts = t.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            # L'API a déjà appliqué 'timezone=tz', donc on interprète comme local tz
            dt = dt.replace(tzinfo=tzinfo)
        else:
            # Si déjà tz-aware, on convertit en tz souhaitée
            dt = dt.astimezone(tzinfo)
        dt_list.append(dt)

    # index de la première heure >= maintenant
    idx = next((i for i, d in enumerate(dt_list) if d >= now), 0)

    # Tronquer / compléter pour obtenir 'hours' pas
    ws_h = list(ws[idx: idx + hours])
    wd_h = list(wd[idx: idx + hours])
    while len(ws_h) < hours:
        ws_h.append(ws_h[-1] if ws_h else 0.0)
        wd_h.append(wd_h[-1] if wd_h else 0.0)

    preview = [{
        "t": dt_list[idx+i].isoformat(),
        "ws_ms": float(ws_h[i]),
        "wd_deg": float(wd_h[i])
    } for i in range(hours)]

    return ws_h, wd_h, preview
@app.route("/")
def index():
    return render_template("index.html")

@app.post("/api/simulate")
def simulate():
    data = request.get_json(force=True)
    if "perimeter" not in data:
        return jsonify({"error": "Missing 'perimeter' GeoJSON geometry"}), 400

    try:
        poly_wgs = shape(data["perimeter"])
        poly_wgs = normalize_input(poly_wgs)
    except Exception as e:
        return jsonify({"error": f"Invalid GeoJSON geometry: {e}"}), 400

    hours = int(data.get("hours", 12))
    wind_ms = float(data.get("wind_ms", 6.0))
    wind_from_deg = float(data.get("wind_deg", 0.0))   # convention météo 'from'
    base_ros_ms = float(data.get("base_ros_ms", 0.02))
    slope_tan_user = float(data.get("slope_tan", 0.05))
    accumulate = str(data.get("accumulate", "false")).lower() in ("1","true","yes","on")
    use_dem = str(data.get("use_dem", "false")).lower() in ("1","true","yes","on")
    use_meteo = str(data.get("use_meteo", "false")).lower() in ("1","true","yes","on")

    meta = {"use_dem": use_dem, "use_meteo": use_meteo, "meteo_source": "open-meteo:forecast"}

    # pente (DEM) ou saisie
    slope_used = slope_tan_user
    if use_dem:
        try:
            info = slope_tan_from_opentopo(poly_wgs)
            slope_used = info["mean"]
            meta.update({
                "slope_from_dem_mean": info["mean"],
                "slope_from_dem_p90": info["p90"],
                "n_points_dem": info["n_points"],
                "grid_dem": info["grid"]
            })
        except Exception as e:
            meta["dem_error"] = str(e)

    # météo horaire (vent)
    hourly_ws = None
    hourly_wd = None
    if use_meteo:
        try:
            c = poly_wgs.centroid
            ws, wd, preview = fetch_open_meteo(c.y, c.x, hours, tz="Europe/Paris")
            hourly_ws, hourly_wd = ws, wd   # m/s, deg('from')
            meta["meteo_preview"] = preview[:min(6, len(preview))]
        except Exception as e:
            meta["meteo_error"] = str(e)

    # reprojection -> mètres pour géo-opérations
    poly_m = shp_transform(to_m, poly_wgs)
    poly_m = clean_geom(poly_m, grid=0.2, min_area=5.0, simplify_tol=0.0)

    fronts_features = []
    cur_m = poly_m
    if use_meteo and hourly_ws and hourly_wd:
        # Vent variable: on itère heure par heure (accumulation logique)
        for h in range(1, hours + 1):
            a1, b1, angle = compute_base_params(1.0, hourly_ws[h-1], hourly_wd[h-1], base_ros_ms, slope_used)
            cur_m = elliptic_minkowski_sum(poly_m if h == 1 else cur_m, a1, b1, angle)
            cur_m = clean_geom(cur_m, grid=0.2, min_area=5.0, simplify_tol=0.0)
            cur_wgs = shp_transform(to_deg, cur_m)
            fronts_features.append({
                "type": "Feature",
                "properties": {"hour": h},
                "geometry": mapping(cur_wgs)
            })
        meta["accumulation_effective"] = True
    else:
        # Vent constant: on respecte le choix accumulate / anti-dérive
        a1, b1, angle = compute_base_params(1.0, wind_ms, wind_from_deg, base_ros_ms, slope_used)
        for h in range(1, hours + 1):
            if accumulate:
                cur_m = elliptic_minkowski_sum(poly_m if h == 1 else cur_m, a1, b1, angle)
            else:
                cur_m = elliptic_minkowski_sum(poly_m, a1*h, b1*h, angle)
            cur_m = clean_geom(cur_m, grid=0.2, min_area=5.0, simplify_tol=0.0)
            cur_wgs = shp_transform(to_deg, cur_m)
            fronts_features.append({
                "type": "Feature",
                "properties": {"hour": h},
                "geometry": mapping(cur_wgs)
            })
        meta["accumulation_effective"] = accumulate

    return jsonify({
        "type": "FeatureCollection",
        "features": fronts_features,
        "meta": {**meta, "slope_tan_used": slope_used}
    })

@app.get("/api/selftest")
def selftest():
    square = Polygon([ (1.4,43.6),(1.406,43.6),(1.406,43.6045),(1.4,43.6045) ])
    poly_m = shp_transform(to_m, square)
    poly_m = clean_geom(poly_m)
    a1, b1, angle = compute_base_params(1.0, wind_ms=0.0, wind_from_deg=0.0, base_ros_ms=0.02, slope_tan=0.0)
    geoms = []
    for h in range(1,5):
        g = elliptic_minkowski_sum(poly_m, a1*h, b1*h, angle)
        g = clean_geom(g)
        geoms.append(g)
    areas = [g.area for g in geoms]
    area_increasing = all(areas[i] < areas[i+1] for i in range(len(areas)-1))
    nested = all(geoms[i].buffer(0).within(geoms[i+1].buffer(0)) for i in range(len(geoms)-1))
    return jsonify({"area_increasing": area_increasing, "nested": nested, "areas_m2": areas})

@app.post("/api/report")
def report_pdf():
    try:
        data = request.get_json(force=True)
        params = data.get("params", {})
        img_data = data.get("map_png", "")
        if not img_data.startswith("data:image/png;base64,"):
            return jsonify({"error":"map_png must be a data URL (PNG)"}), 400
        b64 = img_data.split(",",1)[1]
        img_bytes = base64.b64decode(b64)
    except Exception as e:
        return jsonify({"error": f"Bad payload: {e}"}), 400

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 16); c.drawString(40, H-50, "FeuCast — Rapport de simulation (démo)")
    c.setFont("Helvetica", 9); c.drawString(40, H-64, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    y = H-88
    c.setFont("Helvetica-Bold", 11); c.drawString(40, y, "Paramètres"); y -= 12
    c.setFont("Helvetica", 9)
    for k in ["hours","wind_ms","wind_deg","base_ros_ms","slope_tan","accumulate","use_dem","use_meteo"]:
        if k in params: c.drawString(46, y, f"• {k}: {params[k]}"); y -= 12

    try:
        img = ImageReader(io.BytesIO(img_bytes))
        max_w, max_h = W-80, 420
        iw, ih = img.getSize()
        scale = min(max_w/iw, max_h/ih)
        w, h = iw*scale, ih*scale
        c.drawImage(img, 40, 80, width=w, height=h); c.rect(40, 80, w, h)
    except Exception:
        c.drawString(40, 90, "(Carte non lisible)")
    c.showPage(); c.save()
    pdf = buf.getvalue()
    headers = {"Content-Disposition":"attachment; filename=feucast_report.pdf"}
    return Response(pdf, mimetype="application/pdf", headers=headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
