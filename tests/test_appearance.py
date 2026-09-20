"""Appearance helpers (SPEC 0.8b): validation, image normalisation, storage repair. `pytest tests`, needs Pillow, no Home Assistant."""

import importlib.util
import io
import pathlib
import time

import pytest
from PIL import Image

_spec = importlib.util.spec_from_file_location(
    "helios_appearance", pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "helios" / "appearance.py"
)
appearance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(appearance)

ID = "a" * 64
PATH = f"/api/helios/appearance/entry1/{ID}"


def image_background(**overrides):
    background = {"type": "image", "image_id": ID, "path": PATH, "dim": 50, "focus_x": 50, "focus_y": 50}
    background.update(overrides)
    return {"version": 1, "theme": "night_blue", "background": background}


def test_validation_accepts_both_shapes_and_rejects_everything_else():
    assert appearance.validate_appearance(appearance.DEFAULT_APPEARANCE) == appearance.DEFAULT_APPEARANCE
    assert appearance.validate_appearance(image_background())["background"]["dim"] == 50
    for bad in (
        {"version": 3, "theme": "warm_graphite", "background": {"type": "solid"}},
        # the screensaver block belongs to version 2 only (SPEC 0.14)
        {"version": 1, "theme": "warm_graphite", "background": {"type": "solid"}, "screensaver": appearance.SCREENSAVER_DEFAULTS},
        {"version": 1, "theme": "neon", "background": {"type": "solid"}},
        {"version": 1, "theme": "warm_graphite", "background": {"type": "solid", "dim": 50}},
        {"version": 1, "theme": "warm_graphite", "background": {"type": "solid"}, "extra": 1},
        image_background(dim=34),
        image_background(dim=81),
        image_background(dim=50.0),
        image_background(dim=True),
        image_background(focus_x=101),
        image_background(focus_y=-1),
        image_background(image_id="A" * 64),
        image_background(image_id="b" * 64),  # path no longer ends with the id
        image_background(path=f"/api/helios/appearance/../{ID}"),
        image_background(path=f"/local/{ID}"),
        "solid",
    ):
        with pytest.raises(ValueError):
            appearance.validate_appearance(bad)
    assert appearance.snapshot("e", {}) == appearance.DEFAULT_APPEARANCE
    assert appearance.snapshot("e", {"appearance": {"version": 9}}) == appearance.DEFAULT_APPEARANCE


def test_screensaver_block_is_validated_as_a_whole():
    good = {"version": 2, "theme": "warm_graphite", "background": {"type": "solid"}, "screensaver": dict(appearance.SCREENSAVER_DEFAULTS)}
    assert appearance.validate_appearance(good)["screensaver"] == appearance.SCREENSAVER_DEFAULTS

    def saver(**changes):
        block = dict(appearance.SCREENSAVER_DEFAULTS)
        block.update(changes)
        return {"version": 2, "theme": "warm_graphite", "background": {"type": "solid"}, "screensaver": block}

    for bad in (
        saver(mode="night"),
        saver(idle_seconds=5),           # shorter than the guarantee the clock owes after a touch
        saver(idle_seconds=10_000),
        saver(idle_seconds=60.0),
        saver(dark_enter=8, dark_exit=3),  # the pair is checked as a pair, not field by field
        saver(dark_enter=5, dark_exit=5),
        saver(dark_exit=200),
        saver(photos="yes"),
        saver(photo_seconds=5),
        saver(photo_dim=95),
        {"version": 2, "theme": "warm_graphite", "background": {"type": "solid"}, "screensaver": {"mode": "dark"}},
        {"version": 2, "theme": "warm_graphite", "background": {"type": "solid"}, "screensaver": "dark"},
    ):
        with pytest.raises(ValueError):
            appearance.validate_appearance(bad)


