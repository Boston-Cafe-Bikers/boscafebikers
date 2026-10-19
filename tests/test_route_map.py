"""Tests for the drawn route map — scripts/route_map.py and render_route_maps.py.

Fully offline: the geometry is a fixed polyline, the router is stubbed, and the
basemap "tiles" are one 8x8 fixture PNG (tests/fixtures/tile.png).
"""

from __future__ import annotations

import json
import pytest
from xml.etree import ElementTree

from helpers import GEOMETRY, TILE

import render_route_maps
import route_map

# A route that is much wider than it is tall.
FLAT = [(42.3500, -71.2000), (42.3505, -71.0500)]
# ...and one much taller than it is wide.
TALL = [(42.2500, -71.1000), (42.4500, -71.1005)]


def tiles_for(points):
    """A complete fixture tile set for the plan `route_map` makes for `points`."""
    zoom, plan = route_map.tile_plan(points)
    return {(zoom, x, y): TILE for x, y in plan}


def fake_tile(zoom, x, y):
    return TILE


def no_tile(zoom, x, y):
    return None


def images(root):
    return [el for el in root.iter() if el.tag.endswith("image")]


def svg_tree(svg: str):
    return ElementTree.fromstring(svg)


# --- projection ---


def test_mercator_is_monotonic():
    """North is up (smaller y) and east is right (larger x)."""
    south = route_map.mercator(42.0, -71.0)
    north = route_map.mercator(43.0, -71.0)
    east = route_map.mercator(42.0, -70.0)
    assert north[1] < south[1]
    assert east[0] > south[0]


def test_projected_points_stay_inside_the_drawing_area():
    width, height = route_map.canvas_size(GEOMETRY)
    projected = route_map.project(GEOMETRY, width, height)
    assert len(projected) == len(GEOMETRY)
    for x, y in projected:
        assert route_map.PAD_X - 0.5 <= x <= width - route_map.PAD_X + 0.5
        assert route_map.PAD_TOP - 0.5 <= y <= height - route_map.PAD_BOTTOM + 0.5


def test_projection_preserves_the_route_shape():
    """One scale for both axes — a map that stretches to fill would lie."""
    projected = route_map.project(GEOMETRY)
    span_x = max(x for x, _ in projected) - min(x for x, _ in projected)
    span_y = max(y for _, y in projected) - min(y for _, y in projected)
    merc = [route_map.mercator(lat, lon) for lat, lon in GEOMETRY]
    true_x = max(x for x, _ in merc) - min(x for x, _ in merc)
    true_y = max(y for _, y in merc) - min(y for _, y in merc)
    assert abs((span_x / span_y) - (true_x / true_y)) < 0.01


def test_projection_survives_a_degenerate_route():
    """Two identical points have no span to divide by."""
    projected = route_map.project([(42.0, -71.0), (42.0, -71.0)])
    assert len(projected) == 2
    assert all(isinstance(value, float) for point in projected for value in point)


def test_project_empty_is_empty():
    assert route_map.project([]) == []


# --- canvas shape ---


def test_canvas_follows_the_route_shape():
    flat_w, flat_h = route_map.canvas_size(FLAT)
    tall_w, tall_h = route_map.canvas_size(TALL)
    assert flat_h < tall_h
    assert flat_w == tall_w == route_map.WIDTH


def test_canvas_aspect_is_clamped_at_both_ends():
    for points in (FLAT, TALL, GEOMETRY, []):
        width, height = route_map.canvas_size(points)
        assert route_map.MIN_ASPECT - 0.01 <= height / width <= route_map.MAX_ASPECT + 0.01


# --- basemap tiles ---


def test_tile_index_matches_the_slippy_map_convention():
    """Boston at zoom 13 is the well-known 13/2479/3029 tile."""
    assert route_map.tile_index(42.3601, -71.0589, 13) == (2479, 3029)
    assert route_map.tile_index(42.3601, -71.0589, 14) == (4958, 6059)
    assert route_map.tile_index(0, 0, 1) == (1, 1)
    assert route_map.tile_index(85, -180, 1) == (0, 0)
    assert route_map.tile_index(-85, 179.99, 3) == (7, 7)


