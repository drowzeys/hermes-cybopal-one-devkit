"""A small client for the CyboPal ONE DevKit's HTTP contract (standard library only).

The DevKit's live simulator serves this contract today (``/api/v1/...`` on port 5000); the DevKit keeps the same paths
for the physical CyboPal ONE, so the plugin moves to the hardware by changing ``device_url`` and nothing else.

Safety: the client talks only to ``device_url``. It never follows a redirect and ignores proxy settings from the
environment, so a motion command always reaches the address the person set and no other.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:5000"
DEVKIT_REPO = "https://github.com/drowzeys/keys-CyboPal-ONE-Hermes-DevKit-LiveSimulator-Nemotron3Nano-Omni-for-DGX-Spark"
GET_THE_SIMULATOR = (f"the CyboPal ONE DevKit's live simulator is free and open source: get it from {DEVKIT_REPO} and "
                     "start it with ./oneshot.sh (or: docker run --rm --network host "
                     "ghcr.io/drowzeys/keys-cybopal-one-hermes-devkit:0.1.0), then open http://127.0.0.1:5000/")

# Joint limits of the DevKit's CyboPal ONE (the device reports its own at /api/v1/limits; these are the fallback).
LIMITS = {
    "pan": (-55.0, 55.0),
    "tilt": (-35.0, 35.0),
    "height_mm": (80.0, 420.0),
    "reach_mm": (80.0, 700.0),
    "roll": (-18.0, 18.0),
    "elbow": (-40.0, 25.0),
}
JOINTS = tuple(LIMITS)
MARKDOWN_MAX = 8000
PAULI_MAX = 80
JPEG_MAX = 8 * 2**20


class DeviceError(Exception):
    """Something the person can act on: the device is not running, refused the command, or is E-stopped."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None  # a 3xx surfaces as an HTTPError instead of being followed


# no redirects, and no proxies from the environment: motion commands go straight to device_url
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class Device:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 8.0) -> None:
        url = (url or DEFAULT_URL).strip().rstrip("/")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password \
                or parts.query or parts.fragment:
            raise DeviceError(f"device_url must be an http(s) address like {DEFAULT_URL}, not {url!r}")
        self.url, self.timeout = url, timeout

    # ------------------------------------------------------------------------------------------------ transport
    def _open(self, method: str, path: str, body=None, accept: str = "application/json"):
        data = json.dumps(body).encode() if body is not None else None
        head = {"Accept": accept, "User-Agent": "hermes-cybopal"}
        if data is not None:
            head["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=head)
        try:
            return _OPENER.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise DeviceError(f"the device answered with a redirect ({error.code}); set device_url to the "
                                  "device's own address (redirects are not followed)") from None
            said = ""
            try:
                payload = json.loads(error.read(65536) or b"{}")
                detail = payload.get("detail") if isinstance(payload, dict) else None
                said = str(detail or payload)[:300]
            except (ValueError, AttributeError, OSError):
                pass
            raise DeviceError(f"the device refused {method} {path}: {said or error.reason} ({error.code})") from None
        except (urllib.error.URLError, OSError) as error:
            raise DeviceError(f"the CyboPal device does not answer at {self.url} "
                              f"({getattr(error, 'reason', error)}). If it is not running: {GET_THE_SIMULATOR}. "
                              "If it runs on another machine, set device_url in this plugin's settings.") from None

    def call(self, method: str, path: str, body=None):
        with self._open(method, path, body) as answer:
            raw = answer.read(4 * 2**20)
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise DeviceError(f"the device at {self.url} answered with something that is not JSON") from None

    # ------------------------------------------------------------------------------------------------ the device
    def health(self) -> dict:
        return self.call("GET", "/api/v1/health") or {}

    def limits(self) -> dict:
        return self.call("GET", "/api/v1/limits") or {}

    def telemetry(self) -> dict:
        """The device's state (pose, target, mode, displays, safety, events)."""

        data = self.call("GET", "/api/v1/telemetry") or {}
        state = data.get("hardware_state") if isinstance(data, dict) else None
        if not isinstance(state, dict):
            raise DeviceError("the device's telemetry has no hardware_state")
        return state

    def control(self, body: dict) -> dict:
        data = self.call("POST", "/api/v1/control", body) or {}
        return data.get("current_state") or {}

    def mode(self, mode: str) -> dict:
        data = self.call("POST", "/api/v1/mode", {"mode": mode}) or {}
        return data.get("current_state") or {}

    def frame(self, camera: str) -> bytes:
        """One JPEG from the bezel (front) or base camera."""

        if camera not in ("bezel", "base"):
            raise DeviceError(f"no camera {camera!r}: use bezel or base")
        with self._open("GET", f"/api/v1/camera/{camera}", accept="image/jpeg") as answer:
            data = answer.read(JPEG_MAX + 1)
        if len(data) > JPEG_MAX:
            raise DeviceError(f"the {camera} camera frame is over {JPEG_MAX // 2**20} MB")
        if not data.startswith(b"\xff\xd8"):
            raise DeviceError(f"the {camera} camera did not answer with a JPEG")
        return data
