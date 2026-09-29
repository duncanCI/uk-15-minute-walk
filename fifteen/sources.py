"""
OSM data loader that reads from Esri's hosted OpenStreetMap mirror
(services-eu1.arcgis.com, refreshed from OSM regularly) instead of Overpass.

Why: Overpass and Nominatim were not reachable from the build environment.
The mirror carries the same OSM ways and nodes, so the pipeline is otherwise
identical to the osmnx version. Swap in `osmnx` loaders if you run locally.

Everything is returned in EPSG:27700 (British National Grid, metres).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import LineString, Point, Polygon, MultiPolygon

OSM = "https://services-eu1.arcgis.com/zci5bUiJ8olAal7N/ArcGIS/rest/services"
LAYERS = {
    "highways": f"{OSM}/OpenStreetMap_Highways_for_Europe/FeatureServer/0",
    "pois": f"{OSM}/OpenStreetMap_POIs_for_Europe/FeatureServer/0",          # OSM nodes
    "buildings": f"{OSM}/OpenStreetMap_Buildings_for_Europe/FeatureServer/0",  # building=* ways
    "leisure": f"{OSM}/OpenStreetMap_Leisure_Areas_for_Europe/FeatureServer/0",
    "medical": f"{OSM}/OpenStreetMap_Medical_Facility_Polygons_for_Europe/FeatureServer/0",
    "water": f"{OSM}/OpenStreetMap_Water_Bodies_for_Europe/FeatureServer/0",
}
ONS_IPN = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
           "IPN_GB_2020_2022/FeatureServer/0/query")
ONS_LAD = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
           "Local_Authority_Districts_DEC_2025_Boundaries_UK_BFC/FeatureServer/0/query")

CACHE = Path(__file__).resolve().parents[1] / "cache"
CACHE.mkdir(exist_ok=True)
SESSION = requests.Session()


def _get(url: str, params: dict, tries: int = 5) -> dict:
    for k in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=180)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2 * (k + 1))
    return {}


def _esri_to_shape(geom: dict, gtype: str):
    if not geom:
        return None
    if gtype == "esriGeometryPoint":
        return Point(geom["x"], geom["y"])
    if gtype == "esriGeometryPolyline":
        return [LineString(p) for p in geom.get("paths", []) if len(p) >= 2]
    if gtype == "esriGeometryPolygon":
        rings = [r for r in geom.get("rings", []) if len(r) >= 4]
        if not rings:
            return None
        # Esri rings: clockwise = outer. Good enough to take outers + holes.
        outers, holes = [], []
        for r in rings:
            a = 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r[:-1], r[1:]))
            (outers if a < 0 else holes).append(r)
        polys = [Polygon(o) for o in outers] or [Polygon(rings[0])]
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]
    return None


def query_layer(layer: str, where: str, bbox_4326, fields: list[str],
                tiles: int = 1, workers: int = 6) -> gpd.GeoDataFrame:
    """Page through an ArcGIS feature layer inside a lon/lat bbox, output EPSG:27700."""
    url = LAYERS[layer] + "/query"
    meta = _get(LAYERS[layer], {"f": "json"})
    gtype, oid = meta["geometryType"], meta.get("objectIdField", "OBJECTID")
    fields = list(dict.fromkeys([oid, *fields]))
    x0, y0, x1, y1 = bbox_4326
    xs, ys = np.linspace(x0, x1, tiles + 1), np.linspace(y0, y1, tiles + 1)
    envs = [dict(xmin=xs[i], ymin=ys[j], xmax=xs[i + 1], ymax=ys[j + 1],
                 spatialReference=dict(wkid=4326))
            for i in range(tiles) for j in range(tiles)]

    def fetch_env(env):
        base = dict(where=where, geometry=json.dumps(env), geometryType="esriGeometryEnvelope",
                    inSR=4326, spatialRel="esriSpatialRelIntersects", outSR=27700,
                    outFields=",".join(fields), orderByFields=oid, f="json")
        n = _get(url, {**base, "returnCountOnly": "true"}).get("count", 0)
        out = []
        for off in range(0, n, 2000):
            d = _get(url, {**base, "resultOffset": off, "resultRecordCount": 2000})
            out.extend(d.get("features", []))
        return out

    with ThreadPoolExecutor(workers) as ex:
        feats = [f for chunk in ex.map(fetch_env, envs) for f in chunk]

    rows, geoms = [], []
    for f in feats:
        shp = _esri_to_shape(f.get("geometry"), gtype)
        if shp is None:
            continue
        for s in (shp if isinstance(shp, list) else [shp]):
            rows.append(f["attributes"])
            geoms.append(s)
    gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs=27700)
    if oid in gdf and len(gdf):
        # tiles overlap at their edges; keep one copy of each feature part
        gdf["_wkb"] = gdf.geometry.to_wkb()
        gdf = gdf.drop_duplicates([oid, "_wkb"]).drop(columns="_wkb")
    return gdf.reset_index(drop=True)


def councils(names: list[str]) -> gpd.GeoDataFrame:
    w = "LAD25NM IN (%s)" % ",".join(f"'{n}'" for n in names)
    d = _get(ONS_LAD, dict(where=w, outFields="LAD25CD,LAD25NM", outSR=4326, f="geojson"))
    return gpd.GeoDataFrame.from_features(d["features"], crs=4326).to_crs(27700)


def places_ipn(lad_names: list[str]) -> gpd.GeoDataFrame:
    """ONS Index of Place Names (GB) for the given councils: localities, parishes and wards."""
    w = "lad20nm IN (%s)" % ",".join(f"'{n}'" for n in lad_names)
    rows, off = [], 0
    while True:
        d = _get(ONS_IPN, dict(where=w, outFields="place20nm,descnm,lad20nm,lat,long",
                               resultOffset=off, resultRecordCount=2000, f="json"))
        f = d.get("features", [])
        rows += [x["attributes"] for x in f]
        if len(f) < 2000:
            break
        off += 2000
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.long, df.lat), crs=4326).to_crs(27700)


def cached(name: str, fn):
    p = CACHE / f"{name}.parquet"
    if p.exists():
        return gpd.read_parquet(p)
    g = fn()
    g.to_parquet(p)
    return g


# ---------------------------------------------------------------- polygon queries (UK build)
def _post(url: str, data: dict, tries: int = 6) -> dict:
    for k in range(tries):
        try:
            r = SESSION.post(url, data=data, timeout=240)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(3 * (k + 1))
    return {}


def esri_polygon(geom_4326) -> str:
    """Shapely (Multi)Polygon in lon/lat -> Esri JSON polygon, outer rings clockwise."""
    from shapely.geometry.polygon import orient
    polys = getattr(geom_4326, "geoms", [geom_4326])
    rings = []
    for p in polys:
        p = orient(p, sign=-1.0)
        rings.append([[round(x, 6), round(y, 6)] for x, y in p.exterior.coords])
        rings += [[[round(x, 6), round(y, 6)] for x, y in r.coords] for r in p.interiors]
    return json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})


_META: dict = {}


def query_polygon(layer: str, where: str, poly_4326, fields: list[str], out_sr: int = 27700,
                  workers: int = 6, chunk: int = 1000) -> gpd.GeoDataFrame:
    """All features of `layer` intersecting a polygon, fetched by object id in parallel chunks."""
    url = LAYERS[layer]
    if url not in _META:
        _META[url] = _get(url, {"f": "json"})
    meta = _META[url]
    gtype, oid = meta["geometryType"], meta.get("objectIdField", "OBJECTID")
    fields = list(dict.fromkeys([oid, *fields]))
    ids = _post(url + "/query", dict(where=where, geometry=esri_polygon(poly_4326),
                                     geometryType="esriGeometryPolygon", inSR=4326,
                                     spatialRel="esriSpatialRelIntersects", returnIdsOnly="true",
                                     f="json")).get("objectIds") or []
    ids = sorted(ids)

    def fetch(sub):
        d = _post(url + "/query", dict(objectIds=",".join(map(str, sub)), outFields=",".join(fields),
                                       outSR=out_sr, f="json"))
        return d.get("features", [])

    with ThreadPoolExecutor(workers) as ex:
        feats = [f for part in ex.map(fetch, [ids[i:i + chunk] for i in range(0, len(ids), chunk)])
                 for f in part]
    rows, geoms = [], []
    for f in feats:
        shp = _esri_to_shape(f.get("geometry"), gtype)
        if shp is None:
            continue
        for s in (shp if isinstance(shp, list) else [shp]):
            rows.append(f["attributes"])
            geoms.append(s)
    return gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs=out_sr).reset_index(drop=True)


def uk_councils(generalised: bool = True) -> gpd.GeoDataFrame:
    """All 361 UK local authority districts (ONS, December 2025), lon/lat."""
    kind = "BGC" if generalised else "BFC"
    url = ONS_LAD.replace("_BFC/", f"_{kind}/")
    rows, off = [], 0
    while True:
        d = _get(url, dict(where="1=1", outFields="LAD25CD,LAD25NM", outSR=4326, f="geojson",
                           resultOffset=off, resultRecordCount=200))
        f = d.get("features", [])
        rows += f
        if len(f) < 200:
            break
        off += 200
    return gpd.GeoDataFrame.from_features(rows, crs=4326)


def ipn_localities() -> gpd.GeoDataFrame:
    """ONS Index of Place Names, localities only, Great Britain."""
    rows, off = [], 0
    while True:
        d = _get(ONS_IPN, dict(where="descnm='LOC'", outFields="place20nm,lad20cd,lat,long",
                               resultOffset=off, resultRecordCount=2000, f="json"))
        f = d.get("features", [])
        rows += [x["attributes"] for x in f]
        if len(f) < 2000:
            break
        off += 2000
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.long, df.lat), crs=4326)
