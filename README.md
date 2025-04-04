# osm-boundary-feed

Generates feeds of OSM changes within defined boundaries

## Getting Started

1. Ensure you have [uv](https://github.com/astral-sh/uv) and [osmium](https://osmcode.org/osmium-tool/) installed.
1. Create `./regions/upstream.json` with `{"urls": [...]}` using [a Planet.osm extract](https://wiki.openstreetmap.org/wiki/Planet.osm#Extracts) for your region.
1. Create `./regions/neighbourhood/boundary.geojson` with [the geojson of the area that you want to watch](https://geojson.io/).
1. Create `./regions/neighbourhood/feed.json` with `{"title": "My Neighbourhood"}` to give a name to the resultant feed.
1. Run `uv run osm-boundary-feed run`.
1. Upload your resultant `./feeds/neighbourhood.atom` to somewhere your feed reader can access (example output: https://bradbeattie.com/osm/ubc.atom)
