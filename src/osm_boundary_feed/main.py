import itertools
import json
import logging
import shutil
import subprocess
import tempfile
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import requests
import typer
from bs4 import BeautifulSoup
from dateutil.parser import parse as date_parse
from feedgen.feed import FeedGenerator
from humanfriendly import format_size, format_timespan
from markdown import markdown
from osmium import FileProcessor
from osmium.filter import KeyFilter
from pydantic import BaseModel
from requests import Session
from requests_cache import CachedSession
from rich.logging import RichHandler
from rich.progress import Progress, TransferSpeedColumn

logger = logging.getLogger(__name__)
BLACKLIST_NAME = ("name=",)
BLACKLIST_META = ("addr:", "check_date=", "check_date:", "start_date=")
EXITCODE_NO_FEEDS_GENERATED = 2


class FeedConfig(BaseModel):
    title: str
    changed_trigger: tuple[str, ...] = BLACKLIST_NAME
    changed_blacklist: tuple[str, ...] = BLACKLIST_META
    added_blacklist: tuple[str, ...] = (*BLACKLIST_NAME, *BLACKLIST_META)
    removed_blacklist: tuple[str, ...] = BLACKLIST_META


class UpstreamConfig(BaseModel):
    urls: list[str]  # See https://wiki.openstreetmap.org/wiki/Planet.osm#Extracts


cli = typer.Typer(no_args_is_help=True)


@cli.callback()
def typer_callback(*, verbose: bool = False) -> None:
    """Arguments and options common to all CLI commands."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, enable_link_path=False)],
    )


@cli.command()
def run(
    regions: Annotated[Path, typer.Option(file_okay=False, exists=True)] = Path("regions"),
    destination: Annotated[Path, typer.Option(file_okay=False)] = Path("feeds"),
) -> None:
    """Snapshot all upstream OSM regions, generate our boundaried regions, generate our feeds"""
    if not next(regions.glob("**/upstream.json"), None):
        logger.warning(f"No upstream.json regions defined in {regions}")
    if not next(regions.glob("**/boundary.geojson"), None):
        logger.warning(f"No boundary.geojson files defined in {regions}")
    if not next(regions.glob("**/feed.json"), None):
        logger.warning(f"No feed.json files defined in {regions}")
    for upstream_json in regions.glob("**/upstream.json"):
        snapshot_upstream(upstream_json)
    for boundary_geojson in sorted(regions.glob("**/boundary.geojson")):
        extract_boundary(boundary_geojson)
    for feed_json in sorted(regions.glob("**/feed.json")):
        for osm_pbf in sorted(feed_json.parent.glob("*.osm.pbf")):
            extract_jsonl(osm_pbf)
        feed_atom = destination / f"{feed_json.parent.name}.atom"
        if not feed_atom.exists() or feed_atom.stat().st_mtime < max(
            jsonl.stat().st_mtime for jsonl in feed_json.parent.glob("*.jsonl")
        ):
            generate_feed(feed_json, feed_atom)


def snapshot_upstream(upstream_json: Path) -> Path | None:
    """Downloads a snapshot of the upstream OSM data if there's a new one available"""
    logger.debug(f"Considering {upstream_json}")
    for url in UpstreamConfig.model_validate_json(upstream_json.read_text()).urls:
        try:
            with tempfile.NamedTemporaryFile() as temp:
                with requests.get(url, stream=True, timeout=10) as r:
                    # Determine if we can skip this download
                    modified = date_parse(r.headers["Last-Modified"])
                    modified_ago = datetime.now(UTC) - modified
                    logger.debug(f"{url} last updated {format_timespan(modified_ago, max_units=1)} ago")
                    snapshot = upstream_json.parent / f"{modified.date().isoformat()}.osm.pbf"
                    if snapshot.exists():
                        logger.info(f"{url} skipped as {snapshot} exists")
                        return None

                    size = int(r.headers["Content-Length"])
                    logger.info(f"{url} downloading {format_size(size, binary=True)} into {snapshot}")
                    with Progress(*Progress.get_default_columns(), TransferSpeedColumn()) as progress:
                        task_id = progress.add_task("Downloading...", total=size)
                        for chunk in r.iter_content(chunk_size=8192):
                            temp.write(chunk)
                            progress.update(task_id, advance=len(chunk))
                shutil.move(temp.name, snapshot)
            logger.info(f"{url} downloaded to {snapshot}")
            return snapshot
        except requests.RequestException:
            logger.warning(f"Failed to download {url}")
    logger.warning(f"Failed to download {upstream_json}")
    return None


