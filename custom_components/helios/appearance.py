"""Per-clock appearance (SPEC 0.8b): option validation, background image preparation and the private one-image store.

Pure helpers, importable without Home Assistant so they can be unit-tested; Pillow comes from the HA core requirements.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path

THEMES = ("warm_graphite", "night_blue")
SCREENSAVER_MODES = ("off", "dark", "always")
# SPEC 0.14: the night clock. These defaults reproduce 0.13 exactly, so a clock nobody configures behaves the
# way it does today - black clock in a dark room, ordinary panel in a lit one.
SCREENSAVER_DEFAULTS = {
    "mode": "dark",
    "idle_seconds": 60,
    "dark_enter": 3,
    "dark_exit": 8,
    "photos": False,
    "photo_seconds": 120,
    "photo_dim": 45,
}
IDLE_MIN, IDLE_MAX = 15, 3600
LUX_MIN, LUX_MAX = 0, 100
PHOTO_SECONDS_MIN, PHOTO_SECONDS_MAX = 15, 3600
PHOTO_DIM_MIN, PHOTO_DIM_MAX = 0, 90
DEFAULT_APPEARANCE = {"version": 1, "theme": "warm_graphite", "background": {"type": "solid"}}
DIM_MIN, DIM_MAX, DIM_DEFAULT = 35, 80, 50
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_SIDE = 8192
BOUNDS = (1600, 960)
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
FLATTEN_COLOR = (0x18, 0x1C, 0x24)
IMAGE_ID_RE = re.compile(r"^[a-f0-9]{64}$")
PATH_PREFIX = "/api/helios/appearance/"


def image_path(entry_id: str, image_id: str) -> str:
    return f"{PATH_PREFIX}{entry_id}/{image_id}"


def validate_appearance(value: object) -> dict:
    """Strict, whole-object validation; raises ValueError with a short reason. Unknown keys are rejected."""
    if not isinstance(value, dict):
        raise ValueError("appearance must be an object")
    keys = set(value)
    if keys not in ({"version", "theme", "background"}, {"version", "theme", "background", "screensaver"}):
        raise ValueError("appearance keys must be version, theme, background and optionally screensaver")
    if value["version"] not in (1, 2):
        raise ValueError("unsupported appearance version")
    if "screensaver" in keys and value["version"] != 2:
        raise ValueError("screensaver needs appearance version 2")
    if value["theme"] not in THEMES:
        raise ValueError("unknown theme")
    background = value["background"]
    if not isinstance(background, dict) or background.get("type") not in ("solid", "image"):
        raise ValueError("background.type must be solid or image")
    screensaver = validate_screensaver(value["screensaver"]) if "screensaver" in keys else None
    if background["type"] == "solid":
        if set(background) != {"type"}:
            raise ValueError("solid background takes no other fields")
        return _assembled(value["version"], value["theme"], {"type": "solid"}, screensaver)
    if set(background) != {"type", "image_id", "path", "dim", "focus_x", "focus_y"}:
        raise ValueError("image background needs image_id, path, dim, focus_x, focus_y")
    image_id = background["image_id"]
    if not isinstance(image_id, str) or not IMAGE_ID_RE.match(image_id):
        raise ValueError("image_id must be 64 lowercase hex characters")
    path = background["path"]
    if not isinstance(path, str) or not path.startswith(PATH_PREFIX) or ".." in path or not path.endswith("/" + image_id):
        raise ValueError("path must be /api/helios/appearance/<entry_id>/<image_id>")
    entry_id = path[len(PATH_PREFIX) : -(len(image_id) + 1)]
    if not re.match(r"^[A-Za-z0-9_-]+$", entry_id):
        raise ValueError("path entry id is malformed")
    out = {"type": "image", "image_id": image_id, "path": path}
    for key, low, high in (("dim", DIM_MIN, DIM_MAX), ("focus_x", 0, 100), ("focus_y", 0, 100)):
        number = background[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f"{key} must be an integer in {low}-{high}")
        out[key] = number
    return _assembled(value["version"], value["theme"], out, screensaver)


def _assembled(version: int, theme: str, background: dict, screensaver: dict | None) -> dict:
    result = {"version": version, "theme": theme, "background": background}
    if screensaver is not None:
        result["screensaver"] = screensaver
    return result


def validate_screensaver(value: object) -> dict:
    """Strict like the rest of the contract: the clock is the one allowed to be lenient, not the sender."""
    if not isinstance(value, dict):
        raise ValueError("screensaver must be an object")
    if set(value) != set(SCREENSAVER_DEFAULTS):
        raise ValueError("screensaver keys must be " + ", ".join(sorted(SCREENSAVER_DEFAULTS)))
    if value["mode"] not in SCREENSAVER_MODES:
        raise ValueError("screensaver.mode must be off, dark or always")
    if not isinstance(value["photos"], bool):
        raise ValueError("screensaver.photos must be true or false")
    out = {"mode": value["mode"], "photos": value["photos"]}
    for key, low, high in (
        ("idle_seconds", IDLE_MIN, IDLE_MAX),
        ("dark_enter", LUX_MIN, LUX_MAX),
        ("dark_exit", LUX_MIN, LUX_MAX),
        ("photo_seconds", PHOTO_SECONDS_MIN, PHOTO_SECONDS_MAX),
        ("photo_dim", PHOTO_DIM_MIN, PHOTO_DIM_MAX),
    ):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f"screensaver.{key} must be an integer in {low}-{high}")
        out[key] = number
    # the two thresholds are a pair with hysteresis between them, so they are checked as a pair
    if out["dark_exit"] <= out["dark_enter"]:
        raise ValueError("screensaver.dark_exit must be greater than dark_enter")
    return out


def snapshot(entry_id: str, options: dict) -> dict:
    """The full appearance event payload for one entry; missing or invalid options mean the explicit defaults."""
    try:
        return validate_appearance(options.get("appearance", DEFAULT_APPEARANCE))
    except ValueError:
        return dict(DEFAULT_APPEARANCE)


def image_id_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_image(path: str | os.PathLike) -> bytes:
    """Validate and normalise an uploaded JPEG/PNG into a metadata-free JPEG within BOUNDS and MAX_OUTPUT_BYTES.

    Runs in an executor. Raises ValueError with a user-facing reason; the caller keeps the previous image on error.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    path = Path(path)
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("file_too_large")
    try:
        with Image.open(path) as source:
            if source.format not in ("JPEG", "PNG"):
                raise ValueError("unsupported_format")
            if getattr(source, "is_animated", False) and getattr(source, "n_frames", 1) > 1:
                raise ValueError("animated_image")
            width, height = source.size
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise ValueError("image_too_large")
            source.load()  # a truncated or corrupt file fails here, before any conversion
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in ("RGBA", "LA", "P"):
                rgba = oriented.convert("RGBA")
                flat = Image.new("RGB", rgba.size, FLATTEN_COLOR)
                flat.paste(rgba, mask=rgba.getchannel("A"))
                rgb = flat
            else:
                rgb = oriented.convert("RGB")
    except UnidentifiedImageError as err:
        raise ValueError("unsupported_format") from err
    except OSError as err:
        raise ValueError("corrupt_image") from err
    # a fresh image built from pixels: no EXIF, no GPS, no ICC from the original
    clean = Image.new("RGB", rgb.size)
    clean.putdata(list(rgb.getdata()))
    clean.thumbnail(BOUNDS, Image.LANCZOS)
    for quality in (85, 80, 75, 70, 65, 60):
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_OUTPUT_BYTES:
            return out.getvalue()
    raise ValueError("image_too_large")


