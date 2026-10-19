"""Tests for scripts/geocode_cafes.py — the café lat/lon cache.

Offline like every other suite here: `fetch` and `sleep` are always injected,
so nothing in this file can reach Nominatim.
"""

import json

import pytest

from helpers import REPO_ROOT

import geocode_cafes


# --------------------------------------------------------------------------
# Helpers


def payload(*locations):
    """An events payload whose rides carry the given `location` values."""
    return {"events": [{"uid": f"u{i}", "location": loc}
                       for i, loc in enumerate(locations)]}


def result(lat, lon):
    return [{"lat": str(lat), "lon": str(lon)}]


class Recorder:
    """A stub `fetch` that answers from a table and remembers every query."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for needle, answer in self.answers.items():
            if geocode_cafes.search_url(needle) == url:
                return answer
        return []

    @property
    def count(self):
        return len(self.urls)


def no_sleep(_seconds):
    """Tests never actually wait out Nominatim's one-request-a-second rule."""


def empty_cache():
    return {"points": {}, "missing": []}


# --------------------------------------------------------------------------
# Collecting the locations


def test_cafe_locations_are_distinct_and_sorted():
    payloads = [payload("B Cafe, 2 Main St, Boston, MA", "A Cafe, 1 Elm St, Boston, MA"),
                payload("B Cafe, 2 Main St, Boston, MA")]
    assert geocode_cafes.cafe_locations(payloads) == [
        "A Cafe, 1 Elm St, Boston, MA",
        "B Cafe, 2 Main St, Boston, MA",
    ]


def test_cafe_locations_skips_rides_without_an_address():
    """A link-only or hidden location has no address to geocode."""
    payloads = [{"events": [
        {"uid": "a", "location": None,
         "location_url": "https://maps.app.goo.gl/abc"},
        {"uid": "b", "location": None, "location_hidden": True},
        {"uid": "c", "location": "   "},
        {"uid": "d", "location": "Real Cafe, 1 Elm St, Boston, MA"},
    ]}]
    assert geocode_cafes.cafe_locations(payloads) == ["Real Cafe, 1 Elm St, Boston, MA"]


def test_cafe_locations_survives_junk_payloads():
    assert geocode_cafes.cafe_locations([{}, {"events": "nope"}, None,
                                         {"events": ["str", None]}]) == []


def test_cafe_locations_trims_whitespace():
    assert geocode_cafes.cafe_locations(
        [payload("  Cafe, 1 Elm St, Boston, MA  ")]) == ["Cafe, 1 Elm St, Boston, MA"]


# --------------------------------------------------------------------------
# Query variants


def test_variants_try_the_whole_string_first():
    variants = geocode_cafes.query_variants("Localito Cafe, 30 Riverside Ave, Medford, MA")
    assert variants[0] == "Localito Cafe, 30 Riverside Ave, Medford, MA"


def test_variants_drop_a_leading_cafe_name():
    """The measured win: the address alone resolves where name + address doesn't."""
    variants = geocode_cafes.query_variants("Localito Cafe, 30 Riverside Ave, Medford, MA")
    assert "30 Riverside Ave, Medford, MA" in variants


def test_variants_keep_a_leading_house_number():
    """A digit-leading first segment is the address, not a name — never drop it.

    Dropping it would ask Nominatim for a bare city and get a pin in the middle
    of it, which is worse than no pin.
    """
    variants = geocode_cafes.query_variants("597 Prospect St Apt B, New Haven, CT")
    assert "New Haven, CT" not in variants
    assert variants == ["597 Prospect St Apt B, New Haven, CT"]


def test_variants_fall_back_to_name_plus_locality():
    """What rescues a suite or a landmark buried in the middle of the address."""
    variants = geocode_cafes.query_variants(
        "City Hall Plaza, 1 City Hall Ave, Ste 500, Boston, MA")
    assert variants[-1] == "City Hall Plaza, Boston, MA"


def test_variants_keep_the_house_number_in_the_locality_fallback():
    variants = geocode_cafes.query_variants(
        "65 Concord Ave, Backyard, Somerville, MA 02143")
    assert variants == ["65 Concord Ave, Backyard, Somerville, MA 02143",
                        "65 Concord Ave, Somerville, MA 02143"]


def test_variants_are_deduped():
    variants = geocode_cafes.query_variants("Cafe, Boston, MA")
    assert variants == ["Cafe, Boston, MA"]


def test_variants_never_degrade_to_a_bare_city():
    """Under four segments there is no street address behind the name, so
    trimming would only leave a city — and a pin in the middle of Boston is
    worse than no pin."""
    assert geocode_cafes.query_variants("Some Cafe, Boston, MA") == [
        "Some Cafe, Boston, MA"]