def extract_boundary(boundary_geojson: Path) -> None:
    """Extracts a subset of the parent directory's OSM data within the boundary"""
    for snapshot in sorted(boundary_geojson.parent.parent.glob("*.osm.pbf")):
        if not (extracted := boundary_geojson.parent / snapshot.name).exists():
            logger.info(f"Extracting {snapshot} to {extracted}")
            with tempfile.NamedTemporaryFile() as temp:
                subprocess.run(
                    args=[
                        "/usr/bin/osmium",
                        "extract",
                        "--strategy",
                        "simple",
                        "--polygon",
                        boundary_geojson.as_posix(),
                        "-o",
                        temp.name,
                        "-f",
                        "pbf",
                        "--overwrite",
                        snapshot.as_posix(),
                    ],
                    check=True,
                )
                shutil.move(temp.name, extracted)


def extract_jsonl(osm_pbf: Path) -> Path | None:
    """Extracts the JSONL files from each of the region snapshots"""
    jsonl = osm_pbf.parent / f"{osm_pbf.name.removesuffix('.osm.pbf')}.jsonl"
    if jsonl.exists():
        return None
    logger.info(f"Extracting {osm_pbf} to {jsonl}")
    try:
        with jsonl.open(mode="w") as f:
            for obj in FileProcessor(osm_pbf).with_filter(KeyFilter("name")):
                if obj.tags.get("highway") or obj.tags.get("route"):
                    continue
                f.write(
                    json.dumps(
                        {
                            "id": obj.id,
                            "type": type(obj).__name__.lower(),
                            "name": obj.tags["name"],
                            "tags": sorted([f"{t.k}={t.v}" for t in obj.tags]),
                        }
                    )
                    + "\n"
                )
        return jsonl
    except Exception:
        if jsonl.exists():
            jsonl.unlink()
        raise


def generate_feed(feed_json: Path, feed_atom: Path) -> None:
    """Compares the JSONL files in alphabetical (date) order and produces an atom feed of the diffs at each step"""

    logger.info(f"Considering {feed_json}")
    feed_config = FeedConfig.model_validate_json(feed_json.read_text())

    feedgen = FeedGenerator()
    feedgen.id(f"osm-boundary-feed-{feed_json.parent.name}")
    feedgen.title(f"OSM changes: {feed_config.title}")
    feedgen.link(href=f"https://bradbeattie.com/osm/{feed_json.parent.name}.atom", rel="self")
    if (boundary := feed_json.parent / "boundary.geojson").exists():
        encoded = urllib.parse.quote(json.dumps(json.loads(boundary.read_text())))
        feedgen.link(href=f"http://geojson.io/#data=data:application/json,{encoded}", rel="related")

    prev_nodes = None
    with CachedSession("api.cache", expire_after=timedelta(days=30), allowable_codes=[200, 410]) as session:
        for jsonl in sorted(feed_json.parent.glob("*.jsonl")):
            prev_nodes = generate_feed_item(jsonl, feed_config, feedgen, prev_nodes, session)

    feed_atom.parent.mkdir(exist_ok=True)
    logger.info(f"Generating {feed_atom}")
    feedgen.atom_file(feed_atom, pretty=True)


