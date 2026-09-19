# Helios protocol

Everything between the clock and Home Assistant: how a clock is paired over
HTTP, and the WebSocket channel it uses afterwards. This document describes
protocol revision **2**; revision 1 (clocks paired before the code flow existed,
with an account token) is still accepted on the channel.

The authoritative specification, in Polish, is `docs/SPEC-0.10-onboarding.md`
in the app repository. This file is the English summary of the wire contract.

## 1. Pairing over HTTP

Pairing exists because a clock that has never been paired holds no credential.
It is the only unauthenticated surface of the integration.

### `GET /api/helios/pair`

A capability probe, no authentication, no side effects.

```json
{ "protocol": 2 }
```

The clock uses it to check that the address the user typed really is a Home
Assistant with the integration installed, before it asks for a code.

### `POST /api/helios/pair`

The transaction. The body must be exactly this shape; unknown keys are ignored,
anything malformed is `400 invalid_request`:

```json
{
  "installation_id": "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69",
  "code": "123456",
  "app_version": "0.9.1",
  "version_code": 29
}
```

`installation_id` is a UUID the app generates once per installation, `code` is
the six digits Home Assistant is showing in the *Add integration* dialog at that
moment.

On success, `200`:

```json
{
  "protocol": 2,
  "token": "<the clock's own long-lived access token>",
  "pipeline": "<assist pipeline id>",
  "dashboard_path": "helios-clock"
}
```

Failures are deliberately uniform and say nothing about why:

| Status | Body | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | the body is not the shape above |
| 401 | `unauthorized` | wrong or expired code, or this address is blocked |
| 409 | `pairing_failed` | another pairing for this installation id is in flight |
| 503 | `not_ready` | no Assist pipeline, or the transaction timed out |

Rules that matter for anyone implementing a client:

- A code is valid for five minutes and can be claimed once.
- Five refused attempts from one source address block that address for five
  minutes. Failed attempts are logged with the source address, never with the
  code.
- The body is read with a 5 s timeout and capped at 4 KiB.
- The whole transaction runs under a lock per installation id, and so does a
  re-pair, so two clocks with the same id cannot interleave.
- The token and the code never appear in the Home Assistant log.

### What Home Assistant creates

A successful pairing creates, in this order: a per-clock Home Assistant user
(system-generated, non-admin, `local_only`, plain users group) with a ten-year
system token; optionally a Music Assistant token for the clock; then the config
entry. Only after the entry is loaded is the previous identity of a re-paired
clock retired. Any failure before that point rolls the new identity back, and
the old clock keeps working.

## 2. The WebSocket channel

The clock authenticates to the normal Home Assistant WebSocket API with its
token and then issues one subscription.

### `helios/connect`

```json
{
  "id": 1,
  "type": "helios/connect",
  "protocol": 2,
  "installation_id": "0f3c1b2a-...",
  "app_version": "0.9.1",
  "version_code": 29,
  "capabilities": ["music"]
}
```

The entry for that installation id must exist and must belong to the connecting
user; otherwise the command fails with `unauthorized`. Errors you can get:

| Code | When |
| --- | --- |
| `unsupported_protocol` | the protocol is not 1 or 2 |
| `unauthorized` | unknown device, a different owner, or the device was re-paired while this connect was waiting |
| `not_ready` | the integration is still loading |

A successful subscribe is followed immediately by three events.

### Events pushed to the clock

All events arrive as `event` payloads of the subscription.

**`connected`** - the device identity in Home Assistant:

```json
{ "type": "connected", "device_id": "...", "area_id": "...", "name": "Bedroom clock" }
```

**`appearance`** - the full appearance snapshot (theme, background, dimming,
focus point, image URL). Sent on connect and again after every options change.

**`connection`** - everything the clock needs to run:

```json
{
  "type": "connection",
  "pipeline": "<assist pipeline id>",
  "dashboard_path": "helios-clock",
  "music_assistant": { "url": "http://ma:8095", "token": "...", "sendspin_url": "ws://ma:8927/sendspin" },
  "diagnostics_url": null
}
```

`music_assistant` is `null` when there is no Music Assistant. A missing key
means "unchanged", an explicit `null` means "drop it".

**`device`** - the Home Assistant device was renamed or moved to another area,
so the clock can follow with its player name and voice context.

**`command`** - a command from Home Assistant:

```json
{ "type": "command", "request_id": "...", "command": "lamp.set_brightness", "args": { "brightness": 7 } }
```

The allowlist is closed: `lamp.turn_on`, `lamp.turn_off`, `lamp.set_brightness`,
`audio.set_device_volume`.

**`replaced`** - another device subscribed with the same installation id. The
clock stops; it does not retry, because it no longer owns the pairing.

**`removed`** - the integration was reloaded or the entry is going away. The
clock retries the subscription on the same socket.

### `helios/state`

Telemetry, pushed by the clock whenever something changes:

```json
{ "id": 2, "type": "helios/state", "state": { "app_version": "0.9.1", "voice_state": "idle", "dock_connected": true, "charging": false, "lamp_on": true, "lamp_brightness": 7, "device_volume": 35 } }
```

Entities stay `unavailable` until the first snapshot arrives. A reported
`app_version` that differs from the stored one updates the device registry.

### `helios/result`

The answer to one `command` event:

```json
{ "id": 3, "type": "helios/result", "request_id": "...", "status": "ok" }
```

`status` is `ok`, `busy` (another command for the same resource is in flight),
or an error code. Home Assistant waits 10 s; a timeout is an error, never a
retry, because the outcome on the device is unknown.

Both `helios/state` and `helios/result` require an active `helios/connect`
subscription on the same connection, otherwise they fail with `unauthorized`.

## 3. Versioning

`PROTOCOL` in `const.py` is the revision this integration speaks, `PROTOCOLS`
the set it accepts. A clock that announces something outside that set is
refused rather than tolerated; the app shows the user that it needs an update.
