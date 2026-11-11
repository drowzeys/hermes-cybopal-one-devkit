"""The tools Hermes calls: schemas (what the model reads) and handlers (what runs).

Every handler returns a JSON string and never raises. Motion is refused while the device is E-stopped; only
``cybopal_mode`` with ``clear_estop`` releases it, and only ``cybopal_stop`` is allowed at every moment.
A camera frame is saved in this plugin's data folder and named in ``media_tag`` as a ``MEDIA:<path>`` line, which
Hermes delivers to the chat (Telegram, Discord, Desktop, ...).
"""

from __future__ import annotations

import json
import math
import secrets
import time
from pathlib import Path
from typing import Callable

from .device import DEFAULT_URL, JOINTS, LIMITS, MARKDOWN_MAX, PAULI_MAX, Device, DeviceError

PLUGIN = "cybopal-one-devkit"
SETTLE_MAX = 10.0
ROUTINE_STEPS = 24
ROUTINE_SECONDS = 60.0
KEEP_FRAMES = 30

# Plain names the person uses, mapped to the device's modes ("follow" is the firmware's on-device tracking).
MODES = {"follow": "tracking", "tracking": "tracking", "sit": "sit", "stand": "stand", "recline": "recline",
         "meeting": "meeting", "stow": "stow", "wave": "wave", "idle": "idle"}

ROUTINES = {
    "wave-hello": {
        "description": "Wave the 27-inch panel and say HELLO on Pauli (the main screen is left as it is), then "
                       "return to follow.",
        "steps": [{"mode": "wave", "pauli_text": "HELLO"}, {"wait": 4.0}, {"mode": "follow"}]},
    "posture-cycle": {
        "description": "Sit, stand, recline, then back to follow: a quick test of the presets.",
        "steps": [{"mode": "sit"}, {"wait": 2.5}, {"mode": "stand"}, {"wait": 2.5},
                  {"mode": "recline"}, {"wait": 2.5}, {"mode": "follow"}]},
    "meeting-brief": {
        "description": "Park in the meeting pose and put a short brief on the 27-inch screen.",
        "steps": [{"mode": "meeting"},
                  {"markdown": "# Meeting\n\n- Cameras: bezel + base live\n- Pose: turned toward the guest chair\n"
                               "- Hermes is listening for the next request", "pauli_text": "MEET"}]},
    "nod": {
        "description": "A small yes-nod of the screen, then back where it was.",
        "steps": [{"tilt": 14}, {"wait": 0.7}, {"tilt": -2}, {"wait": 0.7}, {"tilt": 14}, {"wait": 0.7},
                  {"tilt": 6}]},
}

# ------------------------------------------------------------------------------------------------------ schemas
_JOINT_TEXT = {
    "pan": "turn left (-) / right (+), degrees",
    "tilt": "screen tilt down (-) / up (+), degrees",
    "height_mm": "column height, millimetres",
    "reach_mm": "how far the arm reaches toward the person, millimetres",
    "roll": "screen roll, degrees",
    "elbow": "elbow bend, degrees",
}
_JOINT_PROPS = {k: {"type": "number", "minimum": lo, "maximum": hi, "description": f"{_JOINT_TEXT[k]} ({lo:g} to {hi:g})"}
                for k, (lo, hi) in LIMITS.items()}
_MARKDOWN = {"type": "string", "maxLength": MARKDOWN_MAX, "description": "markdown for the 27-inch 4K main screen"}
_PAULI = {"type": "string", "maxLength": PAULI_MAX, "description": "a few words for the 3.5-inch Pauli companion display"}


