import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from osm_boundary_feed.main import FeedConfig, extract_jsonl, generate_feed


def test_extract_jsonl(tmp_path: Path) -> None:
    # Prep the test case
    cases = Path(__file__).parent / "cases"
    for pbf in cases.glob("*.pbf"):
        shutil.copy(pbf, tmp_path / pbf.name)
        extract_jsonl(tmp_path / pbf.name)

    # Confirm the results
    for jsonl in cases.glob("*.jsonl"):
        assert jsonl.read_text() == (tmp_path / jsonl.name).read_text()


def test_generate_feed(tmp_path) -> None:
    # Prep the test case
    cases = Path(__file__).parent / "cases"
    for jsonl in cases.glob("*.jsonl"):
        shutil.copy(jsonl, tmp_path / jsonl.name)
    feed_config = FeedConfig(title="test-generate")
    with (tmp_path / "feed.json").open(mode="w") as f:
        f.write(feed_config.model_dump_json())

    # Generate the feed
    now = datetime.now(UTC)
    generate_feed(tmp_path / "feed.json", tmp_path / "feed.atom")

    # Confirm the results
    atoms = list(tmp_path.glob("*.atom"))
    assert len(atoms) == 1
    iso_ms = re.compile(r"\.[0-9]{6}\+")
    expected_feed_content = (cases / "feed.atom").read_text().replace("NOW", now.isoformat())
    assert iso_ms.sub("", atoms[0].read_text()) == iso_ms.sub("", expected_feed_content)
