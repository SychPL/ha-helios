"""Constants and pure helpers for the Helios integration (no Home Assistant imports here; unit-tested directly)."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

DOMAIN = "helios"
PROTOCOL = 2
PROTOCOLS = (1, 2)
PAIRING_TTL_SECONDS = 300
PAIRING_MAX_ATTEMPTS = 5
PAIRING_MAX_SOURCES = 256
PAIR_BODY_LIMIT = 4096
PAIRING_TIMEOUT_SECONDS = 30
FLOW_TIMEOUT_SECONDS = 20
SETUP_TIMEOUT_SECONDS = 10
MA_TIMEOUT_SECONDS = 5
DASHBOARD_PATH = "helios-clock"
COMMAND_TIMEOUT_SECONDS = 10

_INSTALLATION_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_CODE_RE = re.compile(r"^[0-9]{6}$")

COMMANDS: dict[str, dict[str, tuple[int, int]]] = {
    "lamp.turn_on": {},
    "lamp.turn_off": {},
    "lamp.set_brightness": {"level": (1, 10)},
    "audio.set_device_volume": {"percent": (0, 100)},
    "music.play": {},
    "music.pause": {},
    "music.stop": {},
}


def brightness_to_level(brightness: int) -> int:
    """HA brightness 1..255 to the dock's ten levels; zero is turn_off and never reaches here."""
    return max(1, min(10, round(brightness * 10 / 255)))


def level_to_brightness(level: int) -> int:
    return max(1, min(255, round(level * 255 / 10)))


def validate_command(command: str, args: dict) -> str | None:
    """Returns None when the command and its integer arguments are inside the allowlist, else an error code."""
    spec = COMMANDS.get(command)
    if spec is None:
        return "unknown_command"
    for key, (low, high) in spec.items():
        value = args.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < low or value > high:
            return "invalid_args"
    if set(args) - set(spec):
        return "invalid_args"
    return None


def new_domain_data() -> dict:
    """The hass.data[DOMAIN] shape; every entry point builds it the same way."""
    return {"pairing": PairingRegistry(), "entries": {}, "locks": {}, "pairing_active": set(), "cancelled_users": {}, "views_registered": False, "ws_registered": False}


def parse_pair_request(body: object) -> dict | None:
    """The clock's pairing request; None for anything that is not exactly the documented shape (unknown keys ignored)."""
    if not isinstance(body, dict):
        return None
    installation_id, code, app_version, version_code = body.get("installation_id"), body.get("code"), body.get("app_version"), body.get("version_code")
    if not (isinstance(installation_id, str) and _INSTALLATION_RE.fullmatch(installation_id)):  # fullmatch: "$" alone lets a trailing newline through
        return None
    if not (isinstance(code, str) and _CODE_RE.fullmatch(code)):
        return None
    if not (isinstance(app_version, str) and 1 <= len(app_version) <= 32):
        return None
    if not isinstance(version_code, int) or isinstance(version_code, bool) or version_code < 1:
        return None
    return {"installation_id": installation_id, "code": code, "app_version": app_version, "version_code": version_code}


def sendspin_url_for(url: str) -> str:
    """Sendspin lives next to the MA API on port 8927 (SPEC 0.6 / docs/ma-api-2.10.3.md)."""
    if not url:
        return ""
    host = urlsplit(url).hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"ws://{host}:8927/sendspin" if host else ""


class PairingRegistry:
    """One-time pairing codes with expiry; wrong attempts are bounded per source address and never touch the codes."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._codes: dict[str, dict] = {}
        self._sources: dict[str, dict] = {}  # source -> {"attempts": int, "since": float}

    def issue(self, code: str, payload: dict) -> None:
        self._codes[code] = {"payload": payload, "expires": self._clock() + PAIRING_TTL_SECONDS}

    def cancel(self, code: str) -> None:
        self._codes.pop(code, None)

    def _purge(self, now: float) -> None:
        for key in [k for k, v in self._codes.items() if v["expires"] <= now]:
            self._codes.pop(key)
        for key in [k for k, v in self._sources.items() if now - v["since"] >= PAIRING_TTL_SECONDS]:
            self._sources.pop(key)

    def blocked(self, source: str) -> bool:
        now = self._clock()
        self._purge(now)
        entry = self._sources.get(source)
        if entry is not None:
            return entry["attempts"] >= PAIRING_MAX_ATTEMPTS
        return len(self._sources) >= PAIRING_MAX_SOURCES  # a full map: unknown sources wait for the window to purge

    def claim(self, code: str, source: str) -> dict | None:
        """Consumes and returns the pending payload for a valid code from a source under the attempt limit; wrong or expired codes count against the source only."""
        if self.blocked(source):
            return None
        entry = self._codes.get(code)
        if entry is not None:
            return self._codes.pop(code)["payload"]
        record = self._sources.setdefault(source, {"attempts": 0, "since": self._clock()})
        record["attempts"] += 1
        return None

    def pending(self, code: str) -> bool:
        entry = self._codes.get(code)
        return entry is not None and entry["expires"] > self._clock()
