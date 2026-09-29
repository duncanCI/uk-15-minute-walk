"""
15-minute city connectivity for Glasgow and the Glasgow City Region.

Implements Milan Janosov's city2graph workflow (#30DayMapChallenge day 14,
"How Walkable Is Your City?") and adds a coverage measure.

    python -m pipelines.glasgow_region                  # walk network, UK tags (default)
    python -m pipelines.glasgow_region --network drive  # Milan's drive-network shortcut
    python -m pipelines.glasgow_region --milan-pois     # Milan's exact tags, OSM nodes only

Outputs land in ./out
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import city2graph
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from shapely.geometry import box  # noqa: E402

from fifteen import sources as src  # noqa: E402
from fifteen.core import (MILAN_TAGS, POI_TAGS, Network, assemble_pois, filter_ways,  # noqa: E402
                     nearest_distance, waxman_network)

REGION_COUNCILS = ["Glasgow City", "East Dunbartonshire", "West Dunbartonshire", "East Renfrewshire",
                   "Renfrewshire", "Inverclyde", "North Lanarkshire", "South Lanarkshire"]
AREAS = {
    # zoom="edges" is Milan's auto-zoom; "boundary" keeps the empty rural south of South Lanarkshire in view
    "glasgow_city": dict(title="Glasgow City", councils=["Glasgow City"], size=8, lw=0.6, zoom="edges"),
    "glasgow_city_region": dict(title="Glasgow City Region", councils=REGION_COUNCILS, size=11, lw=0.55,
                                zoom="boundary"),
}
BBOX = (-4.90, 55.29, -3.39, 56.09)  # lon/lat envelope around the eight councils
LAYER_COLOURS = {"active_life": "#00e5ff", "daily_needs": "#ffea00",
                 "education": "#ff4081", "health_services": "#76ff03"}
DRAW_ORDER = ["daily_needs", "health_services", "education", "active_life"]
WALK_15_MIN_M = 1200  # 15 min at 4.8 km/h
CREDIT = "© OpenStreetMap contributors (via Esri OSM mirror) · Boundaries: ONS Dec 2025, OGL v3"
OUT = Path(__file__).resolve().parents[1] / "out"


# ---------------------------------------------------------------- data
def load_data():
    c = src.cached("councils", lambda: src.councils(REGION_COUNCILS))
    ways = src.cached("highways_raw", lambda: src.query_layer(
        "highways",
        "highway NOT IN ('construction','proposed','raceway','no','abandoned','planned','razed',"
        "'platform','elevator','escape','rest_area','services','busway','bus_guideway','corridor')",
        BBOX, ["osm_id", "highway", "access", "foot", "service", "area", "motor_vehicle",
               "motorcar", "junction"], tiles=6, workers=8))
    pw = ("leisure IN ('gym','sports_centre','fitness_centre') OR shop IN ('supermarket','convenience') "
          "OR amenity IN ('school','hospital','clinic','pharmacy','doctors')")
    pts = src.cached("poi_points", lambda: src.query_layer(
        "pois", pw, BBOX, ["osm_id", "name", "leisure", "shop", "amenity"], tiles=2))
    bld = src.cached("poi_buildings", lambda: src.query_layer(
        "buildings", pw + " OR building='school'", BBOX,
        ["osm_id", "name", "leisure", "shop", "amenity", "building"], tiles=2))
    lei = src.cached("poi_leisure", lambda: src.query_layer(
        "leisure", "leisure IN ('gym','sports_centre','fitness_centre')", BBOX,
        ["osm_id2", "name", "leisure"], tiles=2))
    med = src.cached("poi_medical", lambda: src.query_layer(
        "medical", "amenity IN ('hospital','clinic','pharmacy','doctors')", BBOX,
        ["osm_id2", "name", "amenity"]))
    water = src.cached("water", lambda: src.query_layer("water", "1=1", BBOX, ["OBJECTID"], tiles=3))
    return c, ways, (pts, bld, lei, med), water


def build_pois(pts, bld, lei, med, milan: bool):
    if milan:  # Milan's final loop: his tags, `geometry.type == "Point"` only
        return assemble_pois(pts, [], tags=MILAN_TAGS, include_polygons=False)
    school_blocks = bld[(bld.building == "school") & (bld.amenity.fillna("") != "school")]
    return assemble_pois(pts, [bld, lei, med], tags=POI_TAGS, school_buildings=school_blocks)


# ---------------------------------------------------------------- drawing helpers
def _lines(ax, geoms, colour, lw, alpha, z):
    segs = [np.asarray(g.coords) for g in geoms if g is not None and not g.is_empty]
    if segs:
        ax.add_collection(LineCollection(segs, colors=colour, linewidths=lw, alpha=alpha, zorder=z,
                                         capstyle="round"))


def _dark_base(ax, net_seg, water, view):
    """Stand-in for CartoDB DarkMatterNoLabels (tiles unreachable here): OSM water + streets."""
    ax.set_facecolor("black")
    w = water.cx[view[0]:view[2], view[1]:view[3]]
    if len(w):
        w.plot(ax=ax, color="#0a1820", linewidth=0, zorder=1)
    _lines(ax, net_seg.geometry, "#262626", 0.25, 1.0, 2)


def _auto_zoom(ax, bounds_list):
    """Milan's auto-zoom: extent of all edges, 5% padding, extra room at the bottom for the legend."""
    b = np.array(bounds_list)
    minx, miny, maxx, maxy = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
    bx, by = (maxx - minx) * 0.05, (maxy - miny) * 0.05
    zx0, zx1 = minx - bx, maxx + bx
    zy0, zy1 = miny - by - (maxy - miny) * 0.1, maxy + by
    ax.set_xlim(zx0, zx1); ax.set_ylim(zy0, zy1)
    return zx0 - bx, zy0 - by, zx1 + bx, zy1 + by