def generate_feed_item(
    day_jsonl: Path,
    feed_config: FeedConfig,
    feedgen: FeedGenerator,
    prev_nodes: dict | None,
    session: Session,
) -> dict[str, Any]:
    """Generates a feed item for the given JSONL file and returns the nodes for comparison with the next"""
    logger.debug(f"Considering {day_jsonl}")
    curr_nodes = {}
    for line in day_jsonl.open():
        node = json.loads(line)
        node["url"] = f"https://www.openstreetmap.org/{node['type']}/{node['id']}"
        node["tags"] = set(node["tags"])
        curr_nodes[node["id"]] = node
    if prev_nodes is not None:
        added, changed, removed = get_record_delta(curr_nodes, prev_nodes, feed_config)
        markdown_list = get_delta_markdown_list(added, changed, removed, feed_config, session)
        if markdown_list:
            region_name = day_jsonl.parent.name
            fe = feedgen.add_entry()
            fe.id(f"{region_name}-{day_jsonl.stem}")
            fe.title(f"OSM changes for {day_jsonl.stem}")
            fe.description(markdown("\n".join(markdown_list)))
            fe.published(datetime.fromisoformat(f"{day_jsonl.stem}T00:00:00Z") + timedelta(days=1))
            logger.info(f"{day_jsonl.stem}\n{'\n'.join(markdown_list)}")
    return curr_nodes


def get_record_delta(curr_nodes: dict, prev_nodes: dict, feed_config: FeedConfig) -> tuple[dict, dict, dict]:
    """Gets the delta between the current nodes and the previous nodes"""
    added = {}
    changed = {}
    removed = {}
    for nid in curr_nodes.keys() | prev_nodes.keys():
        if nid not in curr_nodes:
            removed[nid] = prev_nodes[nid]
        elif nid not in prev_nodes:
            added[nid] = curr_nodes[nid]
        else:
            curr_tags = {t for t in curr_nodes[nid]["tags"] if t.startswith(feed_config.changed_trigger)}
            prev_tags = {t for t in prev_nodes[nid]["tags"] if t.startswith(feed_config.changed_trigger)}
            if prev_tags != curr_tags:
                changed[nid] = {
                    **curr_nodes[nid],
                    "removed": prev_nodes[nid]["tags"] - curr_nodes[nid]["tags"],
                    "added": curr_nodes[nid]["tags"] - prev_nodes[nid]["tags"],
                }
    return added, changed, removed


def get_delta_markdown_list(
    added: dict, changed: dict, removed: dict, feed_config: FeedConfig, session: Session
) -> list[str]:
    """Render the delta into a list of markdown strings"""
    records = []
    for node in sorted(added.values(), key=lambda n: n["name"]):
        new_tags = ", ".join(f"+{t}" for t in sorted(node["tags"]) if not t.startswith(feed_config.added_blacklist))
        records.append(f"  * 🟢 [{node['name']}]({node['url']}): {new_tags}")
    for node in sorted(changed.values(), key=lambda n: n["name"]):
        edited_tags = ", ".join(
            itertools.chain(
                (f"-{t}" for t in sorted(node["removed"]) if not t.startswith(feed_config.changed_blacklist)),
                (f"+{t}" for t in sorted(node["added"]) if not t.startswith(feed_config.changed_blacklist)),
            )
        )
        records.append(f"  * ✏️ [{node['name']}]({node['url']}): {edited_tags}")
    for node in sorted(removed.values(), key=lambda n: n["name"]):
        old_tags = ", ".join(f"-{t}" for t in sorted(node["tags"]) if not t.startswith(feed_config.removed_blacklist))
        suffix = get_last_comment(node, session) or old_tags
        records.append(f"  * ❌ [{node['name']}]({node['url']}): {suffix}, {old_tags}")
    return records


def get_last_comment(node: dict, existing_session: Session | None = None) -> str | None:
    """Queries the OSM API to get the comment from the last changeset"""
    with existing_session or Session() as session:
        node_url = f"https://www.openstreetmap.org/api/0.6/{node['type']}/{node['id']}/history"
        node_resp = session.get(node_url)
        node_soup = BeautifulSoup(node_resp.text, "xml")
        last_changeset = node_soup.select("[changeset]")[-1].attrs["changeset"]
        changeset_url = f"https://www.openstreetmap.org/api/0.6/changeset/{last_changeset}"
        changeset_soup = BeautifulSoup(session.get(changeset_url).text, "xml")
        for comment in changeset_soup.select("[k=comment]"):
            return str(comment.attrs["v"])
        return None


if __name__ == "__main__":
    cli()