def test_variants_of_nothing():
    assert geocode_cafes.query_variants("") == []
    assert geocode_cafes.query_variants(None) == []


def test_variants_collapse_whitespace():
    assert geocode_cafes.query_variants("A  Cafe\n, Boston") == ["A Cafe, Boston"]


# --------------------------------------------------------------------------
# The Nominatim call itself


def test_search_url_targets_nominatim():
    url = geocode_cafes.search_url("1 Elm St, Boston, MA")
    assert url.startswith("https://nominatim.openstreetmap.org/search?")
    assert "format=json" in url and "limit=1" in url


def test_user_agent_identifies_the_sync():
    """Nominatim's usage policy requires an identifying User-Agent."""
    assert geocode_cafes.USER_AGENT == "boscafebikers-sync/1.0"


def test_delay_respects_one_request_a_second():
    assert geocode_cafes.DELAY >= 1.0


def test_injectables_are_resolved_at_call_time(monkeypatch):
    """Bound defaults would let a monkeypatched test reach the real Nominatim."""
    fetch = Recorder({"Cafe, 1 Elm St, Boston, MA": result(42.1, -71.2)})
    monkeypatch.setattr(geocode_cafes, "fetch_json", fetch)
    monkeypatch.setattr(geocode_cafes.time, "sleep", no_sleep)
    assert geocode_cafes.geocode("Cafe, 1 Elm St, Boston, MA") == ([42.1, -71.2], True)
    assert fetch.count == 1


def test_first_point_rounds():
    assert geocode_cafes.first_point(result("42.36685221", "-71.18690601")) == [
        42.366852, -71.186906]


@pytest.mark.parametrize("results", [[], None, "nope", [{}], [{"lat": "x", "lon": "y"}],
                                     ["string"], [{"lat": "1"}]])
def test_first_point_of_garbage(results):
    assert geocode_cafes.first_point(results) == []


def test_geocode_stops_at_the_first_hit():
    fetch = Recorder({"Cafe, 1 Elm St, Boston, MA": result(42.1, -71.2)})
    point, resolved = geocode_cafes.geocode(
        "Cafe, 1 Elm St, Boston, MA", fetch=fetch, sleep=no_sleep)
    assert (point, resolved) == ([42.1, -71.2], True)
    assert fetch.count == 1


def test_geocode_falls_back_to_the_address():
    fetch = Recorder({"1 Elm St, Boston, MA": result(42.1, -71.2)})
    point, resolved = geocode_cafes.geocode(
        "Cafe, 1 Elm St, Boston, MA", fetch=fetch, sleep=no_sleep)
    assert (point, resolved) == ([42.1, -71.2], True)
    assert fetch.count == 2


def test_geocode_sleeps_between_its_own_retries():
    waits = []
    fetch = Recorder({"1 Elm St, Boston, MA": result(42.1, -71.2)})
    geocode_cafes.geocode("Cafe, 1 Elm St, Boston, MA", fetch=fetch, sleep=waits.append)
    assert waits == [geocode_cafes.DELAY]


def test_geocode_reports_a_real_miss_as_resolved():
    fetch = Recorder()
    assert geocode_cafes.geocode("Nowhere, 1 Elm St, Boston, MA",
                                 fetch=fetch, sleep=no_sleep) == (None, True)


def test_geocode_reports_a_network_failure_as_unresolved():
    def boom(_url):
        raise OSError("no network")
    assert geocode_cafes.geocode("Nowhere, 1 Elm St, Boston, MA",
                                 fetch=boom, sleep=no_sleep) == (None, False)


# --------------------------------------------------------------------------
# The cache: never re-query what we already know