def _mask(ax, area_geom, view, crs, z=4):
    gpd.GeoSeries([box(*view).difference(area_geom)], crs=crs).plot(ax=ax, color="black", zorder=z)


def _finish(fig, ax, handles, title, subtitle, path):
    leg = ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.02, 0.0), frameon=True,
                    framealpha=1, facecolor="black", edgecolor="black", fontsize=9,
                    labelcolor="white", title="  " + title,
                    title_fontproperties={"weight": "bold", "size": 11})
    leg.get_title().set_color("white")
    ax.text(0.02, 0.985, subtitle, transform=ax.transAxes, color="#9a9a9a", fontsize=8, va="top",
            zorder=10)
    ax.text(0.99, 0.005, CREDIT, transform=ax.transAxes, color="#5a5a5a", fontsize=5.5,
            ha="right", va="bottom", zorder=10)
    ax.set_axis_off()
    plt.tight_layout()
    fig.savefig(path, bbox_inches="tight", pad_inches=0, dpi=300, facecolor="black")
    plt.close(fig)


# ---------------------------------------------------------------- 1. method comparison panel
def method_panel(area_geom, pois, net, water, admin, path, radius=2500):
    """Milan's section 1: four graph models on one POI layer (he used Budapest gyms, r = 2,500 m)."""
    models = {
        "Fixed radius (Euclidean ≤ 2.5 km)": city2graph.fixed_radius_graph(pois, radius=radius)[1],
        "Waxman · Manhattan": city2graph.waxman_graph(pois, distance_metric="manhattan", r0=radius,
                                                      beta=0.5, seed=42)[1],
        "Waxman · Euclidean": city2graph.waxman_graph(pois, distance_metric="euclidean", r0=radius,
                                                      beta=0.5, seed=42)[1],
        "Waxman · walk-network distance": waxman_network(pois, net, beta=0.5, r0=radius, seed=42),
    }
    colours = ["#00FFFF", "#FF6EFF", "#00FF9F", "#FFA500"]
    fig, axs = plt.subplots(2, 2, figsize=(10, 10.6))
    fig.patch.set_facecolor("black")
    view = admin.total_bounds
    pad = (view[2] - view[0]) * 0.03
    view = (view[0] - pad, view[1] - pad, view[2] + pad, view[3] + pad)
    for ax, (name, e), col in zip(axs.flat, models.items(), colours):
        _dark_base(ax, net.seg, water, view)
        _mask(ax, area_geom, view, admin.crs)
        admin.boundary.plot(ax=ax, color="white", linewidth=0.8, alpha=0.4, zorder=5)
        _lines(ax, e.geometry, col, 0.75, 0.5, 6)
        ax.scatter(pois.geometry.x, pois.geometry.y, s=3, c="white", zorder=7, linewidths=0)
        ax.set_xlim(view[0], view[2]); ax.set_ylim(view[1], view[3]); ax.set_axis_off()
        ax.set_title(f"{name}\n{len(e):,} edges", color="white", fontsize=11, pad=6)
    fig.suptitle(f"Glasgow City · gyms and sports centres (n = {len(pois)}) · four city2graph models",
                 color="white", fontsize=12, y=0.995)
    fig.text(0.99, 0.003, CREDIT, color="#5a5a5a", fontsize=6, ha="right")
    plt.tight_layout()
    fig.savefig(path, dpi=200, facecolor="black")
    plt.close(fig)
    return {k: len(v) for k, v in models.items()}