def test_tile_index_is_clamped_to_the_grid():
    """Longitude 180 / the pole must not spill past the last tile."""
    assert route_map.tile_index(-89.9, 180.0, 4) == (15, 15)


def test_tile_plan_stays_within_the_budget_and_is_as_deep_as_it_can_be():
    for points in (GEOMETRY, FLAT, TALL):
        width, height = route_map.canvas_size(points)
        zoom, plan = route_map.tile_plan(points)
        assert 1 <= len(plan) <= route_map.MAX_TILES
        assert len(plan) == len(set(plan))
        # Tiles are at least MIN_TILE_SIDE canvas px across, and one zoom
        # deeper they wouldn't be — that's what makes this zoom the deepest
        # one, and the tiles as sharp as the byte budget allows.
        fitted = route_map.fit(points, width, height)
        assert route_map.tile_side(fitted, zoom) >= route_map.MIN_TILE_SIDE
        assert route_map.tile_side(fitted, zoom + 1) < route_map.MIN_TILE_SIDE


def test_a_bigger_canvas_scale_means_a_deeper_zoom():
    """Twice the scale (half the ground per canvas) → one zoom deeper."""
    fitted = (1_000_000.0, 0.0, 0.0, 0.0, 0.0)
    doubled = (2_000_000.0, 0.0, 0.0, 0.0, 0.0)
    assert route_map.choose_zoom(doubled) == route_map.choose_zoom(fitted) + 1


def test_tile_plan_covers_every_corner_of_the_canvas():
    for points in (GEOMETRY, FLAT, TALL):
        width, height = route_map.canvas_size(points)
        zoom, plan = route_map.tile_plan(points)
        fitted = route_map.fit(points, width, height)
        rects = [route_map.tile_rect(fitted, zoom, x, y) for x, y in plan]
        for cx, cy in ((0, 0), (width, 0), (0, height), (width, height), (width / 2, height / 2)):
            assert any(
                left - 1e-6 <= cx <= left + side + 1e-6 and top - 1e-6 <= cy <= top + side + 1e-6
                for left, top, side in rects
            ), (points, cx, cy)


def test_tiles_are_placed_where_the_route_is():
    """The tile that contains a route point must be drawn under that point."""
    width, height = route_map.canvas_size(GEOMETRY)
    zoom, plan = route_map.tile_plan(GEOMETRY)
    fitted = route_map.fit(GEOMETRY, width, height)
    projected = route_map.project(GEOMETRY, width, height)
    for (lat, lon), (px, py) in zip(GEOMETRY, projected):
        tile = route_map.tile_index(lat, lon, zoom)
        assert tile in plan
        left, top, side = route_map.tile_rect(fitted, zoom, *tile)
        assert left <= px <= left + side
        assert top <= py <= top + side


def test_tile_plan_for_nothing_is_empty():
    assert route_map.tile_plan([]) == (route_map.MIN_ZOOM, [])


def test_tile_mime_reads_the_magic_bytes():
    assert route_map.tile_mime(TILE) == "image/png"
    assert route_map.tile_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert route_map.tile_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert route_map.tile_mime(b"<html>not a tile</html>") is None
    assert route_map.tile_mime(b"") is None


def test_svg_embeds_every_planned_tile_as_a_data_uri():
    svg = route_map.render_route_svg(
        GEOMETRY, "A", "B", "~4.0 mi", "Ride",
        tiles=tiles_for(GEOMETRY), tile_source="osm-standard", tile_credit="",
    )
    root = svg_tree(svg)
    zoom, plan = route_map.tile_plan(GEOMETRY)
    imgs = images(root)
    assert len(imgs) == len(plan)
    for img in imgs:
        assert img.get("href").startswith("data:image/png;base64,")
        assert img.get("width") == img.get("height") == str(route_map.TILE_SIZE)
    assert root.get(route_map.BASEMAP_ATTR) == "osm-standard"
    assert route_map.has_basemap(svg)
    # The tiles go under everything else: first drawn, after the background.
    children = list(root)
    first_image_parent = next(el for el in children if el.tag.endswith("g"))
    assert children.index(first_image_parent) < children.index(
        next(el for el in children if el.tag.endswith("path"))
    )


