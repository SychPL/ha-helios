"""Pure-helper tests for the Helios integration; run with `pytest tests` (no Home Assistant needed)."""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location("helios_const", pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "helios" / "const.py")
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)


def test_brightness_mapping_round_trips_and_never_yields_level_zero():
    assert const.brightness_to_level(1) == 1
    assert const.brightness_to_level(13) == 1
    assert const.brightness_to_level(128) == 5
    assert const.brightness_to_level(255) == 10
    assert const.level_to_brightness(10) == 255
    assert const.level_to_brightness(1) == 26
    for level in range(1, 11):
        assert const.brightness_to_level(const.level_to_brightness(level)) == level


def test_command_validation_is_an_allowlist_with_typed_ranges():
    assert const.validate_command("lamp.turn_on", {}) is None
    assert const.validate_command("lamp.set_brightness", {"level": 10}) is None
    assert const.validate_command("audio.set_device_volume", {"percent": 0}) is None
    assert const.validate_command("lamp.set_brightness", {"level": 11}) == "invalid_args"
    assert const.validate_command("lamp.set_brightness", {"level": "5"}) == "invalid_args"
    assert const.validate_command("lamp.set_brightness", {"level": True}) == "invalid_args"
    assert const.validate_command("lamp.set_brightness", {}) == "invalid_args"
    assert const.validate_command("lamp.turn_on", {"extra": 1}) == "invalid_args"
    assert const.validate_command("music.stop", {}) is None
    assert const.validate_command("music.stop", {"force": True}) == "invalid_args"
    assert const.validate_command("shell.exec", {"cmd": "rm"}) == "unknown_command"


INSTALLATION = "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69"


def test_pair_request_is_strictly_validated():
    good = {"installation_id": INSTALLATION, "code": "123456", "app_version": "0.9.0", "version_code": 28}
    assert const.parse_pair_request(good) == good
    assert const.parse_pair_request({**good, "extra": 1}) == good, "unknown keys are ignored, not rejected"
    for bad in (
        None, [], "x",
        {**good, "code": "12345"}, {**good, "code": "12345a"}, {**good, "code": 123456}, {**good, "code": "123456\n"}, {**good, "installation_id": INSTALLATION + "\n"},
        {**good, "installation_id": "short"}, {**good, "installation_id": "x" * 65}, {**good, "installation_id": "bad/id"},
        {**good, "app_version": "v" * 33}, {**good, "app_version": 1},
        {**good, "version_code": 0}, {**good, "version_code": "28"}, {**good, "version_code": True},
        {k: v for k, v in good.items() if k != "code"},
    ):
        assert const.parse_pair_request(bad) is None, bad


def test_wrong_attempts_are_counted_per_source_and_never_retire_codes():
    now = [1000.0]
    registry = const.PairingRegistry(clock=lambda: now[0])
    registry.issue("123456", {"flow": "a"})
    for _ in range(const.PAIRING_MAX_ATTEMPTS):
        assert registry.claim("000000", "10.0.0.5") is None
    assert registry.pending("123456"), "a code survives any number of wrong attempts"
    assert registry.blocked("10.0.0.5")
    assert registry.claim("123456", "10.0.0.5") is None, "a blocked source is refused even with the right code"
    assert registry.pending("123456")
    assert not registry.blocked("10.0.0.6")
    assert registry.claim("123456", "10.0.0.6") == {"flow": "a"}
    assert registry.claim("123456", "10.0.0.6") is None, "single use"
    now[0] += const.PAIRING_TTL_SECONDS + 1
    assert not registry.blocked("10.0.0.5"), "the block expires with the window"


def test_source_map_is_purged_and_bounded():
    now = [0.0]
    registry = const.PairingRegistry(clock=lambda: now[0])
    for i in range(const.PAIRING_MAX_SOURCES):
        registry.claim("000000", f"10.1.{i // 256}.{i % 256}")
    assert registry.blocked("10.9.9.9"), "a full map treats unknown sources as blocked"
    now[0] += const.PAIRING_TTL_SECONDS + 1
    assert not registry.blocked("10.9.9.9"), "expired sources are purged lazily"
    registry.claim("000000", "10.9.9.9")
    assert not registry.blocked("10.9.9.9")


def test_expired_codes_are_still_refused():
    now = [0.0]
    registry = const.PairingRegistry(clock=lambda: now[0])
    registry.issue("654321", {"flow": "b"})
    now[0] += const.PAIRING_TTL_SECONDS
    assert registry.claim("654321", "10.0.0.1") is None, "exactly at expiry the code is already dead"
    registry.issue("111111", {"flow": "c"})
    now[0] += const.PAIRING_TTL_SECONDS - 1
    assert registry.claim("111111", "10.0.0.1") == {"flow": "c"}


def test_sendspin_url_is_derived_from_the_ma_host():
    assert const.sendspin_url_for("http://192.168.1.50:8095") == "ws://192.168.1.50:8927/sendspin"
    assert const.sendspin_url_for("https://ma.local/") == "ws://ma.local:8927/sendspin"
    assert const.sendspin_url_for("http://[fd00::5]:8095") == "ws://[fd00::5]:8927/sendspin"
    assert const.sendspin_url_for("") == ""
