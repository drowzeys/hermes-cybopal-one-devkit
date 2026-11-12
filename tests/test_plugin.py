"""The plugin against a fake CyboPal ONE (a local HTTP server with the DevKit's contract): no Hermes or simulator needed.

Run: python -m pytest tests
Also against the real DevKit simulator: CYBOPAL_SIM_URL=http://127.0.0.1:5000 python -m pytest tests
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("cybopal_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()
tools = sys.modules["cybopal_plugin.tools"]
device_mod = sys.modules["cybopal_plugin.device"]

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64 + b"\xff\xd9"
PRESETS = {"sit": {"pan": 0, "tilt": 6, "height_mm": 190, "reach_mm": 300, "roll": 0, "elbow": -6},
           "stand": {"pan": 0, "tilt": 12, "height_mm": 400, "reach_mm": 210, "roll": 0, "elbow": 4},
           "stow": {"pan": 0, "tilt": 18, "height_mm": 90, "reach_mm": 90, "roll": 0, "elbow": 8}}
START = {"status": "ONLINE", "mode": "tracking",
         "pose": {"pan": 0, "tilt": 6, "height_mm": 200, "reach_mm": 300, "roll": 0, "elbow": -8},
         "display_markdown": "# hi", "pauli_text": "FOLLOW", "user_in_frame": {"u": 320, "v": 240},
         "safety": {"estop": False, "collision": False, "in_bounds": True},
         "events": [{"kind": "boot", "message": "online"}]}


class FakeDevice(BaseHTTPRequestHandler):
    state: dict = {}
    posts: list = []
    redirect = False
    frame = JPEG

    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/json", extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _snap(self):
        s = FakeDevice.state
        s["target"] = dict(s["pose"])
        return copy.deepcopy(s)

    def _set_mode(self, mode):
        s = FakeDevice.state
        if mode == "estop":
            s["mode"], s["safety"]["estop"], s["pauli_text"] = "estop", True, "E-STOP"
            return
        s["safety"]["estop"] = False
        if mode in PRESETS:
            s["mode"], s["pose"], s["pauli_text"] = "preset", dict(PRESETS[mode]), mode.upper()
        elif mode == "wave":
            s["mode"], s["pauli_text"] = "preset", "WAVE"
        elif mode in ("tracking", "follow"):
            s["mode"] = "tracking"
        else:
            s["mode"] = mode

    def do_GET(self):
        if FakeDevice.redirect:
            return self._send(302, {}, extra={"Location": "http://example.com/"})
        if self.path == "/api/v1/health":
            return self._send(200, {"ok": True, "mode": FakeDevice.state["mode"]})
        if self.path == "/api/v1/limits":
            return self._send(200, {"limits": {k: list(v) for k, v in device_mod.LIMITS.items()}, "presets": PRESETS})
        if self.path == "/api/v1/telemetry":
            snap = self._snap()
            return self._send(200, {"status": snap["status"], "hardware_state": snap})
        if self.path in ("/api/v1/camera/bezel", "/api/v1/camera/base"):
            return self._send(200, FakeDevice.frame, "image/jpeg")
        return self._send(404, {"detail": "Not Found"})

    def do_DELETE(self):
        if self.path == "/api/v1/media":
            FakeDevice.posts.append((self.path, "DELETE")); FakeDevice.state["display_media"] = None
            return self._send(200, {"status": "SUCCESS", "current_state": self._snap()})
        return self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        s = FakeDevice.state
        if self.path.startswith("/api/v1/media"):
            FakeDevice.posts.append(("/api/v1/media", {"type": self.headers.get("Content-Type"), "bytes": len(raw),
                                                       "query": self.path.partition("?")[2]}))
            if s["mode"] == "estop":
                return self._send(409, {"detail": "E-stop latched"})
            kind = "video" if self.headers.get("Content-Type", "").startswith("video/") else "image"
            s["display_media"] = {"id": 1, "kind": kind, "mime": self.headers.get("Content-Type"), "caption": "", "bytes": len(raw)}
            return self._send(200, {"status": "SUCCESS", "current_state": self._snap()})
        body = json.loads(raw or b"{}")
        FakeDevice.posts.append((self.path, body))
        if self.path == "/api/v1/mode":
            self._set_mode(body["mode"])
        elif self.path == "/api/v1/control":
            if s["mode"] == "estop" and body.get("mode") not in ("idle", "tracking", "follow", "preset", "agent"):
                return self._send(200, {"status": "SUCCESS", "current_state": self._snap()})  # refused, like the sim
            for k in device_mod.JOINTS:
                if body.get(k) is not None:
                    s["pose"][k] = body[k]
            if body.get("display_markdown") is not None:
                s["display_markdown"] = body["display_markdown"]
            if body.get("pauli_text") is not None:
                s["pauli_text"] = body["pauli_text"]
            if body.get("mode"):
                self._set_mode(body["mode"])
        else:
            return self._send(404, {"detail": "Not Found"})
        return self._send(200, {"status": "SUCCESS", "current_state": self._snap()})


@pytest.fixture()
def fake(tmp_path):
    FakeDevice.state, FakeDevice.posts, FakeDevice.redirect, FakeDevice.frame = copy.deepcopy(START), [], False, JPEG
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDevice)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = {"device_url": f"http://127.0.0.1:{server.server_port}", "settle_seconds": 0}
    settings = tools.Settings(get_config=lambda k, d=None: config.get(k, d), data_dir=lambda: tmp_path,
                              sleep=lambda s: None)
    yield settings, config
    server.shutdown()


def run(settings, tool, **args):
    return json.loads(tools.HANDLERS[tool](settings, args))


# ------------------------------------------------------------------------------------------------ manifest
def test_manifest_matches_registrations():
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["provides_tools"] == list(tools.TOOL_NAMES)
    registered, commands, skills = [], [], []

    class Ctx:
        def get_config(self, key, default=None):
            return default

        def register_tool(self, **kw):
            assert kw["schema"]["name"] == kw["name"]
            registered.append(kw["name"])

        def register_command(self, name, **kw):
            commands.append(name)

        def register_skill(self, name, path):
            skills.append(name)

    plugin.register(Ctx())
    assert registered == list(tools.TOOL_NAMES) and commands == ["cybopal"] and skills == ["cybopal-one-devkit"]


# ------------------------------------------------------------------------------------------------ tools
def test_status(fake):
    settings, _ = fake
    out = run(settings, "cybopal_status")
    assert out["mode"] == "tracking" and out["person_in_camera"].startswith("middle centre")
    assert out["joint_limits"]["pan"] == [-55.0, 55.0] and "wave-hello" in out["routines"]


def test_mode_preset_and_follow(fake):
    settings, _ = fake
    out = run(settings, "cybopal_mode", mode="stand")
    assert out["done"] == "mode stand" and out["pose"]["height_mm"] == 400
    assert FakeDevice.posts[-1] == ("/api/v1/mode", {"mode": "stand"})
    run(settings, "cybopal_mode", mode="follow")
    assert FakeDevice.posts[-1] == ("/api/v1/mode", {"mode": "tracking"})


def test_unknown_mode_refused(fake):
    settings, _ = fake
    assert "no mode" in run(settings, "cybopal_mode", mode="estop")["error"]  # estop only via cybopal_stop
    assert not FakeDevice.posts


def test_pose_leaves_follow_and_moves(fake):
    settings, _ = fake
    out = run(settings, "cybopal_pose", pan=20, tilt=-5)
    assert FakeDevice.posts[-1] == ("/api/v1/control", {"pan": 20.0, "tilt": -5.0, "mode": "agent"})
    assert out["pose"]["pan"] == 20 and out["arrived"] is True


@pytest.mark.parametrize("args, says", [({"pan": 90}, "outside its limits"), ({"height_mm": 10}, "outside"),
                                        ({"tilt": "up"}, "must be a number"), ({"pan": float("nan")}, "finite"),
                                        ({"pan": True}, "must be a number"), ({}, "at least one")])
def test_pose_refuses_bad_values(fake, args, says):
    settings, _ = fake
    assert says in run(settings, "cybopal_pose", **args)["error"]
    assert not FakeDevice.posts  # nothing reached the arm


def test_stop_then_motion_refused_until_cleared(fake):
    settings, _ = fake
    out = run(settings, "cybopal_stop")
    assert out["stopped"] is True and out["mode"] == "estop"
    for name, args in (("cybopal_pose", {"pan": 10}), ("cybopal_mode", {"mode": "sit"}),
                       ("cybopal_routine", {"name": "nod"}), ("cybopal_display", {"pauli_text": "HI"})):
        before = len(FakeDevice.posts)
        out = run(settings, name, **args)
        assert out["error"] == "E-stopped" and "/cybopal resume" in out["note"]
        assert len(FakeDevice.posts) == before, name  # the plugin never sends "mode: agent" past an E-stop
    out = json.loads(tools.resume(settings))  # only the person's /cybopal resume reaches this
    assert out["mode"] == "tracking" and out["safety"]["estop"] is False


@pytest.mark.parametrize("flag", [True, "yes", 1])
def test_no_tool_can_clear_estop(fake, flag):
    settings, _ = fake
    run(settings, "cybopal_stop")
    before = len(FakeDevice.posts)
    for mode in ("follow", "sit", "idle"):
        assert run(settings, "cybopal_mode", mode=mode, clear_estop=flag)["error"] == "E-stopped"
    assert len(FakeDevice.posts) == before  # nothing reached the arm
    assert all("clear_estop" not in json.dumps(schema) for schema in tools.SCHEMAS.values())
    assert "resume" not in tools.HANDLERS


def test_allow_motion_off(fake):
    settings, config = fake
    config["allow_motion"] = False
    for name, args in (("cybopal_pose", {"pan": 10}), ("cybopal_mode", {"mode": "sit"}),
                       ("cybopal_routine", {"name": "nod"})):
        assert "allow_motion" in run(settings, name, **args)["error"]
    assert not FakeDevice.posts
    assert run(settings, "cybopal_display", pauli_text="OK")["pauli_text"] == "OK"  # screens still work
    assert run(settings, "cybopal_stop")["stopped"] is True  # and the E-stop always works


def test_display(fake):
    settings, _ = fake
    out = run(settings, "cybopal_display", markdown="# Focus\n\n25 minutes", pauli_text="FOCUS")
    assert out["pauli_text"] == "FOCUS" and out["main_display"].startswith("# Focus")
    assert "mode" not in FakeDevice.posts[-1][1]
    assert "limit is 80" in run(settings, "cybopal_display", pauli_text="x" * 81)["error"]
    assert "limit is 8000" in run(settings, "cybopal_display", markdown="x" * 8001)["error"]
    assert "markdown and/or" in run(settings, "cybopal_display")["error"]


def test_look_saves_jpeg_and_media_line(fake, tmp_path):
    settings, _ = fake
    out = run(settings, "cybopal_look", camera="base")
    path = Path(out["file"])
    assert path.read_bytes() == JPEG and path.parent == tmp_path / "cameras"
    assert out["media_tag"] == f"MEDIA:{path}" and out["media_tag"].count("\n") == 0
    assert "centre" in out["person_in_camera"]


def test_look_refuses_non_jpeg_and_unknown_camera(fake):
    settings, _ = fake
    assert "no camera" in run(settings, "cybopal_look", camera="../etc")["error"]
    FakeDevice.frame = b"<html>not a picture</html>"
    assert "JPEG" in run(settings, "cybopal_look")["error"]


def test_look_keeps_only_newest_frames(fake, tmp_path):
    settings, _ = fake
    for _ in range(tools.KEEP_FRAMES + 5):
        run(settings, "cybopal_look")
    assert len(list((tmp_path / "cameras").glob("*.jpg"))) == tools.KEEP_FRAMES


def test_builtin_routine(fake):
    settings, _ = fake
    out = run(settings, "cybopal_routine", name="wave-hello")
    assert out["done"] == "routine wave-hello" and out["mode"] == "tracking"
    assert {"pauli_text": "HELLO"} in [body for _, body in FakeDevice.posts]
    assert FakeDevice.state["display_markdown"] == START["display_markdown"]  # the main screen is left alone
    assert ("/api/v1/mode", {"mode": "wave"}) in FakeDevice.posts and FakeDevice.posts[-1][1] == {"mode": "tracking"}


SIT = {"mode": "sit"}  # a good first step: it must not run when a later step is bad


@pytest.mark.parametrize("steps, says", [([SIT, {"pan": 99}], "outside"), ([SIT, {"wait": 61}], "limit is 60"),
                                         ([SIT, {"jump": 1}], "unknown step"), ([SIT, {"mode": "fly"}], "no mode"),
                                         ([SIT, {"wait": -1}], "negative"), ([SIT, "stand"], "object"),
                                         ([SIT, {"pauli_text": "x" * 81}], "limit is 80"),
                                         ([], "routine name"), ([SIT] * 25, "at most 24")])
def test_routine_checks_every_step_before_moving(fake, steps, says):
    settings, _ = fake
    assert says in run(settings, "cybopal_routine", steps=steps)["error"]
    assert not FakeDevice.posts  # a bad step anywhere means nothing moved


def test_design_save_and_rerun_a_routine(fake, tmp_path):
    settings, _ = fake
    steps = [{"mode": "stand"}, {"wait": 1}, {"pan": 20, "pauli_text": "STRETCH"}, {"wait": 1}, {"mode": "follow"}]
    out = run(settings, "cybopal_routine", steps=steps, save_as="stretch-break", description="a standing stretch")
    assert out["saved_as"] == "stretch-break" and "physical CyboPal ONE" in out["note"]
    assert json.loads((tmp_path / "routines.json").read_text())["stretch-break"]["steps"] == steps
    FakeDevice.posts.clear()
    assert run(settings, "cybopal_routine", name="stretch-break")["done"] == "routine stretch-break"
    assert ("/api/v1/mode", {"mode": "stand"}) in FakeDevice.posts
    listed = run(settings, "cybopal_routine", list=True)
    assert listed["saved"]["stretch-break"] == {"description": "a standing stretch", "steps": 5} and "nod" in listed["built_in"]


@pytest.mark.parametrize("save_as", ["nod", "../evil", "Bad Name", "x" * 41])
def test_save_as_names_checked_before_moving(fake, save_as):
    settings, _ = fake
    assert "save_as must be" in run(settings, "cybopal_routine", steps=[{"mode": "sit"}], save_as=save_as)["error"]
    assert not FakeDevice.posts


def test_routine_that_fails_is_not_saved(fake, tmp_path):
    settings, _ = fake
    run(settings, "cybopal_stop")
    assert run(settings, "cybopal_routine", steps=[{"mode": "sit"}], save_as="never")["error"] == "E-stopped"
    assert not (tmp_path / "routines.json").exists()


def test_routine_halts_on_estop_midway(fake):
    settings, _ = fake
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        FakeDevice.state["mode"], FakeDevice.state["safety"]["estop"] = "estop", True  # someone hits E-stop

    settings.sleep = sleep
    out = run(settings, "cybopal_routine", steps=[{"mode": "sit"}, {"wait": 1}, {"mode": "stand"}])
    assert "E-stopped at step 3" in out["error"]
    assert ("/api/v1/mode", {"mode": "stand"}) not in FakeDevice.posts


def test_redirects_not_followed(fake):
    settings, _ = fake
    FakeDevice.redirect = True
    assert "redirect" in run(settings, "cybopal_status")["error"]


def test_bad_device_url(fake):
    settings, config = fake
    for url in ("file:///etc/passwd", "ftp://x", "http://user:pw@127.0.0.1:5000", "http://127.0.0.1:5000/?x=1"):
        config["device_url"] = url
        assert "device_url must be" in run(settings, "cybopal_status")["error"]


def test_missing_device_points_to_devkit(fake):
    settings, config = fake
    config["device_url"] = "http://127.0.0.1:9"
    error = run(settings, "cybopal_status")["error"]
    assert "does not answer" in error and device_mod.DEVKIT_REPO in error and "oneshot.sh" in error


def test_settle_bounds():
    s = tools.Settings(get_config=lambda k, d=None: d)
    assert s.settle({"settle_seconds": 99}) == tools.SETTLE_MAX and s.settle({"settle_seconds": -4}) == 0
    assert s.settle({"settle_seconds": "inf"}) == 3.0 and s.settle({}) == 3


def test_command(fake):
    settings, config = fake

    class Ctx:
        def __init__(self):
            self.cmd = None

        def get_config(self, key, default=None):
            return config.get(key, default)

        def register_tool(self, **kw):
            pass

        def register_command(self, name, handler, **kw):
            self.cmd = handler

        def register_skill(self, *a):
            pass

    ctx = Ctx()
    plugin.register(ctx)
    assert "mode tracking" in ctx.cmd("")
    assert "EMERGENCY STOP" in ctx.cmd("stop")
    assert "E-stopped" in ctx.cmd("sit")
    assert "E-stopped" in ctx.cmd("follow")
    assert "E-stop released, mode follow" in ctx.cmd("resume")
    assert "Pauli 'HI THERE'" in ctx.cmd("say HI THERE")
    assert "wave-hello" in ctx.cmd("help")


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
MP4 = b"\0\0\0\x18ftypmp42" + b"\0" * 64


def test_show_picture_and_video(fake, tmp_path):
    settings, _ = fake
    (tmp_path / "chart.png").write_bytes(PNG); (tmp_path / "clip.mp4").write_bytes(MP4)
    out = run(settings, "cybopal_show", path=str(tmp_path / "chart.png"), caption="Revenue 2026")
    assert out["done"].startswith("showing the image") and out["main_display"] == "image on screen"
    assert FakeDevice.posts[-1][1]["type"] == "image/png" and "caption=Revenue+2026" in FakeDevice.posts[-1][1]["query"]
    out = run(settings, "cybopal_show", path=str(tmp_path / "clip.mp4"))
    assert FakeDevice.posts[-1][1]["type"] == "video/mp4" and out["main_display"] == "video on screen"
    assert run(settings, "cybopal_show", file=str(tmp_path / "chart.png"))["main_display"] == "image on screen"  # alias
    assert "cleared" in run(settings, "cybopal_show", clear=True)["done"] and FakeDevice.posts[-1] == ("/api/v1/media", "DELETE")


@pytest.mark.parametrize("name, data, says", [(".env", b"KEY=secret", "only pictures and videos"),
                                              ("notes.txt", b"hello", "only pictures and videos"),
                                              ("secret.png", b"KEY=secret", "not really a PNG"),
                                              ("fake.mp4", b"KEY=secret" * 3, "not really a MP4"),
                                              ("missing.png", None, "no such file")])
def test_show_sends_only_real_media(fake, tmp_path, name, data, says):
    settings, _ = fake
    if data is not None:
        (tmp_path / name).write_bytes(data)
    assert says in run(settings, "cybopal_show", path=str(tmp_path / name))["error"]
    assert not FakeDevice.posts  # nothing left the computer


def test_show_refused_while_estopped_and_caption_limit(fake, tmp_path):
    settings, _ = fake
    (tmp_path / "a.png").write_bytes(PNG)
    assert "200 characters" in run(settings, "cybopal_show", path=str(tmp_path / "a.png"), caption="x" * 201)["error"]
    run(settings, "cybopal_stop")
    assert run(settings, "cybopal_show", path=str(tmp_path / "a.png"))["error"] == "E-stopped"
    assert not [p for p in FakeDevice.posts if p[0] == "/api/v1/media"]


# ------------------------------------------------------------------------------------------------ real simulator
SIM = os.environ.get("CYBOPAL_SIM_URL")


@pytest.mark.skipif(not SIM, reason="set CYBOPAL_SIM_URL to run against the DevKit simulator")
def test_against_the_real_simulator(tmp_path):
    config = {"device_url": SIM, "settle_seconds": 6}
    settings = tools.Settings(get_config=lambda k, d=None: config.get(k, d), data_dir=lambda: tmp_path)
    try:
        assert run(settings, "cybopal_status")["status"] in ("ONLINE", "ESTOP")
        run(settings, "cybopal_stop")
        assert run(settings, "cybopal_pose", pan=10)["error"] == "E-stopped"
        assert json.loads(tools.resume(settings, "sit"))["arrived"] is True
        out = run(settings, "cybopal_pose", pan=25, tilt=-4, height_mm=260)
        assert out["arrived"] and abs(out["pose"]["pan"] - 25) < 1.5 and out["mode"] == "agent"
        assert run(settings, "cybopal_display", markdown="# test", pauli_text="TEST")["pauli_text"] == "TEST"
        assert Path(run(settings, "cybopal_look")["file"]).read_bytes()[:2] == b"\xff\xd8"
        assert run(settings, "cybopal_routine", name="nod")["done"] == "routine nod"
        png = tmp_path / "shot.png"; png.write_bytes((ROOT / "assets/catalog/banner.png").read_bytes())
        assert run(settings, "cybopal_show", path=str(png), caption="test")["main_display"] == "image on screen: test"
        assert run(settings, "cybopal_display", markdown="# back")["main_display"].startswith("# back")
    finally:
        tools.resume(settings)
