"""
Build the Glasgow City Region viewer (v0.1) and write web/glasgow.html.

Data model (scales to per-council chunks for the UK):
  one row per residential street segment
    hex      H3 res-9 cell of the segment midpoint (index into the hex table)
    len      segment length, metres (uint16)
    area     council index (uint8)
    d_<cat>  walking distance from the midpoint to the nearest POI, 10 m units,
             rounded up, 255 = further than 2,540 m (uint8)
The browser recomputes coverage for any walk time and any mix of categories.

    python -m pipelines.build_web
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
from pyproj import Transformer

from fifteen import sources as src
from fifteen.core import Network, filter_ways, nearest_distance
from pipelines.glasgow_region import LAYER_COLOURS, REGION_COUNCILS, load_data, build_pois

ROOT = Path(__file__).resolve().parents[1]
MAX_M = 2540
H3_RES = 9
CATS = ["daily_needs", "health_services", "education", "active_life"]   # display order
LABELS = {"daily_needs": "Food shop", "health_services": "GP or pharmacy",
          "education": "School", "active_life": "Gym"}
# Principal settlements, labelled first at region scale; everything else ranks by street length.
TOWNS = ["Glasgow", "Paisley", "East Kilbride", "Hamilton", "Motherwell", "Cumbernauld", "Coatbridge",
         "Airdrie", "Greenock", "Clydebank", "Wishaw", "Dumbarton", "Kirkintilloch", "Newton Mearns",
         "Rutherglen", "Bearsden", "Lanark", "Barrhead", "Johnstone", "Carluke", "Larkhall", "Bellshill",
         "Port Glasgow", "Gourock", "Erskine", "Renfrew", "Bishopbriggs", "Cambuslang", "Milngavie",
         "Kilsyth", "Strathaven", "Biggar", "Shotts", "Alexandria", "Blantyre", "Uddingston", "Lesmahagow"]


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def main():
    councils, ways, (pts, bld, lei, med), water = load_data()
    councils = councils.sort_values("LAD25NM").reset_index(drop=True)
    ways = filter_ways(ways, "walk")
    pois_all = build_pois(pts, bld, lei, med, milan=False)
    area = councils.union_all()
    ring = area.buffer(2000)
    net = Network(ways[ways.intersects(ring)])
    seg = net.seg
    L = seg.length.to_numpy()

    d = {}
    for c in CATS:
        p = pois_all[c][pois_all[c].within(ring)]
        dn = nearest_distance(net, p, MAX_M + 200)
        d[c] = np.minimum(dn[seg.u], dn[seg.v]) + L / 2          # midpoint rule, as glasgow_region.py

    mid = gpd.GeoDataFrame(geometry=seg.geometry.interpolate(0.5, normalized=True), crs=27700)
    lab = gpd.sjoin(mid, councils[["LAD25NM", "geometry"]], predicate="within", how="left")
    lab = lab[~lab.index.duplicated()].reindex(seg.index)
    keep = seg.highway.isin(["residential", "living_street"]).to_numpy() & lab.index_right.notna().to_numpy()

    ll = mid.to_crs(4326).geometry[keep]
    cells = np.array([h3.latlng_to_cell(y, x, H3_RES) for x, y in zip(ll.x, ll.y)])
    uniq, hex_idx = np.unique(cells, return_inverse=True)
    assert len(uniq) < 65535

    to_bng = Transformer.from_crs(4326, 27700, always_xy=True)
    x0, y0 = np.floor(councils.total_bounds[:2] / 1000) * 1000 - 2000

    def q(xs, ys):  # 2 m grid from the study-area origin, uint16
        return np.round((np.asarray(xs) - x0) / 2), np.round((np.asarray(ys) - y0) / 2)

    hv = np.zeros((len(uniq), 12), np.uint16)
    hc = np.zeros((len(uniq), 2), np.uint16)
    for i, c in enumerate(uniq):
        b = h3.cell_to_boundary(c)                     # (lat, lng) pairs
        xs, ys = to_bng.transform([p[1] for p in b], [p[0] for p in b])
        qx, qy = q(xs, ys)
        hv[i, 0::2], hv[i, 1::2] = qx[:6], qy[:6]
        cy, cx = h3.cell_to_latlng(c)
        X, Y = to_bng.transform(cx, cy)
        hc[i] = np.array(q([X], [Y])).ravel()

    dist = np.stack([np.minimum(np.ceil(d[c][keep] / 10), 255) for c in CATS], 1).astype(np.uint8)
    lengths = np.round(L[keep]).astype(np.uint16)
    area_idx = lab.index_right.to_numpy()[keep].astype(np.uint8)
    # index_right points at councils' rows (sorted by name above)

    # ---- check against the published CSV rule (1,200 m, all four)
    ok = (dist.astype(np.int32) * 10 <= 1200).all(1)
    check = {councils.LAD25NM[i]: float(round(100 * lengths[(area_idx == i) & ok].sum() /
                                        lengths[area_idx == i].sum(), 1)) for i in range(len(councils))}
    check["Glasgow City Region"] = round(100 * lengths[ok].sum() / lengths.sum(), 1)
    print("all-four @1200 m from exported arrays:", check)

    # ---- outlines: councils and larger water bodies, simplified
    def rings(geom, tol):
        g = geom.simplify(tol)
        polys = getattr(g, "geoms", [g])
        out = []
        for p in polys:
            for r in [p.exterior, *p.interiors]:
                qx, qy = q(*r.xy)
                out.append(np.stack([qx, qy], 1).astype(np.int32).ravel().tolist())
        return out

    council_rings = [rings(g, 40) for g in councils.geometry]
    w = water[water.intersects(ring)].copy()
    w["geometry"] = w.geometry.intersection(ring)
    w = w[w.area > 40_000]
    water_rings = [r for g in w.geometry for r in rings(g, 20) if len(r) >= 6]

    # ---- place labels: rank IPN localities by residential street km within 1.5 km
    places = src.cached("places_ipn", lambda: src.places_ipn(REGION_COUNCILS))
    from scipy.spatial import cKDTree
    mxy = np.column_stack([mid.geometry.x[keep], mid.geometry.y[keep]])
    t = cKDTree(mxy)
    places = places[places.within(area)].copy()
    pxy = np.column_stack([places.geometry.x, places.geometry.y])
    places["w"] = [lengths[ix].sum() / 1000 for ix in t.query_ball_point(pxy, 1500)]
    places["loc"] = places.descnm == "LOC"
    towns = (places[places.place20nm.isin(TOWNS)].sort_values(["loc", "w"], ascending=False)
             .drop_duplicates("place20nm"))
    towns["w"] = 1000 + (len(TOWNS) - towns.place20nm.map(TOWNS.index))   # keeps list order
    rest = places[places["loc"] & ~places.place20nm.isin(TOWNS)].sort_values("w", ascending=False) \
        .drop_duplicates("place20nm")
    places = pd.concat([towns, rest[rest.w > 3]]).sort_values("w", ascending=False)
    missing = sorted(set(TOWNS) - set(towns.place20nm))
    if missing:
        print("towns without an IPN point:", missing)
    px, py = q(places.geometry.x, places.geometry.y)
    place_list = [[n, int(a), int(b), round(float(k), 1)] for n, a, b, k in
                  zip(places.place20nm, px, py, places.w)]

    data = dict(
        title="Glasgow City Region",
        origin=[float(x0), float(y0)], unit=2, h3res=H3_RES, maxM=MAX_M,
        cats=[dict(key=c, label=LABELS[c], colour=LAYER_COLOURS[c],
                   pois=int(pois_all[c].within(area).sum())) for c in CATS],
        areas=[dict(name=n, rings=r) for n, r in zip(councils.LAD25NM, council_rings)],
        water=water_rings, places=place_list,
        n=int(keep.sum()), nh=len(uniq),
        seg=dict(hex=b64(hex_idx.astype(np.uint16)), len=b64(lengths), area=b64(area_idx),
                 dist=b64(dist)),
        hex=dict(v=b64(hv), c=b64(hc)),
        built=pd.Timestamp.now().strftime("%d %B %Y"),
    )
    (ROOT / "web").mkdir(exist_ok=True)
    js = json.dumps(data, separators=(",", ":"))
    tpl = (ROOT / "web" / "template.html").read_text()
    (ROOT / "web" / "glasgow.html").write_text(tpl.replace("/*__DATA__*/null", js))
    print(f"{data['n']:,} residential segments, {data['nh']:,} hexes, "
          f"{len(js) / 1e6:.2f} MB of data, {len(place_list)} place labels")


if __name__ == "__main__":
    main()
