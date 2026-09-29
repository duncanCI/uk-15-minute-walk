# Roadmap

v0.2 covers every UK council. It reads OpenStreetMap through Esri's hosted mirror, one council at a time, and took about 80 minutes end to end on one core. The street files total 59 MB across 361 councils, well inside GitHub Pages' 1 GB site limit.

## Next

| Version | Scope |
|---|---|
| 0.3 | Weight by residents instead of street length |
| 0.4 | Read Geofabrik extracts instead of the mirror, which adds school grounds and makes the build reproducible in GitHub Actions |
| 0.5 | Monthly scheduled refresh, and Northern Ireland place names for search |

## Decisions needed

1. **Population source.** Census output areas are precise but come from three agencies on three geographies: ONS for England and Wales (2021), NRS for Scotland (2022) and NISRA for Northern Ireland (2021). A 100 m grid such as the JRC's GHSL covers the whole UK in one file, at the cost of local precision.
2. **The needs list.** Public transport stops, parks and post offices are common additions. Each adds a byte per street segment.
3. **Imagery.** Esri World Imagery shows individual streets but expects an ArcGIS account for production use. EOX's Sentinel-2 cloudless mosaic is openly licensed for non-commercial use, but at 10 m it cannot show individual streets.

## Known build issues

- A long build process grows in memory over hundreds of councils. `pipelines/uk.py build` now runs in fresh 40-council batches.
- The mirror is a free public service. Keep fetches to two parallel processes.