def test_cached_entries_are_never_requeried():
    cache = {"points": {"Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]}, "missing": []}
    fetch = Recorder()
    stats = geocode_cafes.update_cache(["Cafe, 1 Elm St, Boston, MA"], cache,
                                       fetch=fetch, sleep=no_sleep)
    assert fetch.count == 0
    assert stats["queried"] == 0
    assert cache["points"] == {"Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]}


def test_recorded_misses_are_never_requeried():
    """This is what makes the sync-time cost converge to zero."""
    cache = {"points": {}, "missing": ["Nowhere, 1 Elm St, Boston, MA"]}
    fetch = Recorder()
    geocode_cafes.update_cache(["Nowhere, 1 Elm St, Boston, MA"], cache,
                               fetch=fetch, sleep=no_sleep)
    assert fetch.count == 0


def test_retry_missing_looks_again():
    cache = {"points": {}, "missing": ["Cafe, 1 Elm St, Boston, MA"]}
    fetch = Recorder({"Cafe, 1 Elm St, Boston, MA": result(42.1, -71.2)})
    geocode_cafes.update_cache(["Cafe, 1 Elm St, Boston, MA"], cache,
                               retry_missing=True, fetch=fetch, sleep=no_sleep)
    assert cache["points"] == {"Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]}
    # It resolved, so it is no longer a miss.
    assert cache["missing"] == []


def test_a_miss_is_recorded_and_does_not_crash():
    cache = empty_cache()
    stats = geocode_cafes.update_cache(["Nowhere, 1 Elm St, Boston, MA"], cache,
                                       fetch=Recorder(), sleep=no_sleep)
    assert cache["missing"] == ["Nowhere, 1 Elm St, Boston, MA"]
    assert cache["points"] == {}
    assert stats["missed"] == 1


def test_a_network_failure_is_not_recorded_as_a_miss():
    """A Nominatim outage must not poison the cache for good."""
    def boom(_url):
        raise OSError("no network")
    cache = empty_cache()
    stats = geocode_cafes.update_cache(["Cafe, 1 Elm St, Boston, MA"], cache,
                                       fetch=boom, sleep=no_sleep)
    assert cache == empty_cache()
    assert stats == {"queried": 1, "found": 0, "missed": 0, "deferred": 1, "pending": 0}


def test_limit_bounds_a_run():
    names = [f"Cafe {n}, {n} Elm St, Boston, MA" for n in range(5)]
    fetch = Recorder({name: result(42.0 + i, -71.0) for i, name in enumerate(names)})
    cache = empty_cache()
    stats = geocode_cafes.update_cache(names, cache, limit=2, fetch=fetch, sleep=no_sleep)
    assert stats["queried"] == 2 and stats["pending"] == 3
    assert len(cache["points"]) == 2


def test_a_second_run_picks_up_where_the_limit_stopped():
    names = [f"Cafe {n}, {n} Elm St, Boston, MA" for n in range(4)]
    answers = {name: result(42.0 + i, -71.0) for i, name in enumerate(names)}
    cache = empty_cache()
    for _ in range(2):
        geocode_cafes.update_cache(names, cache, limit=2,
                                   fetch=Recorder(answers), sleep=no_sleep)
    assert sorted(cache["points"]) == sorted(names)


def test_sleeps_between_locations():
    names = ["A Cafe, 1 Elm St, Boston, MA", "B Cafe, 2 Elm St, Boston, MA"]
    fetch = Recorder({name: result(42.0, -71.0) for name in names})
    waits = []
    geocode_cafes.update_cache(names, cache=empty_cache(), fetch=fetch, sleep=waits.append)
    assert waits == [geocode_cafes.DELAY]


# --------------------------------------------------------------------------
# Reading and writing the file


def test_missing_cache_file_is_an_empty_one(tmp_path):
    assert geocode_cafes.load_cache(tmp_path / "nope.json") == empty_cache()


def test_round_trip(tmp_path):
    path = tmp_path / "cafe-points.json"
    cache = {"points": {"Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]},
             "missing": ["Nowhere, 2 Elm St, Boston, MA"]}
    assert geocode_cafes.write_cache(path, cache) is True
    assert geocode_cafes.load_cache(path) == cache


def test_output_is_sorted_and_stable(tmp_path):
    path = tmp_path / "cafe-points.json"
    cache = {"points": {"B, 2 Elm St, Boston, MA": [1.0, 2.0],
                        "A, 1 Elm St, Boston, MA": [3.0, 4.0]},
             "missing": ["Z, 9 Elm St, Boston, MA", "M, 5 Elm St, Boston, MA",
                         "Z, 9 Elm St, Boston, MA"]}
    geocode_cafes.write_cache(path, cache)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written["points"]) == ["A, 1 Elm St, Boston, MA", "B, 2 Elm St, Boston, MA"]
    assert written["missing"] == ["M, 5 Elm St, Boston, MA", "Z, 9 Elm St, Boston, MA"]
    assert path.read_text(encoding="utf-8").endswith("}\n")


def test_write_is_skipped_when_nothing_changed(tmp_path):
    """The no-churn guard: a no-op sync must leave the bytes alone."""
    path = tmp_path / "cafe-points.json"
    cache = {"points": {"Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]}, "missing": []}
    assert geocode_cafes.write_cache(path, cache) is True
    before = path.stat().st_mtime_ns
    assert geocode_cafes.write_cache(path, geocode_cafes.load_cache(path)) is False
    assert path.stat().st_mtime_ns == before


def test_non_ascii_names_survive(tmp_path):
    path = tmp_path / "cafe-points.json"
    name = "Lizzy’s Ice Cream, 29 Church St, Cambridge, MA"
    geocode_cafes.write_cache(path, {"points": {name: [42.3, -71.1]}, "missing": []})
    assert name in path.read_text(encoding="utf-8")
    assert geocode_cafes.load_cache(path)["points"][name] == [42.3, -71.1]


@pytest.mark.parametrize("body", [
    "{not json",
    "[]",
    '{"points": []}',
    '{"points": {}, "missing": {}}',
    '{"points": {"a": [1]}, "missing": []}',
    '{"points": {"a": "42,-71"}, "missing": []}',
])
def test_a_malformed_cache_fails_loudly(tmp_path, body):
    path = tmp_path / "cafe-points.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(geocode_cafes.GeocodeError):
        geocode_cafes.load_cache(path)


# --------------------------------------------------------------------------
# End to end through the CLI


def test_cli_writes_the_cache(tmp_path, monkeypatch, capsys):
    source = tmp_path / "events-past.json"
    source.write_text(json.dumps(payload("Cafe, 1 Elm St, Boston, MA")), encoding="utf-8")
    cache_path = tmp_path / "cafe-points.json"
    fetch = Recorder({"Cafe, 1 Elm St, Boston, MA": result(42.1, -71.2)})
    monkeypatch.setattr(geocode_cafes, "fetch_json", fetch)
    monkeypatch.setattr(geocode_cafes.time, "sleep", no_sleep)

    assert geocode_cafes.main([str(source), "--cache", str(cache_path)]) == 0
    assert geocode_cafes.load_cache(cache_path)["points"] == {
        "Cafe, 1 Elm St, Boston, MA": [42.1, -71.2]}
    assert "1 café locations" in capsys.readouterr().out


def test_cli_second_run_is_a_no_op(tmp_path, monkeypatch, capsys):
    source = tmp_path / "events-past.json"
    source.write_text(json.dumps(payload("Cafe, 1 Elm St, Boston, MA")), encoding="utf-8")
    cache_path = tmp_path / "cafe-points.json"
    fetch = Recorder({"Cafe, 1 Elm St, Boston, MA": result(42.1, -71.2)})
    monkeypatch.setattr(geocode_cafes, "fetch_json", fetch)
    monkeypatch.setattr(geocode_cafes.time, "sleep", no_sleep)

    geocode_cafes.main([str(source), "--cache", str(cache_path)])
    before = cache_path.read_bytes()
    calls = fetch.count
    capsys.readouterr()

    geocode_cafes.main([str(source), "--cache", str(cache_path)])
    assert cache_path.read_bytes() == before
    assert fetch.count == calls
    assert "cache unchanged" in capsys.readouterr().out


def test_cli_reports_a_missing_source(tmp_path, capsys):
    assert geocode_cafes.main([str(tmp_path / "nope.json"),
                               "--cache", str(tmp_path / "c.json")]) == 1
    assert "geocode_cafes" in capsys.readouterr().err


def test_cli_reports_a_malformed_cache(tmp_path, capsys):
    source = tmp_path / "events-past.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    cache_path = tmp_path / "cafe-points.json"
    cache_path.write_text("{oops", encoding="utf-8")
    assert geocode_cafes.main([str(source), "--cache", str(cache_path)]) == 1
    assert "geocode_cafes" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The published cache itself
#
# It is generated onto the `data` branch, not committed beside this code, so
# both checks below skip on a plain code checkout and run once the data has
# been pulled into site/ (scripts/pull_data.sh).


REPO = REPO_ROOT
DATA = REPO / "site"


def require_data(*names):
    for name in names:
        if not (DATA / name).exists():
            pytest.skip(f"site/{name} lives on the data branch (scripts/pull_data.sh)")


def test_committed_cache_is_well_formed():
    require_data("cafe-points.json")
    cache = geocode_cafes.load_cache(REPO / "site" / "cafe-points.json")
    assert cache["points"], "the committed cache should hold the seeded cafés"
    for name, (lat, lon) in cache["points"].items():
        # Everything the group has ridden to is in New England, and a swapped
        # lat/lon pair would land in the Indian Ocean.
        assert 40.0 < lat < 44.0, name
        assert -74.0 < lon < -69.0, name


def test_committed_cache_covers_the_archive():
    """Every archived café address should already have a pin — no sync needed."""
    require_data("cafe-points.json", "events-past.json", "events.json")
    archive = json.loads((REPO / "site" / "events-past.json").read_text(encoding="utf-8"))
    upcoming = json.loads((REPO / "site" / "events.json").read_text(encoding="utf-8"))
    cache = geocode_cafes.load_cache(REPO / "site" / "cafe-points.json")
    known = set(cache["points"]) | set(cache["missing"])
    assert not set(geocode_cafes.cafe_locations([archive, upcoming])) - known