def test_svg_with_tiles_credits_the_map_and_any_provider_line():
    """OSM's own tiles need no extra line; a provider that does gets appended."""
    def credit(svg):
        texts = [el.text for el in svg_tree(svg).iter() if el.tag.endswith("text")]
        return [text for text in texts if "OpenStreetMap" in (text or "")]

    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles_for(GEOMETRY))
    assert credit(svg) == ["Map © OpenStreetMap contributors"]
    svg = route_map.render_route_svg(
        GEOMETRY, "A", "B", "", "Ride", tiles=tiles_for(GEOMETRY), tile_credit="© Example Tiles"
    )
    assert credit(svg) == ["Map © OpenStreetMap contributors · © Example Tiles"]


def test_credit_is_big_enough_to_read_on_the_card():
    """The card shows the 800-unit canvas at ~280 CSS px on a phone, so the
    licence credit has to be at least 16 units to be legible there at all."""
    assert route_map.CREDIT_SIZE >= 16
    for tiles in (None, tiles_for(GEOMETRY)):
        root = svg_tree(route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles))
        credit = [el for el in root.iter() if el.tag.endswith("text") and "OpenStreetMap" in (el.text or "")]
        assert len(credit) == 1
        assert int(credit[0].get("font-size")) == route_map.CREDIT_SIZE


def test_svg_without_tiles_is_unmarked_and_credits_only_the_route():
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride")
    assert not route_map.has_basemap(svg)
    assert svg_tree(svg).get(route_map.BASEMAP_ATTR) is None
    assert not images(svg_tree(svg))
    assert "Route © OpenStreetMap contributors" in svg


def test_a_missing_tile_means_no_basemap_at_all():
    """Fail-soft, all or nothing: a basemap with a hole is not a basemap."""
    tiles = tiles_for(GEOMETRY)
    tiles.pop(next(iter(tiles)))
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles)
    assert not images(svg_tree(svg))
    assert not route_map.has_basemap(svg)
    # ...and it's the plain drawing, byte for byte.
    assert svg == route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride")


def test_a_tile_that_is_not_an_image_is_rejected():
    tiles = tiles_for(GEOMETRY)
    tiles[next(iter(tiles))] = b"<html>rate limited</html>"
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles)
    assert not images(svg_tree(svg))
    assert "<html" not in svg


def test_tiles_sit_under_a_wash_and_the_labels_get_a_halo():
    svg = route_map.render_route_svg(
        GEOMETRY, "JP Licks, Boston", "Tatte, Cambridge", "~4.0 mi", "Ride",
        tiles=tiles_for(GEOMETRY),
    )
    root = svg_tree(svg)
    rects = [el for el in root.iter() if el.tag.endswith("rect")]
    assert any(el.get("opacity") == str(route_map.WASH_OPACITY) for el in rects)
    assert any((el.get("fill") or "").startswith("url(#") for el in rects)
    labels = [el for el in root.iter() if el.tag.endswith("text") and "Start" in (el.text or "")]
    assert labels and labels[0].get("paint-order") == "stroke"


def test_has_basemap_only_trusts_the_root_attribute():
    assert route_map.has_basemap('<svg xmlns="x" data-basemap="osm-standard">')
    assert not route_map.has_basemap('<svg xmlns="x" data-basemap="">')
    assert not route_map.has_basemap('<svg xmlns="x"><title>data-basemap="no"</title>')
    assert not route_map.has_basemap("<svg/>")


# --- cropping edge tiles ---


def palette_png(width=256, height=256, filters=None):
    """A palette PNG whose pixel (x, y) has index (x + y) % 7 — written here
    with plain filter-0 rows so the test doesn't lean on the code under test."""
    import struct
    import zlib

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(
            ">I", zlib.crc32(tag + body) & 0xFFFFFFFF
        )

    rows = []
    for y in range(height):
        row = bytes((x + y) % 7 for x in range(width))
        kind = (filters or [0])[y % len(filters or [0])]
        if kind == 1:  # Sub
            row = bytes([row[0]] + [(row[i] - row[i - 1]) & 255 for i in range(1, width)])
        rows.append(bytes([kind]) + row)
    palette = bytes(range(21))  # 7 RGB entries
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", palette)
        + chunk(b"tRNS", b"\xff" * 7)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def decode_filter0_png(data):
    """(width, height, rows, chunk tags) — reads only filter-0 rows, on purpose."""
    import struct
    import zlib

    tags, pos, idat = [], 8, b""
    while pos < len(data):
        length, = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        tags.append(tag)
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    width, height = struct.unpack(">II", data[16:24])
    raw = zlib.decompress(idat)
    rows = []
    for y in range(height):
        assert raw[y * (width + 1)] == 0, "crop_png writes filter-0 rows"
        rows.append(raw[y * (width + 1) + 1:(y + 1) * (width + 1)])
    return width, height, rows, tags