# ---------------------------------------------------------------- 2. Milan-style connectivity map
def plot_city_connectivity(key, cfg, area_geom, admin, pois, wax, net, water, subtitle, radius):
    fig, ax = plt.subplots(figsize=(cfg["size"], cfg["size"]))
    fig.patch.set_facecolor("black")
    bounds = [e.total_bounds for e in wax.values() if len(e)] if cfg["zoom"] == "edges" \
        else [admin.total_bounds]
    view = _auto_zoom(ax, bounds)
    _dark_base(ax, net.seg, water, view)
    _mask(ax, area_geom, view, admin.crs)
    admin.boundary.plot(ax=ax, color="dimgrey", linewidth=1.2 if len(admin) == 1 else 0.6,
                        alpha=0.6, zorder=5)
    lw = cfg["lw"]
    for cat in DRAW_ORDER:
        e = wax.get(cat)
        if e is None or e.empty:
            continue
        _lines(ax, e.geometry, LAYER_COLOURS[cat], lw * 3.3, 0.05, 6)   # soft glow
        _lines(ax, e.geometry, LAYER_COLOURS[cat], lw, 0.3, 7)          # core line
    handles = [Line2D([0], [0], color=c, lw=3,
                      label=f"{k.replace('_', ' ').title()}  ({len(pois[k])} places, {len(wax[k]):,} links)")
               for k, c in LAYER_COLOURS.items()]
    _finish(fig, ax, handles, cfg["title"], subtitle,
            OUT / f"{key}_connectivity_{radius}.png")


# ---------------------------------------------------------------- 3. coverage (addition)
def coverage(net, pois_all, councils, limit=WALK_15_MIN_M):
    """Share of residential street length whose midpoint sits within `limit` walk of each category.

    Midpoint distance = min(d_u, d_v) + length / 2. One rule for every category and for
    'all four', so the all-four figure is the length where every category passes at once.
    """
    seg = net.seg
    res = seg.highway.isin(["residential", "living_street"]).to_numpy()
    L = seg.length.to_numpy()
    ok = {}
    for cat in LAYER_COLOURS:
        d = nearest_distance(net, pois_all[cat], limit)
        seg[f"d_{cat}"] = np.minimum(d[seg.u], d[seg.v]) + L / 2
        ok[cat] = (seg[f"d_{cat}"] <= limit).to_numpy()
    seg["n_cats"] = sum(ok[c].astype(int) for c in LAYER_COLOURS)
    all4 = seg["n_cats"].to_numpy() == len(LAYER_COLOURS)
    mid = gpd.GeoDataFrame(geometry=seg.geometry.interpolate(0.5, normalized=True), crs=seg.crs)
    lab = gpd.sjoin(mid, councils[["LAD25NM", "geometry"]], predicate="within", how="left")
    lab = lab[~lab.index.duplicated()]["LAD25NM"].reindex(seg.index)
    rows = []
    for name, m in [("Glasgow City Region", lab.notna().to_numpy())] + \
                   [(c, (lab == c).to_numpy()) for c in councils.LAD25NM]:
        mm = m & res
        tot = L[mm].sum()
        row = {"area": name, "residential_street_km": round(tot / 1000, 1)}
        for c in LAYER_COLOURS:
            row[f"{c}_pct"] = round(100 * L[mm & ok[c]].sum() / tot, 1)
        row["all_four_pct"] = round(100 * L[mm & all4].sum() / tot, 1)
        row["none_pct"] = round(100 * L[mm & (seg["n_cats"].to_numpy() == 0)].sum() / tot, 1)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("all_four_pct", ascending=False), seg


