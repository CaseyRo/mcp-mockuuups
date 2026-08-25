"""Local catalog search — the upstream API has none, so this carries the weight."""

from mcp_mockuuups import catalog


def test_exact_placement_slug_wins(sample_catalog):
    hits = catalog.search(sample_catalog, query="ipad-air")
    assert {m["id"] for m in hits} == {"m-ipad", "m-duo"}


def test_device_word_aliases_map_to_families(sample_catalog):
    # "tablet" appears in no title or tag; only the alias table can find it.
    tablet = catalog.search(sample_catalog, query="tablet", limit=1)
    assert tablet[0]["id"] in {"m-ipad", "m-duo"}
    assert catalog.search(sample_catalog, query="television")[0]["id"] == "m-tv"
    assert catalog.search(sample_catalog, query="poster")[0]["id"] == "m-poster"


def test_title_match_outranks_incidental_match(sample_catalog):
    hits = catalog.search(sample_catalog, query="living room")
    assert hits[0]["id"] in {"m-ipad", "m-tv"}


def test_family_and_kind_filters(sample_catalog):
    assert {m["id"] for m in catalog.search(sample_catalog, family="TV")} == {"m-tv"}
    assert {m["id"] for m in catalog.search(sample_catalog, kind="print")} == {
        "m-poster"
    }


def test_tag_filter_returns_whole_shoot(sample_catalog):
    hits = catalog.search(sample_catalog, tag="shoot-a")
    assert {m["id"] for m in hits} == {"m-ipad", "m-tv", "m-mbp"}


def test_no_match_returns_empty_not_everything(sample_catalog):
    assert catalog.search(sample_catalog, query="zzzznotathing") == []


def test_limit_is_honoured(sample_catalog):
    assert len(catalog.search(sample_catalog, query="mockup", limit=2)) == 2


def test_facets_list_families_and_types(sample_catalog):
    face = catalog.facets(sample_catalog)
    assert "iPad" in face["families"] and "TV" in face["families"]
    assert face["types"] == ["digital", "print"]