def test_crop_png_keeps_exactly_the_window_pixels():
    src = palette_png()
    out = route_map.crop_png(src, 40, 200, 100, 56)
    width, height, rows, tags = decode_filter0_png(out)
    assert (width, height) == (100, 56)
    for y, row in enumerate(rows):
        assert list(row) == [(x + y + 240) % 7 for x in range(100)]
    # Palette and transparency come along untouched; the size drops.
    assert tags == [b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"]
    assert len(out) < len(src)
    assert route_map.png_size(out) == (100, 56)
    assert route_map.tile_mime(out) == "image/png"


def test_crop_png_reads_a_tile_whose_image_data_spans_several_idat_chunks():
    """Live OSM tiles sometimes arrive with two IDAT chunks (13/2479/3029 did);
    the crop has to join them before inflating, not read just the first."""
    import struct
    import zlib

    src = palette_png(64, 64)
    pos, out, idat = 8, src[:8], b""
    while pos < len(src):
        length, = struct.unpack(">I", src[pos:pos + 4])
        tag, body = src[pos + 4:pos + 8], src[pos + 8:pos + 8 + length]
        if tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            for part in (idat[:len(idat) // 3], idat[len(idat) // 3:]):
                out += struct.pack(">I", len(part)) + b"IDAT" + part + struct.pack(
                    ">I", zlib.crc32(b"IDAT" + part) & 0xFFFFFFFF
                )
            out += src[pos:pos + 12 + length]
        else:
            out += src[pos:pos + 12 + length]
        pos += 12 + length
    assert out.count(b"IDAT") == 2
    cropped = route_map.crop_png(out, 10, 20, 30, 15)
    width, height, rows, _ = decode_filter0_png(cropped)
    assert (width, height) == (30, 15)
    for y, row in enumerate(rows):
        assert list(row) == [((x + 10) + (y + 20)) % 7 for x in range(30)]


def test_crop_png_undoes_sub_and_up_filtered_rows():
    """OSM's standard tiles use filter 0 today; the decoder still handles the others."""
    src = palette_png(64, 64, filters=[0, 1])
    out = route_map.crop_png(src, 10, 10, 20, 20)
    _, _, rows, _ = decode_filter0_png(out)
    for y, row in enumerate(rows):
        assert list(row) == [(x + y + 20) % 7 for x in range(20)]


def test_crop_png_refuses_what_it_does_not_understand():
    src = palette_png(32, 32)
    assert route_map.crop_png(b"not a png", 0, 0, 1, 1) is None
    assert route_map.crop_png(src, 0, 0, 33, 1) is None  # window off the edge
    assert route_map.crop_png(src, -1, 0, 4, 4) is None
    assert route_map.crop_png(src, 0, 0, 0, 4) is None
    interlaced = bytearray(src)
    interlaced[28] = 1  # IHDR interlace flag
    assert route_map.crop_png(bytes(interlaced), 0, 0, 4, 4) is None
    sixteen_bit = bytearray(src)
    sixteen_bit[24] = 16
    assert route_map.crop_png(bytes(sixteen_bit), 0, 0, 4, 4) is None
    assert route_map.crop_png(src[:40], 0, 0, 4, 4) is None  # truncated


def test_visible_window_is_the_whole_tile_when_it_is_inside_the_canvas():
    assert route_map.visible_window(10, 10, 256, 800, 800) == (0, 0, 256, 256)


def test_visible_window_rounds_outwards_at_the_canvas_edge():
    # Tile hanging 100px off the left and 100px off the top of an 800x800
    # canvas, drawn at 2x: the visible part starts 50 tile pixels in.
    x, y, w, h = route_map.visible_window(-100, -100, 512, 800, 800)
    assert (x, y) == (50, 50)
    assert (w, h) == (206, 206)
    # Off the far edges: only the first 100 canvas px (50 tile px) show.
    x, y, w, h = route_map.visible_window(700, 700, 512, 800, 800)
    assert (x, y) == (0, 0) and (w, h) == (50, 50)
    # Non-integer edges round outwards so nothing visible is cut.
    x, y, w, h = route_map.visible_window(-0.5, 0, 256, 800, 800)
    assert x == 0 and w == 256


def test_edge_tiles_are_cropped_and_interior_tiles_embedded_as_served():
    zoom, plan = route_map.tile_plan(GEOMETRY)
    tiles = {(zoom, x, y): palette_png() for x, y in plan}
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles)
    root = svg_tree(svg)
    imgs = images(root)
    assert len(imgs) == len(plan)
    width, height = route_map.canvas_size(GEOMETRY)
    fitted = route_map.fit(GEOMETRY, width, height)
    whole = len("data:image/png;base64,") + len(__import__("base64").b64encode(palette_png()))
    cropped = 0
    for img, (x, y) in zip(imgs, plan):
        left, top, side = route_map.tile_rect(fitted, zoom, x, y)
        inside = left >= 0 and top >= 0 and left + side <= width and top + side <= height
        w, h = int(img.get("width")), int(img.get("height"))
        assert 0 < w <= route_map.TILE_SIZE and 0 < h <= route_map.TILE_SIZE
        if inside:
            assert (w, h) == (route_map.TILE_SIZE, route_map.TILE_SIZE)
            assert len(img.get("href")) == whole
        else:
            cropped += 1
            assert (w, h) != (route_map.TILE_SIZE, route_map.TILE_SIZE) or len(img.get("href")) < whole
    assert cropped > 0
    # The map is still marked finished, and the embedded bytes are fewer than
    # the uncropped set would have been.
    assert route_map.has_basemap(svg)
    assert sum(len(img.get("href")) for img in imgs) < whole * len(plan)


def test_a_tile_of_another_size_is_embedded_whole(tmp_path):
    """The 8x8 fixture isn't a 256px tile, so it is stretched, never cropped."""
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride", tiles=tiles_for(GEOMETRY))
    for img in images(svg_tree(svg)):
        assert img.get("width") == img.get("height") == str(route_map.TILE_SIZE)


# --- the SVG itself ---


def test_svg_is_well_formed_and_sized():
    svg = route_map.render_route_svg(GEOMETRY, "A, Boston", "B, Watertown", "~4.0 mi", "Ride")
    root = svg_tree(svg)
    assert root.tag.endswith("svg")
    width, height = route_map.canvas_size(GEOMETRY)
    assert root.get("viewBox") == f"0 0 {width} {height}"


def test_svg_draws_the_route_and_both_endpoints():
    svg = route_map.render_route_svg(GEOMETRY)
    root = svg_tree(svg)
    paths = [el for el in root.iter() if el.tag.endswith("path")]
    circles = [el for el in root.iter() if el.tag.endswith("circle")]
    assert len(paths) == 2  # casing + route
    assert len(circles) == 2  # start + end
    assert paths[0].get("d").count("L") == len(GEOMETRY) - 1


def test_svg_carries_the_labels_and_the_osm_credit():
    svg = route_map.render_route_svg(
        GEOMETRY, "JP Licks, Boston", "Tatte Bakery, Cambridge", "~4.0 mi", "Sunday ride"
    )
    text = " ".join(el.text or "" for el in svg_tree(svg).iter() if el.tag.endswith("text"))
    assert "Start · JP Licks" in text
    assert "Tatte Bakery · End" in text
    assert "~4.0 mi" in text
    assert "OpenStreetMap" in text


def test_svg_escapes_text_from_the_feed():
    """Titles and place names come from Partiful — they can't inject markup."""
    svg = route_map.render_route_svg(GEOMETRY, "<script>x</script>, Boston", "B", "", "A & B")
    assert "<script>" not in svg
    svg_tree(svg)  # still parses


def test_svg_without_a_distance_has_no_badge():
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", "Ride")
    rects = [el for el in svg_tree(svg).iter() if el.tag.endswith("rect")]
    assert len(rects) == 1  # just the background


def test_the_distance_badge_is_pinned_into_the_corner_clear_of_the_route():
    """The chip is frame furniture, not part of the drawing.

    An opaque pill hides whatever tile art is under it, so it lives in the
    corner — inside PAD_X and above PAD_TOP, where the route never goes — and
    wears a foam ring so map text running under it reads as covered rather
    than eaten (the same job the endpoint labels' halo does).
    """
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "~4.0 mi", "Ride")
    badges = [
        el for el in svg_tree(svg).iter() if el.tag.endswith("rect") and el.get("rx")
    ]
    assert len(badges) == 1
    badge, = badges
    assert float(badge.get("x")) == route_map.BADGE_INSET < route_map.PAD_X
    assert float(badge.get("y")) == route_map.BADGE_INSET
    assert float(badge.get("y")) + float(badge.get("height")) <= route_map.PAD_TOP
    assert badge.get("stroke") == route_map.FOAM


def test_svg_with_no_geometry_still_renders():
    svg = route_map.render_route_svg([], "A", "B", "~1.0 mi", "Ride")
    root = svg_tree(svg)
    assert not [el for el in root.iter() if el.tag.endswith("path")]


def test_long_place_names_are_clipped_to_fit_side_by_side():
    left, right = route_map.fit_labels(
        "The Extremely Long Café Name On Beacon Street, Boston, MA",
        "Another Extremely Long Destination Name, Watertown, MA",
        route_map.WIDTH,
    )
    budget = route_map.WIDTH - 2 * route_map.PAD_X - 24
    assert (len(left) + len(right)) * route_map.LABEL_SIZE * route_map.CHAR_WIDTH_RATIO <= budget
    assert left.startswith("Start · ")
    assert right.endswith(" · End")


def test_start_label_names_the_bluebikes_station_not_bluebikes():
    """Same rule as ride-card.js startName(): the dock's detail, not the brand."""
    left, right = route_map.fit_labels(
        "Bluebikes, Cleveland Circle, Boston, MA 02135",
        "O'Some Café, 100 Main St, Watertown, MA 02472",
        route_map.WIDTH,
    )
    assert left == "Start · Cleveland Circle"
    assert right == "O'Some Café · End"


@pytest.mark.parametrize(
    "address, expected",
    [
        ("Bluebikes, Cleveland Circle, Boston, MA 02135", "Cleveland Circle"),
        ("Bluebikes, Washington St at Temple Pl, Boston, MA 02111", "Washington St at Temple Pl"),
        ("Bluebikes, Bunker Hill Mall, Main St at Austin St, Boston, MA 02129",
         "Bunker Hill Mall, Main St at Austin St"),
        ("bluebikes  Cleveland Circle", "Cleveland Circle"),
        ("Bluebikes", "Bluebikes"),
        ("J.P. Licks, 659 Centre St, Boston, MA 02130", "J.P. Licks"),
        ("H Mart Brookline, Beacon Street, Brookline, MA", "H Mart Brookline"),
        ("", ""),
    ],
)
def test_start_name_matches_the_card(address, expected):
    assert route_map._start_name(address) == expected


def test_end_label_keeps_the_place_name_even_for_a_bluebikes_dock():
    left, right = route_map.fit_labels(
        "Localito, Riverside Avenue, Medford, MA",
        "Bluebikes, Bunker Hill Mall, Main St at Austin St, Boston, MA 02129",
        route_map.WIDTH,
    )
    assert left == "Start · Localito"
    assert right == "Bluebikes · End"


def test_svg_attributes_survive_a_double_quote_in_the_title():
    title = 'Ride to "The Bean" & back'
    svg = route_map.render_route_svg(GEOMETRY, "A", "B", "", title)
    root = svg_tree(svg)  # a bare quote in aria-label made this malformed
    assert root.get("aria-label") == title
    assert root.find("{http://www.w3.org/2000/svg}title").text == title


def test_short_place_names_are_left_alone():
    left, right = route_map.fit_labels("Tatte, Boston, MA", "Merai, Brookline, MA", route_map.WIDTH)
    assert left == "Start · Tatte"
    assert right == "Merai · End"


# --- render_route_maps ---


def ride(uid="ride-1", **extra):
    base = {
        "uid": uid,
        "title": "Café ride",
        "routes": [{
            "label": "Estimated Route",
            "url": "https://maps.app.goo.gl/x",
            "start": "A, Boston",
            "end": "B, Watertown",
            "distance_display": "~4.0 mi",
            "points": [[42.3355, -71.1506], [42.3668, -71.1868]],
        }],
    }
    base.update(extra)
    return base


def fake_geometry(points):
    return GEOMETRY


def test_render_writes_the_svg_and_points_the_ride_at_it(tmp_path):
    r = ride()
    url = render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile)
    assert url == "maps/ride-1.svg"
    assert r["map_image"] == "maps/ride-1.svg"
    svg = (tmp_path / "ride-1.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    assert route_map.has_basemap(svg)
    assert svg_tree(svg).get(route_map.BASEMAP_ATTR) == render_route_maps.TILE_SOURCE
    assert "Map © OpenStreetMap contributors" in svg
    if render_route_maps.TILE_CREDIT:
        assert render_route_maps.TILE_CREDIT in svg


def test_tiles_come_from_openstreetmap_and_are_credited():
    """The provider is a licensing decision, not a URL: the tiles are stored in
    committed SVGs, which OSM's terms allow with attribution and CARTO's forbid.
    Changing TILE_URL means re-checking that the new provider allows it."""
    assert render_route_maps.TILE_URL.startswith("https://tile.openstreetmap.org/")
    assert render_route_maps.TILE_URL.endswith("/{z}/{x}/{y}.png")
    assert "carto" not in render_route_maps.TILE_URL
    assert render_route_maps.TILE_SOURCE == "osm-standard"
    assert render_route_maps.USER_AGENT == "boscafebikers-sync/1.0"


def test_fetch_tiles_asks_for_exactly_the_plan():
    asked = []

    def spy(zoom, x, y):
        asked.append((zoom, x, y))
        return TILE

    tiles = render_route_maps.fetch_tiles(GEOMETRY, spy)
    zoom, plan = route_map.tile_plan(GEOMETRY)
    assert asked == [(zoom, x, y) for x, y in plan]
    assert set(tiles) == set(asked)


def test_fetch_tiles_gives_up_on_the_first_miss():
    calls = []

    def flaky(zoom, x, y):
        calls.append((x, y))
        return None if len(calls) == 2 else TILE

    assert render_route_maps.fetch_tiles(GEOMETRY, flaky) == {}
    assert len(calls) == 2  # no point fetching the rest


def test_render_without_tiles_still_draws_the_route_but_stays_unfinished(tmp_path):
    r = ride()
    url = render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=no_tile)
    assert url == "maps/ride-1.svg"
    assert r["map_image"] == "maps/ride-1.svg"
    svg = (tmp_path / "ride-1.svg").read_text(encoding="utf-8")
    assert not route_map.has_basemap(svg)
    assert not images(svg_tree(svg))


