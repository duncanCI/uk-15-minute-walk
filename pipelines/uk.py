"""
Walking access to daily needs for every council in the UK.

    python -m pipelines.uk fetch            # download OSM per council (resumable)
    python -m pipelines.uk build            # distances, stats, street files (resumable)
    python -m pipelines.uk merge            # web/data/ for the viewer
    python -m pipelines.uk fetch build --only S12000049 N09000003

Each council is processed with a 2 km margin, so streets near a boundary can use
places over the line. Great Britain runs in EPSG:27700, Northern Ireland in EPSG:2157.
"""
from __future__ import annotations

import argparse
import json
import gc
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
from pyproj import Transformer

from fifteen import sources as src
from fifteen.core import POI_TAGS, Network, assemble_pois, filter_ways, nearest_distance

ROOT = Path(__file__).resolve().parents[1]
UK = src.CACHE / "uk"
RAW, OUTC = UK / "raw", UK / "council"
WEB = ROOT / "web" / "data"
CATS = ["daily_needs", "health_services", "education", "active_life"]   # file order
MARGIN_M = 2000
MAX_M = 2540          # distances stored to 2,540 m in 10 m steps; 255 = further
MINUTES = list(range(5, 26))
H3_RES = 8
SIMPLIFY_M = 2.0
Q = 1e5               # coordinates stored in 1e-5 degree units (about 1 m)

HW_FIELDS = ["highway", "access", "foot", "service", "area"]
HW_WHERE = ("highway NOT IN ('construction','proposed','raceway','no','abandoned','planned','razed',"
            "'platform','elevator','escape','rest_area','services','busway','bus_guideway','corridor',"
            "'motorway','motorway_link')")
POI_WHERE = ("leisure IN ('gym','sports_centre','fitness_centre') OR shop IN ('supermarket','convenience') "
             "OR amenity IN ('school','hospital','clinic','pharmacy','doctors')")


def crs_for(code: str) -> int:
    return 2157 if code.startswith("N") else 27700


def councils() -> gpd.GeoDataFrame:
    return src.cached("uk_councils", src.uk_councils).sort_values("LAD25CD").reset_index(drop=True)


def area_of(c: gpd.GeoDataFrame, code: str):
    row = c[c.LAD25CD == code]
    crs = crs_for(code)
    shape = row.to_crs(crs).union_all()
    ring = shape.buffer(MARGIN_M)
    query = gpd.GeoSeries([ring.simplify(200)], crs=crs).to_crs(4326).iloc[0]
    return crs, shape, ring, query


# ---------------------------------------------------------------- fetch
def fetch(code: str, c: gpd.GeoDataFrame) -> None:
    done = RAW / f"{code}.done"
    if done.exists():
        return
    crs, _, _, q = area_of(c, code)
    t = time.time()
    parts = {
        "hw": src.query_polygon("highways", HW_WHERE, q, HW_FIELDS, crs),
        "pts": src.query_polygon("pois", POI_WHERE, q, ["name", "leisure", "shop", "amenity"], crs),
        "bld": src.query_polygon("buildings", POI_WHERE + " OR building='school'", q,
                                 ["name", "leisure", "shop", "amenity", "building"], crs),
        "lei": src.query_polygon("leisure", "leisure IN ('gym','sports_centre','fitness_centre')", q,
                                 ["name", "leisure"], crs),
        "med": src.query_polygon("medical", "amenity IN ('hospital','clinic','pharmacy','doctors')", q,
                                 ["name", "amenity"], crs),
    }
    for k, g in parts.items():
        g.to_parquet(RAW / f"{code}_{k}.parquet")
    done.write_text(json.dumps({k: len(v) for k, v in parts.items()} | {"s": round(time.time() - t, 1)}))


# ---------------------------------------------------------------- build
def _weighted_median(cell, val, w):
    df = pd.DataFrame({"c": cell, "v": val, "w": w}).sort_values(["c", "v"])
    df["cw"] = df.groupby("c").w.cumsum()
    half = df.groupby("c").w.transform("sum") / 2
    return df[df.cw >= half].groupby("c").v.first()