def _schema(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


SCHEMAS = {
    "cybopal_status": _schema(
        "cybopal_status",
        "The CyboPal ONE (a 27-inch 4K screen on a 6-DOF robotic arm, with the 3.5-inch Pauli companion display and "
        "two cameras): its mode, arm pose, what its screens show, safety state (E-stop, collision, in bounds), where "
        "the person is in its camera, the joint limits and recent events. Call it first, and whenever unsure.",
        {}),
    "cybopal_mode": _schema(
        "cybopal_mode",
        "Put the CyboPal ONE in a mode. follow: the arm keeps the screen pointed at the person (it does not change the "
        "height). sit: desk height for a seated person. stand: raises the screen to standing height (\"I'm standing up\", "
        "\"raise the screen\"). recline: lowers the screen and tilts it for leaning back. meeting: turns the screen toward "
        "a guest. stow: folds the arm away. wave: a greeting gesture with the screen. idle: holds still. Waits for the "
        "arm to settle and returns its pose.",
        {"mode": {"type": "string", "enum": list(dict.fromkeys(MODES)), "description": "the mode"},
         "clear_estop": {"type": "boolean", "description": "release an emergency stop. Only when the person has said "
                                                          "it is safe to move again."},
         "settle_seconds": {"type": "number", "minimum": 0, "maximum": SETTLE_MAX,
                            "description": "how long to wait for the arm to arrive (default: the plugin setting, 3)"}},
        ("mode",)),
    "cybopal_pose": _schema(
        "cybopal_pose",
        "Move the CyboPal ONE's arm to an exact pose: any of pan, tilt, height_mm, reach_mm, roll, elbow (the rest stay "
        "put). Stops follow mode so the arm holds the pose. Values outside the joint limits are refused, not clamped.",
        {**_JOINT_PROPS,
         "settle_seconds": {"type": "number", "minimum": 0, "maximum": SETTLE_MAX,
                            "description": "how long to wait for the arm to arrive (default: the plugin setting, 3)"}}),
    "cybopal_display": _schema(
        "cybopal_display",
        "Show something on the CyboPal ONE: markdown on the 27-inch main screen and/or a few words on Pauli. Does not "
        "move the arm.",
        {"markdown": _MARKDOWN, "pauli_text": _PAULI}),
    "cybopal_look": _schema(
        "cybopal_look",
        "Take a picture with one of the CyboPal ONE's cameras (bezel: the front camera on top of the screen, facing "
        "the person; base: the camera in the base) and say where the person is in the front camera's frame. Returns "
        "the picture as a MEDIA line.",
        {"camera": {"type": "string", "enum": ["bezel", "base"], "description": "which camera (default bezel)"}}),
    "cybopal_routine": _schema(
        "cybopal_routine",
        "Run a short sequence on the CyboPal ONE: a built-in routine by name (wave-hello, posture-cycle, meeting-brief, "
        f"nod) or your own steps. Each step is one of: {{\"mode\": ...}}, joint values ({{\"pan\": 20, \"tilt\": 5}}), "
        "{\"markdown\": ...} and/or {\"pauli_text\": ...}, or {\"wait\": seconds}. At most "
        f"{ROUTINE_STEPS} steps and {ROUTINE_SECONDS:g} seconds of waiting. Stops at once if the device is E-stopped.",
        {"name": {"type": "string", "enum": list(ROUTINES), "description": "a built-in routine"},
         "steps": {"type": "array", "maxItems": ROUTINE_STEPS, "items": {"type": "object"},
                   "description": "your own steps, when no name is given"}}),
    "cybopal_stop": _schema(
        "cybopal_stop",
        "EMERGENCY STOP: freeze the CyboPal ONE's arm where it is. Use it at once, before anything else, whenever the "
        "person says stop, freeze, wait or anything that sounds like the arm is in the way or unsafe.",
        {}),
}

TOOL_NAMES = tuple(SCHEMAS)


# ------------------------------------------------------------------------------------------------------ settings
class Settings:
    """The plugin's settings (``plugins.entries.cybopal-one-devkit.settings``), read at each call."""

    def __init__(self, get_config: Callable | None = None, data_dir: Callable[[], Path] | None = None,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self._get_config = get_config or (lambda key, default=None: default)
        self._data_dir = data_dir
        self.clock, self.sleep = clock, sleep

    def get(self, key: str, default):
        try:
            value = self._get_config(key, default)
        except Exception:  # noqa: BLE001 - a broken config never breaks a tool
            return default
        return default if value in (None, "") else value

    def device(self) -> Device:
        return Device(str(self.get("device_url", DEFAULT_URL)))

    def motion_allowed(self) -> bool:
        value = self.get("allow_motion", True)
        return not (value is False or str(value).strip().lower() in ("false", "0", "no", "off"))

    def settle(self, args: dict) -> float:
        given = args.get("settle_seconds")
        value = self.get("settle_seconds", 3) if given in (None, "") else given
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 3.0
        return max(0.0, min(value, SETTLE_MAX)) if math.isfinite(value) else 3.0

    def frames(self) -> Path:
        if self._data_dir is None:
            raise DeviceError("no data folder for camera pictures")
        return self._data_dir() / "cameras"


# ------------------------------------------------------------------------------------------------------ helpers
def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str, **more) -> str:
    return json.dumps({"error": message, **more}, ensure_ascii=False)


def _number(name: str, value) -> float:
    if isinstance(value, bool):
        raise DeviceError(f"{name} must be a number")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise DeviceError(f"{name} must be a number, not {str(value)[:40]!r}") from None
    if not math.isfinite(n):
        raise DeviceError(f"{name} must be a finite number")
    return n


def _joints(args: dict) -> dict:
    """The joint values given, each checked against the joint limits (refused, never clamped)."""

    out = {}
    for name in JOINTS:
        if args.get(name) in (None, ""):
            continue
        n = _number(name, args[name])
        lo, hi = LIMITS[name]
        if not lo <= n <= hi:
            raise DeviceError(f"{name} {n:g} is outside its limits ({lo:g} to {hi:g})")
        out[name] = round(n, 2)
    return out


def _texts(args: dict) -> dict:
    out = {}
    for key, field, limit in (("markdown", "display_markdown", MARKDOWN_MAX), ("pauli_text", "pauli_text", PAULI_MAX)):
        value = args.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise DeviceError(f"{key} must be text")
        if len(value) > limit:
            raise DeviceError(f"{key} is {len(value)} characters; the limit is {limit}")
        out[field] = value
    return out


def _estopped(state: dict) -> bool:
    return state.get("mode") == "estop" or bool((state.get("safety") or {}).get("estop"))


ESTOP_NOTE = ("The CyboPal is E-stopped, so it will not move. Ask the person whether it is safe; only then call "
              "cybopal_mode with clear_estop true.")


def _brief(state: dict, events: int = 0) -> dict:
    """The parts of the device's state an agent needs."""

    out = {"status": state.get("status"), "mode": state.get("mode"), "pose": state.get("pose"),
           "pauli_text": state.get("pauli_text"), "safety": state.get("safety")}
    target = state.get("target")
    if target and target != state.get("pose"):
        out["moving_to"] = target
    shown = str(state.get("display_markdown") or "")
    out["main_display"] = shown[:400] + ("…" if len(shown) > 400 else "")
    seen = state.get("user_in_frame")
    if isinstance(seen, dict):
        out["person_in_camera"] = _where(seen)
    if events:
        out["events"] = [f"{e.get('kind')}: {e.get('message')}" for e in (state.get("events") or [])[:events]
                         if isinstance(e, dict)]
    return out


def _where(seen: dict) -> str:
    """Where the person sits in the 640x480 front camera frame, in words."""

    try:
        u, v = float(seen.get("u")), float(seen.get("v"))
    except (TypeError, ValueError):
        return "unknown"
    if not (0 <= u <= 640 and 0 <= v <= 480):
        return f"out of frame (u={u:.0f}, v={v:.0f} of 640x480)"
    across = "left" if u < 213 else "right" if u > 427 else "centre"
    down = "top" if v < 160 else "bottom" if v > 320 else "middle"
    return f"{down} {across} of the frame (u={u:.0f}, v={v:.0f} of 640x480)"


def _settle(settings: Settings, device: Device, seconds: float) -> tuple[dict, bool]:
    """The device's state once the arm has reached its target, or as it stands after ``seconds``."""

    ends = settings.clock() + seconds
    while True:
        state = device.telemetry()
        pose, target = state.get("pose") or {}, state.get("target") or {}
        arrived = all(abs(float(pose.get(k, 0)) - float(target.get(k, 0))) < (8 if k.endswith("_mm") else 1.5)
                      for k in JOINTS if k in pose and k in target)
        if arrived or state.get("mode") in ("tracking", "estop") or settings.clock() >= ends:
            return state, arrived
        settings.sleep(min(0.25, max(0.05, ends - settings.clock())))


def _ready_to_move(settings: Settings, device: Device) -> dict | None:
    """None when motion may go ahead; otherwise the reason it may not, as a result payload."""

    if not settings.motion_allowed():
        return {"error": "motion is switched off in this plugin's settings (allow_motion)"}
    state = device.telemetry()
    if _estopped(state):
        return {"error": "E-stopped", "note": ESTOP_NOTE, **_brief(state)}
    return None


# ------------------------------------------------------------------------------------------------------ handlers
def cybopal_status(settings: Settings, args: dict) -> str:
    try:
        device = settings.device()
        state = device.telemetry()
        try:
            limits = device.limits()
        except DeviceError:
            limits = {}
        out = {"device": device.url, **_brief(state, events=6),
               "joint_limits": limits.get("limits") or {k: list(v) for k, v in LIMITS.items()},
               "modes": list(dict.fromkeys(MODES)), "routines": {k: v["description"] for k, v in ROUTINES.items()},
               "motion_allowed": settings.motion_allowed()}
        if _estopped(state):
            out["note"] = ESTOP_NOTE
        return _ok(out)
    except DeviceError as error:
        return _error(str(error))


def cybopal_mode(settings: Settings, args: dict) -> str:
    try:
        wanted = str(args.get("mode") or "").strip().lower()
        if wanted not in MODES:
            raise DeviceError(f"no mode {wanted!r}: use one of {', '.join(dict.fromkeys(MODES))}")
        if not settings.motion_allowed():
            return _error("motion is switched off in this plugin's settings (allow_motion)")
        device = settings.device()
        state = device.telemetry()
        if _estopped(state) and args.get("clear_estop") is not True:
            return _ok({"error": "E-stopped", "note": ESTOP_NOTE, **_brief(state)})
        device.mode(MODES[wanted])
        state, arrived = _settle(settings, device, settings.settle(args))
        return _ok({"done": f"mode {wanted}", "arrived": arrived, **_brief(state)})
    except DeviceError as error:
        return _error(str(error))


def cybopal_pose(settings: Settings, args: dict) -> str:
    try:
        joints = _joints(args)
        if not joints:
            raise DeviceError(f"give at least one of {', '.join(JOINTS)}")
        device = settings.device()
        refused = _ready_to_move(settings, device)
        if refused:
            return _ok(refused)
        device.control({**joints, "mode": "agent"})  # out of follow, so the arm holds the pose
        state, arrived = _settle(settings, device, settings.settle(args))
        return _ok({"done": "pose " + ", ".join(f"{k}={v:g}" for k, v in joints.items()), "arrived": arrived,
                    **_brief(state)})
    except DeviceError as error:
        return _error(str(error))


def cybopal_display(settings: Settings, args: dict) -> str:
    try:
        texts = _texts(args)
        if not texts:
            raise DeviceError("give markdown and/or pauli_text")
        device = settings.device()
        state = device.telemetry()
        if _estopped(state):
            return _ok({"error": "E-stopped", "note": "The device accepts no commands while E-stopped. " + ESTOP_NOTE,
                        **_brief(state)})
        state = device.control(texts)
        return _ok({"done": "shown on " + " and ".join("the main screen" if k == "display_markdown" else "Pauli"
                                                         for k in texts), **_brief(state)})
    except DeviceError as error:
        return _error(str(error))


def cybopal_look(settings: Settings, args: dict) -> str:
    try:
        camera = str(args.get("camera") or "bezel").strip().lower()
        device = settings.device()
        jpeg = device.frame(camera)
        folder = settings.frames()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{camera}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.jpg"
        path.write_bytes(jpeg)
        old = sorted((p for p in folder.glob("*.jpg") if p.is_file() and not p.is_symlink()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[KEEP_FRAMES:]:  # keep only the newest pictures this plugin took
            stale.unlink(missing_ok=True)
        out = {"camera": camera, "file": str(path), "media_tag": f"MEDIA:{path}",
               "note": "Include media_tag's MEDIA line in your reply to show the picture."}
        try:
            state = device.telemetry()
            if isinstance(state.get("user_in_frame"), dict):
                out["person_in_camera"] = _where(state["user_in_frame"])
            out["mode"] = state.get("mode")
        except DeviceError:
            pass
        return _ok(out)
    except (DeviceError, OSError) as error:
        return _error(str(error))


STEP_FIELDS = {"mode", "markdown", "pauli_text", "wait", *JOINTS}


def _check_fields(step) -> None:
    if not isinstance(step, dict):
        raise DeviceError("each step must be an object")
    unknown = set(step) - STEP_FIELDS
    if unknown:
        raise DeviceError(f"unknown step field(s): {', '.join(sorted(map(str, unknown)))[:200]}")


def _step(device: Device, step) -> str:
    """Run one routine step (not a wait) and say what it did."""

    _check_fields(step)
    done = []
    if step.get("mode") not in (None, ""):
        wanted = str(step["mode"]).strip().lower()
        if wanted not in MODES:
            raise DeviceError(f"no mode {wanted!r}")
        device.mode(MODES[wanted])
        done.append(f"mode {wanted}")
    joints = _joints(step)
    if joints:
        device.control({**joints, "mode": "agent"})
        done.append(", ".join(f"{k}={v:g}" for k, v in joints.items()))
    texts = _texts(step)
    if texts:
        device.control(texts)
        done.append("display")
    return "; ".join(done)


def cybopal_routine(settings: Settings, args: dict) -> str:
    try:
        name = str(args.get("name") or "").strip()
        if name:
            if name not in ROUTINES:
                raise DeviceError(f"no routine {name!r}: use one of {', '.join(ROUTINES)}")
            steps = ROUTINES[name]["steps"]
        else:
            steps = args.get("steps")
            if not isinstance(steps, list) or not steps:
                raise DeviceError("give a routine name or a list of steps")
        if len(steps) > ROUTINE_STEPS:
            raise DeviceError(f"at most {ROUTINE_STEPS} steps")
        waits = 0.0
        for step in steps:  # check everything before the arm moves at all
            _check_fields(step)
            if "wait" in step:
                w = _number("wait", step["wait"])
                if w < 0:
                    raise DeviceError("wait must not be negative")
                waits += w
            _joints(step)
            _texts(step)
            if step.get("mode") not in (None, "") and str(step["mode"]).strip().lower() not in MODES:
                raise DeviceError(f"no mode {str(step['mode'])[:40]!r}")
        if waits > ROUTINE_SECONDS:
            raise DeviceError(f"the steps wait {waits:g} seconds; the limit is {ROUTINE_SECONDS:g}")
        device = settings.device()
        refused = _ready_to_move(settings, device)
        if refused:
            return _ok(refused)
        log = []
        for i, step in enumerate(steps, 1):
            if i > 1 and _estopped(device.telemetry()):
                state = device.telemetry()
                return _ok({"error": f"E-stopped at step {i}; the routine stopped", "steps_done": log,
                            "note": ESTOP_NOTE, **_brief(state)})
            did = _step(device, {k: v for k, v in step.items() if k != "wait"})
            if did:
                log.append(f"{i}: {did}")
            if step.get("wait"):
                settings.sleep(float(step["wait"]))
                log.append(f"{i}: waited {float(step['wait']):g}s")
        state = device.telemetry()
        return _ok({"done": f"routine {name or 'custom'}", "steps_done": log, **_brief(state)})
    except DeviceError as error:
        return _error(str(error))


def cybopal_stop(settings: Settings, args: dict) -> str:
    try:
        device = settings.device()
        state = device.mode("estop")
        if not _estopped(state):
            state = device.telemetry()
        return _ok({"done": "EMERGENCY STOP", "stopped": _estopped(state),
                    "note": "The arm is frozen. It stays stopped until the person says it is safe and cybopal_mode "
                            "is called with clear_estop true.", **_brief(state)})
    except DeviceError as error:
        return _error(str(error))


HANDLERS = {"cybopal_status": cybopal_status, "cybopal_mode": cybopal_mode, "cybopal_pose": cybopal_pose,
            "cybopal_display": cybopal_display, "cybopal_look": cybopal_look, "cybopal_routine": cybopal_routine,
            "cybopal_stop": cybopal_stop}
