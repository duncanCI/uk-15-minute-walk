"""Check waxman_network() against city2graph.waxman_graph() on Glasgow City.

    python -m pipelines.validate daily_needs
"""
import time, sys
import geopandas as gpd, numpy as np, city2graph
from fifteen.core import *
from fifteen import sources as src

cat = sys.argv[1] if len(sys.argv) > 1 else "daily_needs"
h = gpd.read_parquet(src.CACHE / "highways_raw.parquet")
reg = gpd.read_parquet(src.CACHE / "councils.parquet")
gla = reg[reg.LAD25NM == "Glasgow City"].union_all()
w = filter_ways(h, "drive"); w = w[w.intersects(gla)]
net = Network(w)
pts = gpd.read_parquet(src.CACHE / "poi_points.parquet")
bld = gpd.read_parquet(src.CACHE / "poi_buildings.parquet")
lei = gpd.read_parquet(src.CACHE / "poi_leisure.parquet"); med = gpd.read_parquet(src.CACHE / "poi_medical.parquet")
sb = bld[(bld.building == "school") & (bld.amenity.fillna("") != "school")]
pois = assemble_pois(pts, [bld, lei, med], school_buildings=sb)[cat]
pois = pois[pois.within(gla)].reset_index(drop=True)
print(cat, len(pois), "POIs;", net.n_nodes, "network nodes")

# nx.Graph keeps the last of two parallel segments; hand it the shortest so both engines see one graph
k = np.minimum(net.seg.u, net.seg.v).astype(str) + "-" + np.maximum(net.seg.u, net.seg.v).astype(str)
dedup = net.seg.assign(k=k).sort_values("length").drop_duplicates("k")[["geometry"]]
t = time.time()
_, e_ref = city2graph.waxman_graph(pois, beta=0.5, r0=1000, seed=42, distance_metric="network",
                                   network_gdf=dedup)
t_ref = time.time() - t
t = time.time()
e_new = waxman_network(pois, net, beta=0.5, r0=1000, seed=42, p_floor=None)
t_new = time.time() - t
t = time.time()
e_cut = waxman_network(pois, net, beta=0.5, r0=1000, seed=42, p_floor=1e-5)
t_cut = time.time() - t

ref = {tuple(sorted(x)) for x in e_ref.index} if isinstance(e_ref.index[0], tuple) else set()
new = {tuple(sorted((a, b))) for a, b in zip(e_new.u, e_new.v)}
cut = {tuple(sorted((a, b))) for a, b in zip(e_cut.u, e_cut.v)}
print(f"city2graph: {len(ref)} edges in {t_ref:.1f}s | scipy exact: {len(new)} in {t_new:.1f}s "
      f"| scipy cut-off: {len(cut)} in {t_cut:.1f}s")
print("exact identical edge set:", ref == new, "| sym diff", len(ref ^ new))
print("cut-off vs city2graph sym diff:", len(ref ^ cut), "| lost-edge bound:", round(e_cut.attrs["lost_edges_bound"], 4))
m = e_ref.reset_index()
wref = dict(zip([tuple(sorted(x)) for x in e_ref.index], e_ref.weight))
wnew = dict(zip([tuple(sorted((a, b))) for a, b in zip(e_new.u, e_new.v)], e_new.weight))
common = ref & new
print("max |weight diff| m:", max(abs(wref[k] - wnew[k]) for k in common) if common else None)
