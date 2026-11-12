"""CyboPal ONE DevKit for Hermes (unofficial): move the robotic screen, show things on it, look through its cameras, stop it.

Registers the cybopal_* tools, the /cybopal command and the cybopal skill. Everything goes to the CyboPal ONE DevKit's
HTTP contract at the ``device_url`` setting (default http://127.0.0.1:5000): the DevKit's live simulator today, the
same contract on the physical CyboPal ONE later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import tools
from .device import DEVKIT_REPO

logger = logging.getLogger(__name__)

USAGE = ("/cybopal — CyboPal ONE\n"
         "  /cybopal                 mode, pose, screens and safety\n"
         "  /cybopal stop            EMERGENCY STOP (freeze the arm)\n"
         "  /cybopal follow|sit|stand|recline|meeting|stow|wave|idle\n"
         "  /cybopal say <words>     put the words on Pauli\n"
         "  /cybopal show <markdown> put it on the 27-inch screen\n"
         "  /cybopal look [base]     a picture from the front (or base) camera\n"
         "  /cybopal run <routine>   wave-hello, posture-cycle, meeting-brief, nod\n"
         "  /cybopal resume          clear an E-stop (only when it is safe) and go back to follow\n"
         f"Get the live simulator (free, open source): {DEVKIT_REPO}")


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(tools.PLUGIN)


def _line(result: str) -> str:
    """A tool result as a short chat reply."""

    data = json.loads(result)
    if data.get("error") and "mode" not in data:
        return "CyboPal: " + data["error"]
    lines = []
    if data.get("error"):
        lines.append("CyboPal: " + data["error"])
    if data.get("done"):
        lines.append(data["done"])
    if data.get("mode"):
        pose = data.get("pose") or {}
        lines.append(f"mode {data['mode']} · Pauli {data.get('pauli_text')!r} · "
                     + " ".join(f"{k}={v}" for k, v in pose.items()))
    if data.get("person_in_camera"):
        lines.append("person: " + data["person_in_camera"])
    if data.get("note"):
        lines.append(data["note"])
    if data.get("media_tag"):
        lines.append(data["media_tag"])
    return "\n".join(lines) or USAGE


def register(ctx) -> None:
    settings = tools.Settings(get_config=ctx.get_config, data_dir=_data_dir)

    for name in tools.TOOL_NAMES:
        handler = tools.HANDLERS[name]
        ctx.register_tool(name=name, toolset="cybopal_devkit", schema=tools.SCHEMAS[name],
                          handler=lambda args, _h=handler, **kwargs: _h(settings, args),
                          description=tools.SCHEMAS[name]["description"],
                          emoji="🛑" if name == "cybopal_stop" else "🦾")

    def command(raw_args: str) -> str:
        words = (raw_args or "").strip().split(None, 1)
        verb, rest = (words[0].lower() if words else ""), (words[1].strip() if len(words) > 1 else "")
        if verb in ("", "status"):
            return _line(tools.cybopal_status(settings, {}))
        if verb in ("help", "-h", "--help"):
            return USAGE
        if verb in ("stop", "estop", "freeze"):
            return _line(tools.cybopal_stop(settings, {}))
        if verb == "resume":
            return _line(tools.resume(settings, "follow"))
        if verb in tools.MODES:
            return _line(tools.cybopal_mode(settings, {"mode": verb}))
        if verb == "say" and rest:
            return _line(tools.cybopal_display(settings, {"pauli_text": rest[:tools.PAULI_MAX]}))
        if verb == "show" and rest:
            return _line(tools.cybopal_display(settings, {"markdown": rest}))
        if verb == "look":
            return _line(tools.cybopal_look(settings, {"camera": rest or "bezel"}))
        if verb == "run" and rest:
            return _line(tools.cybopal_routine(settings, {"name": rest}))
        return USAGE

    ctx.register_command("cybopal", handler=command, description="CyboPal ONE: status, stop, modes, screens, cameras",
                         args_hint="[stop|follow|sit|stand|say|show|look|run|resume] ...")

    skills = Path(__file__).parent / "skills"
    for child in sorted(skills.iterdir()) if skills.is_dir() else []:
        if (child / "SKILL.md").is_file():
            ctx.register_skill(child.name, child / "SKILL.md")