def plot_coverage(seg, councils, area_geom, water, title, path, size, lw):
    ramp = {0: "#3b3b3b", 1: "#5e2b7e", 2: "#c2366b", 3: "#f58b3c", 4: "#f9f871"}
    fig, ax = plt.subplots(figsize=(size, size))
    fig.patch.set_facecolor("black")
    b = councils.total_bounds
    pad = (b[3] - b[1]) * 0.05
    view = (b[0] - pad, b[1] - pad - (b[3] - b[1]) * 0.08, b[2] + pad, b[3] + pad)
    ax.set_xlim(view[0], view[2]); ax.set_ylim(view[1], view[3])
    ax.set_facecolor("black")
    w = water.cx[view[0]:view[2], view[1]:view[3]]
    if len(w):
        w.plot(ax=ax, color="#0a1820", linewidth=0, zorder=1)
    for k in range(5):
        s = seg[seg.n_cats == k]
        _lines(ax, s.geometry, ramp[k], lw * (0.6 if k == 0 else 1.0), 0.9, 3 + k)
    _mask(ax, area_geom, view, councils.crs, z=8)
    councils.boundary.plot(ax=ax, color="#8a8a8a", linewidth=0.5, alpha=0.7, zorder=9)
    handles = [Line2D([0], [0], color=ramp[k], lw=3,
                      label=("all four" if k == 4 else f"{k} of 4")) for k in range(4, -1, -1)]
    _finish(fig, ax, handles, title,
            "Street segments by number of categories within a 15-minute walk (1,200 m): "
            "shop, health, school, gym", path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="walk", choices=["walk", "drive"])
    ap.add_argument("--milan-pois", action="store_true")
    ap.add_argument("--r0", type=float, default=1000.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--areas", nargs="+", default=list(AREAS))
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    t0 = time.time()

    councils, ways, (pts, bld, lei, med), water = load_data()
    ways = filter_ways(ways, a.network)
    pois_all = build_pois(pts, bld, lei, med, a.milan_pois)
    print(f"data ready {time.time() - t0:.0f}s")

    log = []
    for key in a.areas:
        cfg = AREAS[key]
        admin = councils[councils.LAD25NM.isin(cfg["councils"])]
        area = admin.union_all()
        net = Network(ways[ways.intersects(area)])        # graph_from_place: ways inside the area
        pois = {k: v[v.within(area)].reset_index(drop=True) for k, v in pois_all.items()}
        wax = {}
        for cat, p in pois.items():                       # Milan's loop
            wax[cat] = waxman_network(p, net, beta=0.5, r0=a.r0, seed=a.seed) if len(p) > 1 \
                else gpd.GeoDataFrame(geometry=[], crs=net.seg.crs)
            log.append(dict(area=cfg["title"], category=cat, pois=len(p), edges=len(wax[cat]),
                            median_edge_m=round(float(np.median(wax[cat].weight)), 0) if len(wax[cat]) else None,
                            lost_edges_bound=round(wax[cat].attrs.get("lost_edges_bound", 0), 3)))
            print(f"  {key:20s} {cat:16s} {len(p):4d} POIs -> {len(wax[cat]):6,d} edges "
                  f"({time.time() - t0:.0f}s)")
        sub = (f"Waxman graph · {a.network}-network distance · r0 = {a.r0:,.0f} m · β = 0.5 · "
               f"seed {a.seed}")
        plot_city_connectivity(key, cfg, area, admin, pois, wax, net, water, sub, int(a.r0))
        if key == "glasgow_city":
            method_panel(area, pois["active_life"], net, water, admin,
                         OUT / "glasgow_city_method_comparison.png")
        if key == "glasgow_city_region" and a.network == "walk":
            # 2 km margin so people near a council edge can use the shop over the boundary
            ring = area.buffer(2000)
            net_cov = Network(ways[ways.intersects(ring)])
            pois_cov = {k: v[v.within(ring)] for k, v in pois_all.items()}
            stats, seg = coverage(net_cov, pois_cov, admin)
            stats.to_csv(OUT / "coverage_15min_by_council.csv", index=False)
            print(stats.to_string(index=False))
            plot_coverage(seg, admin, area, water, "Glasgow City Region · 15-minute walk",
                          OUT / "glasgow_city_region_15min_coverage.png", 11, 0.35)
            gla = councils[councils.LAD25NM == "Glasgow City"]
            gseg = seg[seg.intersects(gla.union_all())]
            plot_coverage(gseg, gla, gla.union_all(), water, "Glasgow City · 15-minute walk",
                          OUT / "glasgow_city_15min_coverage.png", 8, 0.6)
    pd.DataFrame(log).to_csv(OUT / "waxman_summary.csv", index=False)
    print(pd.DataFrame(log).to_string(index=False))
    print(f"done {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
