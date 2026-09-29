"""
Core pieces for the Glasgow 15-minute city pipeline.

- build_network():   OSM ways -> noded, routable street graph (osmnx-style filters)
- assemble_pois():   OSM nodes + polygon centroids per category, de-duplicated
- waxman_network():  city2graph.waxman_graph(distance_metric="network") re-implemented
                     on scipy's C Dijkstra. Same formula, same RNG stream, same snapping,
                     so a given seed gives the same edges (checked by pipelines/validate.py).
- coverage():        multi-source walking distance from every street node to the
                     nearest POI of each category.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra, connected_components
from scipy.spatial import cKDTree
import shapely


def linestrings(coord_list):
    """Ragged list of (k, 2) arrays -> array of LineStrings."""
    if not len(coord_list):
        return []
    lens = np.array([len(c) for c in coord_list])
    return shapely.linestrings(np.vstack(coord_list), indices=np.repeat(np.arange(len(lens)), lens))

# ------------------------------------------------------------------ network
WALK_EXCLUDE = {"abandoned", "bus_guideway", "construction", "motorway", "motorway_link", "no",
                "planned", "platform", "proposed", "raceway", "razed", "rest_area", "services"}
DRIVE_EXCLUDE = {"abandoned", "bridleway", "bus_guideway", "construction", "corridor", "cycleway",
                 "elevator", "escalator", "footway", "no", "path", "pedestrian", "planned",
                 "platform", "proposed", "raceway", "razed", "rest_area", "service", "services",
                 "steps", "track"}


def filter_ways(h: gpd.GeoDataFrame, network_type: str = "walk") -> gpd.GeoDataFrame:
    """osmnx 2.x network_type filters applied to OSM ways.

    One Scottish change for walk: osmnx drops every highway=cycleway. Under the
    Land Reform (Scotland) Act 2003 people may walk on shared paths, so cycleways
    stay unless tagged foot=no.
    """
    s = lambda c: h[c].fillna("").astype(str)  # noqa: E731
    keep = (s("area") != "yes") & (s("access") != "private")
    if network_type == "walk":
        keep &= ~s("highway").isin(WALK_EXCLUDE) & (s("foot") != "no") & (s("service") != "private")
    elif network_type == "drive":
        keep &= ~s("highway").isin(DRIVE_EXCLUDE)
        keep &= (s("motor_vehicle") != "no") & (s("motorcar") != "no")
        keep &= ~s("service").isin({"alley", "driveway", "emergency_access", "parking",
                                    "parking_aisle", "private"})
    else:
        raise ValueError(network_type)
    return h[keep].reset_index(drop=True)


class Network:
    """Noded street graph. Nodes = way ends + vertices shared by 2+ ways (OSM junctions)."""

    def __init__(self, ways: gpd.GeoDataFrame, largest_component: bool = True):
        coords = [np.asarray(g.coords)[:, :2] for g in ways.geometry]
        lens = np.array([len(c) for c in coords])
        allc = np.vstack(coords)
        way_of = np.repeat(np.arange(len(coords)), lens)
        q = np.round(allc * 100).astype(np.int64)            # 1 cm grid for vertex matching
        key = q[:, 0] * 100_000_000 + q[:, 1]
        uniq, first_idx, inv, cnt = np.unique(key, return_index=True, return_inverse=True,
                                              return_counts=True)
        starts = np.r_[0, np.cumsum(lens)[:-1]]
        ends = starts + lens - 1
        is_node = cnt[inv] >= 2
        is_node[starts] = True
        is_node[ends] = True

        seg_u, seg_v, seg_len, seg_way, seg_coords = [], [], [], [], []
        step = np.r_[np.hypot(*np.diff(allc, axis=0).T), 0.0]
        for w, (a, b) in enumerate(zip(starts, ends)):
            idx = a + np.flatnonzero(is_node[a:b + 1])
            for i0, i1 in zip(idx[:-1], idx[1:]):
                u, v = inv[i0], inv[i1]
                if u == v:
                    continue
                seg_u.append(u); seg_v.append(v); seg_way.append(w)
                seg_len.append(step[i0:i1].sum())
                c = allc[i0:i1 + 1].copy()
                c[0], c[-1] = allc[first_idx[u]], allc[first_idx[v]]  # one canonical xy per junction
                seg_coords.append(c)
        seg_u, seg_v = np.array(seg_u), np.array(seg_v)
        # compact node ids
        used, remap = np.unique(np.r_[seg_u, seg_v], return_inverse=True)
        u, v = remap[:len(seg_u)], remap[len(seg_u):]
        xy = allc[first_idx[used]]

        seg = gpd.GeoDataFrame(
            {"u": u, "v": v, "length": np.array(seg_len),
             "highway": ways["highway"].to_numpy()[seg_way]},
            geometry=linestrings(seg_coords), crs=ways.crs)

        n = len(xy)
        # parallel segments between the same two junctions: keep the shortest
        A = self._min_duplicates(np.r_[u, v], np.r_[v, u], np.r_[seg.length, seg.length], n)

        if largest_component:  # osmnx default (retain_all=False)
            _, lab = connected_components(A, directed=False)
            big = np.bincount(lab).argmax()
            keep_nodes = np.flatnonzero(lab == big)
            new_id = -np.ones(n, dtype=np.int64); new_id[keep_nodes] = np.arange(len(keep_nodes))
            m = (lab[seg.u] == big) & (lab[seg.v] == big)
            seg = seg[m].copy()
            seg["u"], seg["v"] = new_id[seg.u], new_id[seg.v]
            xy = xy[keep_nodes]
            n = len(xy)
            A = self._min_duplicates(np.r_[seg.u, seg.v], np.r_[seg.v, seg.u],
                                     np.r_[seg.length, seg.length], n)
        self.xy, self.A, self.seg = xy, A, seg.reset_index(drop=True)
        self.tree = cKDTree(xy)

    @staticmethod
    def _min_duplicates(r, c, w, n):
        df = pd.DataFrame({"r": r, "c": c, "w": w}).groupby(["r", "c"], as_index=False).w.min()
        return coo_matrix((df.w, (df.r, df.c)), shape=(n, n)).tocsr()

    def snap(self, pts_xy):
        return self.tree.query(pts_xy)[1]

    @property
    def n_nodes(self):
        return len(self.xy)


# ------------------------------------------------------------------ POIs
POI_TAGS = {  # Milan Janosov's four layers, with UK OSM tagging added (see README)
    "active_life": {"leisure": ["gym", "sports_centre", "fitness_centre"]},
    "daily_needs": {"shop": ["supermarket", "convenience"]},
    "education": {"amenity": ["school"]},
    "health_services": {"amenity": ["hospital", "clinic", "pharmacy", "doctors"]},
}
MILAN_TAGS = {
    "active_life": {"leisure": ["gym", "sports_centre"]},
    "daily_needs": {"shop": ["supermarket", "convenience"]},
    "education": {"amenity": ["school"]},
    "health_services": {"amenity": ["hospital", "clinic", "pharmacy"]},
}


def _match(df, tags):
    m = np.zeros(len(df), bool)
    for k, vals in tags.items():
        if k in df:
            m |= df[k].isin(vals).to_numpy()
    return m


def assemble_pois(points, polygons: list[gpd.GeoDataFrame], tags=POI_TAGS,
                  include_polygons=True, school_buildings=None, school_merge_m=150):
    """Return {category: GeoDataFrame of POI points}.

    points   : OSM nodes (what Milan's `geometry.type == "Point"` filter keeps)
    polygons : OSM areas; used as centroids unless a same-category node sits inside
    school_buildings : building=school footprints, merged into sites within
               `school_merge_m` metres, because the mirror lacks amenity=school site areas.
    """
    out = {}
    for cat, t in tags.items():
        p = points[_match(points, t)][["name", "geometry"]].assign(src="node")
        parts = [p]
        if include_polygons:
            for poly in polygons:
                a = poly[_match(poly, t)]
                if len(a):
                    inside = gpd.sjoin(a[["geometry"]], p[["geometry"]], predicate="contains").index
                    a = a.drop(index=inside.unique())
                    parts.append(gpd.GeoDataFrame({"name": a.get("name"), "src": "area"},
                                                  geometry=a.geometry.centroid, crs=a.crs))
            if cat == "education" and school_buildings is not None and len(school_buildings):
                parts.append(gpd.GeoDataFrame({"name": school_buildings.get("name"), "src": "building"},
                                              geometry=school_buildings.geometry.centroid,
                                              crs=school_buildings.crs))
        g = pd.concat(parts, ignore_index=True)
        g = gpd.GeoDataFrame(g, geometry="geometry", crs=points.crs)
        merge = school_merge_m if cat == "education" else 25
        g = _merge_close(g, merge)
        out[cat] = g.reset_index(drop=True)
    return out


def _merge_close(g, d):
    """Collapse points closer than d metres (one site mapped as node + area, or many school blocks)."""
    if len(g) < 2:
        return g
    xy = np.column_stack([g.geometry.x, g.geometry.y])
    pairs = cKDTree(xy).query_pairs(d, output_type="ndarray")
    n = len(g)
    A = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) \
        else coo_matrix((n, n))
    _, lab = connected_components(A, directed=False)
    prio = g["src"].map({"node": 0, "area": 1, "building": 2}).to_numpy()
    df = pd.DataFrame({"lab": lab, "prio": prio, "x": xy[:, 0], "y": xy[:, 1]})
    keep = df.sort_values(["lab", "prio"]).groupby("lab").head(1).index
    c = df.groupby("lab")[["x", "y"]].mean()
    res = g.loc[keep].copy()
    res["geometry"] = gpd.points_from_xy(c.loc[lab[keep], "x"], c.loc[lab[keep], "y"], crs=g.crs)
    return res


# ------------------------------------------------------------------ Waxman
def _network_rows(net, src_nodes, limit, want_pred=False, chunk=48):
    """Yield (source_node, distances[, predecessors]) using scipy's Dijkstra in chunks."""
    uniq = np.unique(src_nodes)
    for k in range(0, len(uniq), chunk):
        sub = uniq[k:k + chunk]
        res = dijkstra(net.A, directed=False, indices=sub, limit=limit,
                       return_predecessors=want_pred)
        if want_pred:
            D, P = res
            for i, s in enumerate(sub):
                yield s, D[i], P[i]
        else:
            for i, s in enumerate(sub):
                yield s, res[i]


def waxman_network(pois: gpd.GeoDataFrame, net: Network, beta=0.5, r0=1000.0, seed=None,
                   p_floor=1e-5):
    """city2graph.waxman_graph(..., distance_metric="network") on scipy.

    P(u,v) = beta * exp(-d_network(u,v) / r0). Every pair is drawn once from
    numpy.random.default_rng(seed) in city2graph's order.

    Speed-up: Dijkstra stops at d_max = r0 * ln(beta / p_floor). Pairs further
    apart get P = 0 instead of P < p_floor. `lost_edges_bound` reports an upper
    bound on the expected number of edges this removes.
    """
    n = len(pois)
    xy = np.column_stack([pois.geometry.centroid.x, pois.geometry.centroid.y])
    snapped = net.snap(xy)
    d_max = r0 * np.log(beta / p_floor) if p_floor else np.inf
    dm = np.full((n, n), np.inf)
    np.fill_diagonal(dm, 0)
    rows_of = pd.Series(np.arange(n)).groupby(snapped).apply(list).to_dict()
    for s, D in _network_rows(net, snapped, d_max):
        dm[rows_of[s], :] = D[snapped]
    dm = np.minimum(dm, dm.T)  # symmetric, as in city2graph
    np.fill_diagonal(dm, 0)

    with np.errstate(divide="ignore"):
        probs = beta * np.exp(-dm / r0)
    probs[dm == np.inf] = 0
    rng = np.random.default_rng(seed)
    rand = rng.random(dm.shape)
    mask = (rand <= probs) & np.triu(np.ones_like(dm, dtype=bool), 1)
    ei, ej = np.where(mask)

    # bound on what the cut-off removed: truncated pairs had d_net > d_max and d_net >= d_euclid
    trunc = np.triu(np.isinf(dm), 1)
    if trunc.any():
        de = np.hypot(*(xy[:, None, :] - xy[None, :, :]).transpose(2, 0, 1))
        lost = float((beta * np.exp(-np.maximum(de[trunc], d_max) / r0)).sum())
    else:
        lost = 0.0

    # trace edge geometries along the network (node positions, like city2graph)
    geoms = [None] * len(ei)
    by_src = pd.Series(np.arange(len(ei))).groupby(snapped[ei]).apply(list).to_dict()
    for s, D, P in _network_rows(net, np.array(list(by_src)), d_max, want_pred=True):
        for k in by_src[s]:
            t = snapped[ej[k]]
            path = [t]
            while path[-1] != s and path[-1] >= 0:
                path.append(P[path[-1]])
            if len(path) >= 2 and path[-1] == s:
                geoms[k] = net.xy[path[::-1]]
            else:
                geoms[k] = np.vstack([xy[ei[k]], xy[ej[k]]])
    edges = gpd.GeoDataFrame({"u": pois.index[ei], "v": pois.index[ej], "weight": dm[ei, ej]},
                             geometry=linestrings(geoms) if geoms else None, crs=pois.crs)
    edges.attrs.update(beta=beta, r0=r0, d_max=d_max, lost_edges_bound=lost, seed=seed)
    return edges


# ------------------------------------------------------------------ coverage
def nearest_distance(net: Network, pois: gpd.GeoDataFrame, limit: float) -> np.ndarray:
    """Walking distance from every street node to its nearest POI (inf beyond limit)."""
    src = np.unique(net.snap(np.column_stack([pois.geometry.x, pois.geometry.y])))
    return dijkstra(net.A, directed=False, indices=src, limit=limit, min_only=True)