def _png_with_alpha(size=(3000, 2000)):
    img = Image.new("RGBA", size, (255, 0, 0, 0))  # fully transparent red: flattening must yield the theme background
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _jpeg_with_exif_rotation(tmp_path, size=(400, 200)):
    img = Image.new("RGB", size, (0, 128, 255))
    exif = img.getexif()
    exif[0x0112] = 6  # orientation: rotate 90 CW on display
    exif[0x010F] = "Camera Co"  # maker tag: must not survive
    path = tmp_path / "rot.jpg"
    img.save(path, format="JPEG", exif=exif.tobytes())
    return path


def test_prepared_image_is_bounded_flattened_oriented_and_metadata_free(tmp_path):
    png = tmp_path / "alpha.png"
    png.write_bytes(_png_with_alpha())
    data = appearance.prepare_image(png)
    assert len(data) <= appearance.MAX_OUTPUT_BYTES
    with Image.open(io.BytesIO(data)) as out:
        assert out.format == "JPEG" and out.mode == "RGB"
        assert out.size[0] <= 1600 and out.size[1] <= 960 and abs(out.size[0] / out.size[1] - 1.5) < 0.01
        r, g, b = out.getpixel((10, 10))
        assert abs(r - 0x18) < 8 and abs(g - 0x1C) < 8 and abs(b - 0x24) < 8
        assert not out.getexif()
    rotated = appearance.prepare_image(_jpeg_with_exif_rotation(tmp_path))
    with Image.open(io.BytesIO(rotated)) as out:
        assert out.size == (200, 400)
        assert not out.getexif()
    assert appearance.image_id_of(data) == appearance.image_id_of(data) and len(appearance.image_id_of(data)) == 64


def test_prepare_rejects_wrong_format_oversize_and_corrupt_files(tmp_path):
    gif = tmp_path / "a.gif"
    Image.new("RGB", (10, 10)).save(gif, format="GIF")
    with pytest.raises(ValueError, match="unsupported_format"):
        appearance.prepare_image(gif)
    wide = tmp_path / "wide.png"
    Image.new("RGB", (9000, 100)).save(wide, format="PNG")
    with pytest.raises(ValueError, match="image_too_large"):
        appearance.prepare_image(wide)
    corrupt = tmp_path / "c.jpg"
    corrupt.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    with pytest.raises(ValueError):
        appearance.prepare_image(corrupt)
    text = tmp_path / "t.png"
    text.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="unsupported_format"):
        appearance.prepare_image(text)


def test_storage_repair_keeps_a_consistent_pair_after_any_interruption(tmp_path):
    a, b = "a" * 64, "b" * 64
    appearance.write_image(tmp_path, a, b"A")
    time.sleep(0.02)
    appearance.write_image(tmp_path, b, b"B")
    options = {"image_id": a, "appearance": image_background(image_id=a, path=appearance.image_path("entry1", a))}
    # options persisted with A, the newer B never made it into the options: A stays, B is pruned
    assert appearance.repair_storage(tmp_path, options) == options
    assert sorted(f.stem for f in tmp_path.glob("*.jpg")) == [a]
    # options say A but only B is on disk: B is adopted and the appearance follows
    appearance.write_image(tmp_path, b, b"B")
    (tmp_path / f"{a}.jpg").unlink()
    repaired = appearance.repair_storage(tmp_path, options)
    assert repaired["image_id"] == b and repaired["appearance"]["background"]["image_id"] == b
    assert repaired["appearance"]["background"]["path"] == appearance.image_path("entry1", b)
    appearance.validate_appearance(repaired["appearance"])
    # nothing on disk: the image is dropped and the background becomes solid
    (tmp_path / f"{b}.jpg").unlink()
    repaired = appearance.repair_storage(tmp_path, options)
    assert "image_id" not in repaired and repaired["appearance"]["background"] == {"type": "solid"}
    # the same id is never rewritten and a tmp leftover is pruned
    (tmp_path / ".x.tmp").write_bytes(b"x")
    appearance.write_image(tmp_path, a, b"A")
    appearance.write_image(tmp_path, a, b"changed")
    assert (tmp_path / f"{a}.jpg").read_bytes() == b"A"
    appearance.prune(tmp_path, {a})
    assert not (tmp_path / ".x.tmp").exists()