def test_an_unfinished_map_is_redrawn_once_tiles_are_available(tmp_path):
    r = ride()
    render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=no_tile)
    plain = (tmp_path / "ride-1.svg").read_text(encoding="utf-8")
    # Next sync: tiles still down → nothing rewritten, ride still wired up.
    r2 = ride()
    assert render_route_maps.render_for_ride(r2, tmp_path, "maps", fetch=fake_geometry, fetch_tile=no_tile) is None
    assert r2["map_image"] == "maps/ride-1.svg"
    assert (tmp_path / "ride-1.svg").read_text(encoding="utf-8") == plain
    # The sync after that: tiles back → the map gets its basemap.
    r3 = ride()
    assert render_route_maps.render_for_ride(r3, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile) == "maps/ride-1.svg"
    assert route_map.has_basemap((tmp_path / "ride-1.svg").read_text(encoding="utf-8"))


def test_an_unfinished_map_survives_a_router_outage(tmp_path):
    (tmp_path / "ride-1.svg").write_text("<svg/>", encoding="utf-8")
    r = ride()
    assert render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=lambda p: [], fetch_tile=fake_tile) is None
    assert r["map_image"] == "maps/ride-1.svg"
    assert (tmp_path / "ride-1.svg").read_text(encoding="utf-8") == "<svg/>"