def encode_streets(lines_ll: list[np.ndarray], lengths, dist) -> bytes:
    """Binary street file. See web/uk.html for the reader."""
    firsts, deltas, nverts = [], [], []
    keep_len, keep_d = [], []
    for c, L, d in zip(lines_ll, lengths, dist):
        qd = np.round(c * Q).astype(np.int64)
        qd = qd[np.r_[True, np.any(np.diff(qd, axis=0) != 0, axis=1)]]   # drop repeats
        if len(qd) < 2:
            continue
        # densify any step too long for int16
        pts = [qd[0]]
        for p in qd[1:]:
            prev = pts[-1]
            step = p - prev
            k = max(1, int(np.ceil(np.abs(step).max() / 32000)))
            pts += [prev + (step * j) // k for j in range(1, k + 1)]
        pts = np.array(pts)
        while len(pts) > 1:                                     # split into <=255-vertex pieces
            piece, pts = pts[:255], pts[254:] if len(pts) > 255 else pts[:0]
            firsts.append(piece[0]); deltas.append(np.diff(piece, axis=0)); nverts.append(len(piece))
            keep_len.append(L); keep_d.append(d)
    n = len(firsts)
    D = np.vstack(deltas).astype(np.int16) if n else np.zeros((0, 2), np.int16)
    head = struct.pack("<4sHHII16x", b"G15S", 1, len(CATS), n, len(D))
    return b"".join([head,
                     np.array(firsts, np.int32).reshape(-1, 2).tobytes(),
                     np.minimum(np.round(keep_len), 65535).astype(np.uint16).tobytes(),
                     D.tobytes(),
                     np.array(nverts, np.uint8).tobytes(),
                     np.array(keep_d, np.uint8).reshape(-1, len(CATS)).tobytes()])


def build(code: str, c: gpd.GeoDataFrame) -> None:
    out = OUTC / f"{code}.json"
    if out.exists() or not (RAW / f"{code}.done").exists():
        return
    t = time.time()
    crs, shape, ring, _ = area_of(c, code)
    rd = {k: gpd.read_parquet(RAW / f"{code}_{k}.parquet") for k in ["hw", "pts", "bld", "lei", "med"]}
    for k, g in rd.items():
        for col in ["name", "leisure", "shop", "amenity", "building", "highway", "access", "foot",
                    "service", "area"]:
            if col not in g:
                g[col] = None
    ways = filter_ways(rd["hw"], "walk")
    stats = {"code": code, "tot": 0, "cov": [], "pois": {}}
    if len(ways) < 2:
        out.write_text(json.dumps(stats)); return
    net = Network(ways, largest_component=False)       # keep islands
    seg = net.seg
    L = seg.length.to_numpy()
    sb = rd["bld"][(rd["bld"].building == "school") & (rd["bld"].amenity.fillna("") != "school")]
    pois = assemble_pois(rd["pts"], [rd["bld"], rd["lei"], rd["med"]], tags=POI_TAGS, school_buildings=sb)
    D = np.empty((len(seg), len(CATS)))
    for j, cat in enumerate(CATS):
        p = pois[cat][pois[cat].within(ring)]
        stats["pois"][cat] = int(p.within(shape).sum())
        if len(p):
            dn = nearest_distance(net, p, MAX_M + 200)
            D[:, j] = np.minimum(dn[seg.u], dn[seg.v]) + L / 2
        else:
            D[:, j] = np.inf
    mid = seg.geometry.interpolate(0.5, normalized=True)
    res = seg.highway.isin(["residential", "living_street"]).to_numpy()
    inside = gpd.GeoSeries(mid, crs=crs).within(shape).to_numpy()
    keep = res & inside
    if not keep.any():
        out.write_text(json.dumps(stats)); return
    dist = np.minimum(np.ceil(D[keep] / 10), 255).astype(np.uint8)
    Lk = L[keep]

    # coverage by needs subset (bitmask 1..15) and minute 5..25, in metres
    di = dist.astype(np.int32)
    cov = []
    for s in range(1, 16):
        cols = [j for j in range(len(CATS)) if s >> j & 1]
        mx = di[:, cols].max(1)
        cov.append([int(Lk[mx <= 8 * m].sum()) for m in MINUTES])
    stats.update(tot=int(Lk.sum()), cov=cov)

    # overview cells: length and the typical (length-weighted median) distance per need
    to_ll = Transformer.from_crs(crs, 4326, always_xy=True)
    mx_, my_ = to_ll.transform(mid.x.to_numpy()[keep], mid.y.to_numpy()[keep])
    cells = np.array([h3.latlng_to_cell(y, x, H3_RES) for x, y in zip(mx_, my_)])
    cdf = pd.DataFrame({"cell": cells, "len": Lk})
    agg = cdf.groupby("cell").len.sum().to_frame()
    for j in range(len(CATS)):
        agg[f"m{j}"] = _weighted_median(cells, dist[:, j], Lk)
    ll = np.array([h3.cell_to_latlng(x) for x in agg.index])
    agg["lat"], agg["lon"] = ll[:, 0], ll[:, 1]
    agg.reset_index().to_parquet(OUTC / f"{code}_cells.parquet")

    # street geometry, simplified, lon/lat
    geoms = seg.geometry[keep].simplify(SIMPLIFY_M)
    lines = []
    for g in geoms:
        x, y = to_ll.transform(*np.asarray(g.coords)[:, :2].T)
        lines.append(np.column_stack([x, y]))
    (WEB / "streets").mkdir(parents=True, exist_ok=True)
    (WEB / "streets" / f"{code}.bin").write_bytes(encode_streets(lines, Lk, dist))
    stats["n"] = int(keep.sum())
    stats["s"] = round(time.time() - t, 1)
    out.write_text(json.dumps(stats))


# ---------------------------------------------------------------- merge
def merge(c: gpd.GeoDataFrame) -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    nations = {"E": "England", "W": "Wales", "S": "Scotland", "N": "Northern Ireland"}
    rows, cells = [], []
    for i, r in c.iterrows():
        f = OUTC / f"{r.LAD25CD}.json"
        if not f.exists():
            continue
        s = json.loads(f.read_text())
        b = r.geometry.bounds
        rows.append(dict(code=r.LAD25CD, name=r.LAD25NM, nation=nations[r.LAD25CD[0]],
                         bbox=[round(v, 4) for v in b], tot=s["tot"] // 10,
                         cov=[[v // 10 for v in row] for row in s["cov"]], pois=s.get("pois", {}),
                         has=bool(s.get("n"))))
        cf = OUTC / f"{r.LAD25CD}_cells.parquet"
        if cf.exists():
            cells.append(pd.read_parquet(cf).assign(ci=len(rows) - 1))
    (WEB / "councils.json").write_text(json.dumps(dict(
        cats=[dict(key=k, label=l) for k, l in zip(CATS, ["Food shop", "GP or pharmacy", "School", "Gym"])],
        minutes=MINUTES, unit_m=10, councils=rows,
        built=pd.Timestamp.now().strftime("%d %B %Y")), separators=(",", ":")))
    # outlines for the map, lightly simplified
    g = c[c.LAD25CD.isin([r["code"] for r in rows])].copy()
    g["geometry"] = g.geometry.simplify(0.002, preserve_topology=True)
    g[["LAD25CD", "LAD25NM", "geometry"]].rename(columns={"LAD25CD": "code", "LAD25NM": "name"}) \
        .to_file(WEB / "councils.geojson", driver="GeoJSON", COORDINATE_PRECISION=4)
    if cells:
        a = pd.concat(cells, ignore_index=True)
        # a cell straddling two councils appears twice; keep the part with more street
        a = a.sort_values("len", ascending=False).drop_duplicates("cell")
        head = struct.pack("<4sHHI20x", b"G15C", 1, len(CATS), len(a))
        body = [np.column_stack([a.lon, a.lat]).astype(np.float32).tobytes(),
                np.minimum(a.len.to_numpy(), 65535).astype(np.uint16).tobytes(),
                a.ci.to_numpy().astype(np.uint16).tobytes(),
                a[[f"m{j}" for j in range(len(CATS))]].to_numpy().astype(np.uint8).tobytes()]
        (WEB / "cells.bin").write_bytes(head + b"".join(body))
    p = src.cached("uk_places", src.ipn_localities).drop_duplicates(["place20nm", "lad20cd"])
    (WEB / "places.json").write_text(json.dumps(
        [[n, round(x, 4), round(y, 4)] for n, x, y in zip(p.place20nm, p.long, p.lat)],
        separators=(",", ":"), ensure_ascii=False))
    print(f"merged {len(rows)} councils, {sum(len(x) for x in cells) if cells else 0:,} cells")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("steps", nargs="+", choices=["fetch", "build", "merge"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--wait", action="store_true", help="build: wait for fetch to catch up")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    for d in (RAW, OUTC, WEB):
        d.mkdir(parents=True, exist_ok=True)
    c = councils()
    codes = a.only or list(c.LAD25CD)
    for step in a.steps:
        if step == "merge":
            merge(c); continue
        pending = list(codes)
        # memory grows over hundreds of councils in one process, so build in fresh 40-council batches
        if step == "build" and not a.child and not a.wait and len(pending) > 40:
            for i in range(0, len(pending), 40):
                subprocess.run([sys.executable, "-m", "pipelines.uk", "build", "--child", "--only",
                                *pending[i:i + 40]], check=False)
            continue
        while pending:
            nxt = []
            for code in pending:
                if step == "build" and not (RAW / f"{code}.done").exists():
                    nxt.append(code); continue
                t = time.time()
                try:
                    (fetch if step == "fetch" else build)(code, c)
                    gc.collect()
                    print(f"{step} {code} {time.time() - t:6.1f}s", flush=True)
                except Exception:  # noqa: BLE001
                    print(f"{step} {code} FAILED\n{traceback.format_exc()}", flush=True)
            if not (step == "build" and a.wait and nxt):
                break
            pending = nxt
            time.sleep(20)


if __name__ == "__main__":
    sys.exit(main())
