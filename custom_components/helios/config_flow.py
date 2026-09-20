"""Pairing flow: HA shows a one-time code, the user types it on the clock, the clock's helios/connect completes the flow."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import appearance as ap
from . import websocket
from .const import DOMAIN, PAIRING_TTL_SECONDS, PairingRegistry, new_domain_data
from .http import async_register_views


class HeliosConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._code: str | None = None
        self._future: asyncio.Future | None = None
        self._done: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._timed_out = False
        self._pending: dict | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HeliosOptionsFlow:
        return HeliosOptionsFlow()

    @property
    def _registry(self) -> PairingRegistry:
        return self.hass.data.setdefault(DOMAIN, new_domain_data())["pairing"]

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        # First pairing happens before any config entry exists, so async_setup has not run yet:
        # the helios/* websocket commands and the pairing endpoint must be available for the clock right now.
        data = self.hass.data.setdefault(DOMAIN, new_domain_data())
        if not data.get("ws_registered"):
            websocket.async_register(self.hass)
            data["ws_registered"] = True
        async_register_views(self.hass)
        if self._code is None:
            self._code = f"{secrets.randbelow(10**6):06d}"
            self._future = self.hass.loop.create_future()
            self._done = asyncio.Event()
            self._pending = {"future": self._future, "done": self._done, "flow_id": self.flow_id, "cancelled": False, "existing": None}
            self._registry.issue(self._code, self._pending)
            self._task = self.hass.async_create_task(self._wait_for_clock())
        if not self._task.done():
            return self.async_show_progress(
                step_id="user",
                progress_action="pair",
                description_placeholders={"code": self._code, "minutes": str(PAIRING_TTL_SECONDS // 60)},
                progress_task=self._task,
            )
        return self.async_show_progress_done(next_step_id="finish")

    async def _wait_for_clock(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._future), PAIRING_TTL_SECONDS)
        except TimeoutError:
            self._timed_out = True

    async def async_step_finish(self, user_input=None) -> ConfigFlowResult:
        """The pairing endpoint did the work (SPEC 0.10 pkt 4.1): a new entry is created here, an existing one was updated by the endpoint."""
        self._registry.cancel(self._code)
        if self._timed_out or self._future is None or not self._future.done() or (self._pending or {}).get("cancelled"):
            self._release()
            return self.async_abort(reason="timeout")
        data = self._future.result()
        if data.get("reconfigured"):
            self._release()
            return self.async_abort(reason="reconfigured")
        await self.async_set_unique_id(data["installation_id"])
        self._abort_if_unique_id_configured()
        result = self.async_create_entry(title=f"Helios {data['installation_id'][:8]}", data=data)
        self._release()
        return result

    @callback
    def _release(self) -> None:
        """Lets the waiting helios/connect proceed once the entry exists (or the flow gave up)."""
        if self._done is not None:
            self._done.set()

    @callback
    def async_remove(self) -> None:
        if self._code is not None:
            self._registry.cancel(self._code)
        self._release()


class HeliosOptionsFlow(OptionsFlow):
    """Appearance per clock (SPEC 0.8b): theme, background colour or photo, dim and focus. Plain OptionsFlow: no reload."""

    def __init__(self) -> None:
        self._draft: dict | None = None

    def _dir(self) -> Path:
        return Path(self.hass.config.path("helios", self.config_entry.entry_id))

    def _has_image(self) -> bool:
        image_id = self.config_entry.options.get("image_id")
        return bool(image_id) and (self._dir() / f"{image_id}.jpg").is_file()

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        current = ap.snapshot(self.config_entry.entry_id, self.config_entry.options)
        background = current["background"]
        saver = current.get("screensaver", ap.SCREENSAVER_DEFAULTS)
        errors: dict[str, str] = {}
        if user_input is not None:
            self._draft = user_input
            if int(user_input.get("dark_exit", 0)) <= int(user_input.get("dark_enter", 0)):
                errors["dark_exit"] = "dark_exit_too_low"  # the gap between the two is the hysteresis
            if user_input["background"] == "upload":
                return await self.async_step_upload()
            music = (user_input.get("music_url") or "").strip()
            if music and not music.startswith(("http://", "https://")):
                errors["music_url"] = "invalid_url"
            sendspin = (user_input.get("sendspin_url") or "").strip()
            diagnostics = (user_input.get("diagnostics_url") or "").strip()
            if sendspin and not sendspin.startswith(("ws://", "wss://")):
                errors["sendspin_url"] = "invalid_url"
            if diagnostics and not diagnostics.startswith(("http://", "https://")):
                errors["diagnostics_url"] = "invalid_url"
            if user_input["background"] == "keep" and not self._has_image():
                errors["background"] = "no_image"
            elif not errors:
                return await self._save(None)
        has_image = self._has_image()
        choices = ["solid"] + (["keep"] if has_image else []) + ["upload"]
        default_background = "keep" if background["type"] == "image" and has_image else "solid"
        schema = vol.Schema(
            {
                vol.Required("theme", default=current["theme"]): SelectSelector(
                    SelectSelectorConfig(options=list(ap.THEMES), mode=SelectSelectorMode.DROPDOWN, translation_key="theme")
                ),
                vol.Required("background", default=default_background): SelectSelector(
                    SelectSelectorConfig(options=choices, mode=SelectSelectorMode.LIST, translation_key="background")
                ),
                vol.Required("dim", default=background.get("dim", ap.DIM_DEFAULT)): NumberSelector(
                    NumberSelectorConfig(min=ap.DIM_MIN, max=ap.DIM_MAX, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required("focus_x", default=background.get("focus_x", 50)): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Required("focus_y", default=background.get("focus_y", 50)): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
                vol.Optional("music_token", default=self.config_entry.options.get("music_token", "")): TextSelector(  # SPEC 0.10 pkt 6.2: empty = the integration mints one (an MA add-on does not allow that)
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional("music_url", default=self.config_entry.options.get("music_url", "")): str,  # SPEC 0.10 pkt 6.1: empty = the address Music Assistant reports
                vol.Optional("sendspin_url", default=self.config_entry.options.get("sendspin_url", "")): str,  # SPEC 0.10 pkt 6.2: empty = derived from the MA url
                vol.Optional("diagnostics_url", default=self.config_entry.options.get("diagnostics_url", "")): str,  # empty = no diagnostics sink
                # SPEC 0.14: off = never, dark = only a dark room (the 0.13 behaviour), always = after any quiet spell
                vol.Required("screensaver_mode", default=saver["mode"]): SelectSelector(
                    SelectSelectorConfig(options=list(ap.SCREENSAVER_MODES), mode=SelectSelectorMode.DROPDOWN, translation_key="screensaver_mode")
                ),
                vol.Required("idle_seconds", default=saver["idle_seconds"]): NumberSelector(
                    NumberSelectorConfig(min=ap.IDLE_MIN, max=ap.IDLE_MAX, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
                vol.Required("dark_enter", default=saver["dark_enter"]): NumberSelector(
                    NumberSelectorConfig(min=ap.LUX_MIN, max=ap.LUX_MAX, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="lx")
                ),
                vol.Required("dark_exit", default=saver["dark_exit"]): NumberSelector(
                    NumberSelectorConfig(min=ap.LUX_MIN, max=ap.LUX_MAX, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="lx")
                ),
                vol.Required("photos", default=saver["photos"]): bool,  # the slideshow only ever runs in a lit room
                vol.Required("photo_seconds", default=saver["photo_seconds"]): NumberSelector(
                    NumberSelectorConfig(min=ap.PHOTO_SECONDS_MIN, max=ap.PHOTO_SECONDS_MAX, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
                vol.Required("photo_dim", default=saver["photo_dim"]): NumberSelector(
                    NumberSelectorConfig(min=ap.PHOTO_DIM_MIN, max=ap.PHOTO_DIM_MAX, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_upload(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await self.hass.async_add_executor_job(self._prepare_upload, user_input["file"])
            except ValueError as err:
                errors["file"] = str(err) if str(err) in ("file_too_large", "unsupported_format", "animated_image", "image_too_large", "corrupt_image") else "corrupt_image"
            else:
                return await self._save(data)
        schema = vol.Schema({vol.Required("file"): FileSelector(FileSelectorConfig(accept="image/jpeg,image/png"))})
        return self.async_show_form(step_id="upload", data_schema=schema, errors=errors)

    def _prepare_upload(self, file_id: str) -> bytes:
        """Executor: the temporary upload is consumed here and never kept; only the normalised JPEG survives."""
        with process_uploaded_file(self.hass, file_id) as path:
            return ap.prepare_image(path)

    async def _save(self, data: bytes | None) -> ConfigFlowResult:
        entry = self.config_entry
        locks = self.hass.data.setdefault(DOMAIN, new_domain_data())["locks"]
        lock = locks.setdefault(entry.entry_id, asyncio.Lock())
        async with lock:
            options = dict(entry.options)
            previous = options.get("image_id")
            if data is not None:
                new_id = ap.image_id_of(data)
                await self.hass.async_add_executor_job(ap.write_image, self._dir(), new_id, data)
                options["image_id"] = new_id
            draft = self._draft or {}
            options["music_token"] = (draft.get("music_token") or "").strip()
            options["music_url"] = (draft.get("music_url") or "").strip()
            options["sendspin_url"] = (draft.get("sendspin_url") or "").strip()
            options["diagnostics_url"] = (draft.get("diagnostics_url") or "").strip()
            solid = draft.get("background", "solid") == "solid"
            if solid:
                background = {"type": "solid"}
            else:
                background = {
                    "type": "image",
                    "image_id": options["image_id"],
                    "path": ap.image_path(entry.entry_id, options["image_id"]),
                    "dim": int(round(float(draft.get("dim", ap.DIM_DEFAULT)))),
                    "focus_x": int(round(float(draft.get("focus_x", 50)))),
                    "focus_y": int(round(float(draft.get("focus_y", 50)))),
                }
            screensaver = {
                "mode": draft.get("screensaver_mode", ap.SCREENSAVER_DEFAULTS["mode"]),
                "idle_seconds": int(round(float(draft.get("idle_seconds", ap.SCREENSAVER_DEFAULTS["idle_seconds"])))),
                "dark_enter": int(round(float(draft.get("dark_enter", ap.SCREENSAVER_DEFAULTS["dark_enter"])))),
                "dark_exit": int(round(float(draft.get("dark_exit", ap.SCREENSAVER_DEFAULTS["dark_exit"])))),
                "photos": bool(draft.get("photos", ap.SCREENSAVER_DEFAULTS["photos"])),
                "photo_seconds": int(round(float(draft.get("photo_seconds", ap.SCREENSAVER_DEFAULTS["photo_seconds"])))),
                "photo_dim": int(round(float(draft.get("photo_dim", ap.SCREENSAVER_DEFAULTS["photo_dim"])))),
            }
            options["appearance"] = ap.validate_appearance(
                {"version": 2, "theme": draft.get("theme", "warm_graphite"), "background": background, "screensaver": screensaver}
            )
            result = self.async_create_entry(title="", data=options)
            if data is not None:
                # the previous file stays until the next upload: HA persists entries with a delay, so a crash in between
                # still leaves a pair the startup repair can resolve to what was actually saved
                keep = {options["image_id"]} | ({previous} if previous else set())
                await self.hass.async_add_executor_job(ap.prune, self._dir(), keep)
            return result