def test_redraw_draws_a_finished_map_again(tmp_path):
    (tmp_path / "ride-1.svg").write_text('<svg data-basemap="osm-standard"/>', encoding="utf-8")
    r = ride()
    assert render_route_maps.render_for_ride(
        r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile, redraw=True
    ) == "maps/ride-1.svg"
    assert len((tmp_path / "ride-1.svg").read_text(encoding="utf-8")) > 100


def test_the_default_tile_fetcher_never_runs_in_tests():
    """Every test above injects a fetcher; this pins the seam it goes through."""
    assert render_route_maps.render_for_ride.__defaults__[1] is render_route_maps.fetch_tile
    assert "fetch_tile" in render_route_maps.process.__code__.co_varnames


def test_render_is_skipped_when_the_map_already_exists(tmp_path):
    (tmp_path / "ride-1.svg").write_text('<svg data-basemap="osm-standard"/>', encoding="utf-8")

    def unreachable(points):
        raise AssertionError("an existing map must not be re-drawn")

    r = ride()
    assert render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=unreachable, fetch_tile=fake_tile) is None
    assert r["map_image"] == "maps/ride-1.svg"  # still wired up


def test_render_skips_a_ride_with_no_route(tmp_path):
    r = ride(routes=[])
    assert render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile) is None
    assert "map_image" not in r


