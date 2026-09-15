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
)

from . import appearance as ap
from . import websocket
from .const import DOMAIN, PAIRING_TTL_SECONDS, PairingRegistry


class HeliosConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._code: str | None = None
        self._future: asyncio.Future | None = None
        self._done: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._timed_out = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HeliosOptionsFlow:
        return HeliosOptionsFlow()

    @property
    def _registry(self) -> PairingRegistry:
        return self.hass.data.setdefault(DOMAIN, {"pairing": PairingRegistry(), "entries": {}})["pairing"]

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        # First pairing happens before any config entry exists, so async_setup has not run yet:
        # the helios/* websocket commands must be available for the clock right now.
        data = self.hass.data.setdefault(DOMAIN, {"pairing": PairingRegistry(), "entries": {}})
        if not data.get("ws_registered"):
            websocket.async_register(self.hass)
            data["ws_registered"] = True
        if self._code is None:
            self._code = f"{secrets.randbelow(10**6):06d}"
            self._future = self.hass.loop.create_future()
            self._done = asyncio.Event()
            self._registry.issue(self._code, {"future": self._future, "done": self._done})
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
        self._registry.cancel(self._code)
        if self._timed_out or self._future is None or not self._future.done():
            self._release()
            return self.async_abort(reason="timeout")
        data = self._future.result()
        await self.async_set_unique_id(data["installation_id"])
        existing = self._async_current_entries()
        for entry in existing:
            if entry.unique_id == data["installation_id"]:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data})
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                self._release()
                return self.async_abort(reason="reconfigured")
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
        errors: dict[str, str] = {}
        if user_input is not None:
            self._draft = user_input
            if user_input["background"] == "upload":
                return await self.async_step_upload()
            if user_input["background"] == "keep" and not self._has_image():
                errors["background"] = "no_image"
            else:
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
        locks = self.hass.data.setdefault(DOMAIN, {}).setdefault("locks", {})
        lock = locks.setdefault(entry.entry_id, asyncio.Lock())
        async with lock:
            options = dict(entry.options)
            previous = options.get("image_id")
            if data is not None:
                new_id = ap.image_id_of(data)
                await self.hass.async_add_executor_job(ap.write_image, self._dir(), new_id, data)
                options["image_id"] = new_id
            draft = self._draft or {}
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
            options["appearance"] = ap.validate_appearance({"version": 1, "theme": draft.get("theme", "warm_graphite"), "background": background})
            result = self.async_create_entry(title="", data=options)
            if data is not None:
                # the previous file stays until the next upload: HA persists entries with a delay, so a crash in between
                # still leaves a pair the startup repair can resolve to what was actually saved
                keep = {options["image_id"]} | ({previous} if previous else set())
                await self.hass.async_add_executor_job(ap.prune, self._dir(), keep)
            return result