def write_image(directory: str | os.PathLike, image_id: str, data: bytes) -> Path:
    """Atomic write of <image_id>.jpg (tmp + rename); a new id never overwrites an existing file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{image_id}.jpg"
    if target.exists():
        return target
    tmp = directory / f".{image_id}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def prune(directory: str | os.PathLike, keep: set[str]) -> None:
    directory = Path(directory)
    if not directory.is_dir():
        return
    for file in directory.iterdir():
        if file.suffix == ".jpg" and file.stem not in keep:
            file.unlink(missing_ok=True)
        elif file.suffix == ".tmp":
            file.unlink(missing_ok=True)


def repair_storage(directory: str | os.PathLike, options: dict) -> dict:
    """Startup self-heal of the file/options pair after an interrupted save (plan B1).

    Returns the options to persist: unchanged when the referenced file exists; otherwise the newest remaining
    file is adopted (the user did upload it), and with no file at all the image is dropped and an image
    background falls back to solid.
    """
    directory = Path(directory)
    options = dict(options)
    current = options.get("image_id")
    files = sorted(directory.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True) if directory.is_dir() else []
    ids = [f.stem for f in files if IMAGE_ID_RE.match(f.stem)]
    if current in ids:
        prune(directory, {current})
        return options
    appearance = dict(options.get("appearance", DEFAULT_APPEARANCE))
    background = dict(appearance.get("background", {}))
    if ids:
        adopted = ids[0]
        options["image_id"] = adopted
        if background.get("type") == "image":
            background["image_id"] = adopted
            entry_id = background.get("path", PATH_PREFIX + "x/x")[len(PATH_PREFIX) :].split("/")[0]
            background["path"] = image_path(entry_id, adopted)
            appearance["background"] = background
            options["appearance"] = appearance
        prune(directory, {adopted})
        return options
    options.pop("image_id", None)
    if background.get("type") == "image":
        appearance["background"] = {"type": "solid"}
        options["appearance"] = appearance
    return options