def test_render_skips_a_route_without_coordinates(tmp_path):
    r = ride(routes=[{"label": "Route", "url": "x", "start": "A", "end": "B"}])
    assert render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile) is None
    assert "map_image" not in r


def test_render_is_soft_when_the_router_is_down(tmp_path):
    r = ride()
    assert render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=lambda p: [], fetch_tile=fake_tile) is None
    assert "map_image" not in r
    assert not list(tmp_path.iterdir())


def test_render_picks_the_first_route_with_coordinates(tmp_path):
    r = ride(routes=[
        {"label": "No points", "url": "x", "start": "A", "end": "B"},
        {"label": "Has points", "url": "y", "start": "C", "end": "D",
         "points": [[42.0, -71.0], [42.1, -71.1]]},
    ])
    assert render_route_maps.mappable_route(r)["label"] == "Has points"


def test_uids_are_made_filename_safe(tmp_path):
    r = ride(uid="evt-past/../weird uid@partiful.com")
    render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile)
    written = [p.name for p in tmp_path.iterdir()]
    assert written == ["evt-past-weird-uid-partiful.com.svg"]
    # The point of the exercise: no separators, and no traversal.
    assert ".." not in written[0]
    assert "/" not in r["map_image"].split("/")[-1]


def test_a_uid_that_is_all_punctuation_still_gets_a_name(tmp_path):
    r = ride(uid="///...")
    render_route_maps.render_for_ride(r, tmp_path, "maps", fetch=fake_geometry, fetch_tile=fake_tile)
    assert [p.name for p in tmp_path.iterdir()] == ["ride.svg"]


def test_process_rewrites_the_payload(tmp_path):
    payload = tmp_path / "events.json"
    payload.write_text(json.dumps({"count": 1, "events": [ride()]}), encoding="utf-8")
    drawn = render_route_maps.process(payload, tmp_path / "maps", "maps", fetch=fake_geometry, fetch_tile=fake_tile)
    assert drawn == 1
    written = json.loads(payload.read_text(encoding="utf-8"))
    assert written["events"][0]["map_image"] == "maps/ride-1.svg"


def test_process_leaves_the_payload_alone_when_nothing_changed(tmp_path):
    payload = tmp_path / "events.json"
    payload.write_text(json.dumps({"count": 1, "events": [ride(routes=[])]}), encoding="utf-8")
    before = payload.read_text(encoding="utf-8")
    assert render_route_maps.process(payload, tmp_path / "maps", "maps", fetch=fake_geometry, fetch_tile=fake_tile) == 0
    assert payload.read_text(encoding="utf-8") == before
