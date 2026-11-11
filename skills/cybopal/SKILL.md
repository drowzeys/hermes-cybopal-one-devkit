---
name: cybopal
description: How to drive the CyboPal ONE robotic screen with the cybopal_* tools (follow, postures, poses, gestures, both screens, cameras) and the safety rules for a machine that moves next to a person.
---

# Driving the CyboPal ONE

The CyboPal ONE is a 27-inch 4K screen on a 6-DOF robotic arm (700 mm reach) with a 3.5-inch Pauli companion display,
a front (bezel) camera and a base camera. It sits on a desk next to a person. The `cybopal_*` tools drive it; today
they usually drive the DevKit's live simulator, which behaves the same way.

## Safety first

1. **Stop means stop.** If the person says stop, freeze, wait, ouch, or anything that suggests the arm is in the way,
   call `cybopal_stop` first, before answering or doing anything else.
2. **Never clear an E-stop on your own.** While it is E-stopped the device refuses motion. Ask the person whether it
   is safe; only after they say so, call `cybopal_mode` with `clear_estop: true` (usually with mode `follow`).
3. **Small moves near the person.** Prefer presets and follow mode. For exact poses, change one or two joints at a
   time and check the result. Out-of-range values are refused, not clamped: read `joint_limits` from `cybopal_status`.
4. If motion is switched off in the plugin's settings, say so; screens and cameras still work.

## How to use the tools

- **`cybopal_status` first**, and whenever unsure: mode, pose, screens, safety, where the person is in the camera.
- **`cybopal_mode`**: `follow` (the arm keeps the screen on the person), `sit`, `stand`, `recline`, `meeting`, `stow`,
  `wave`, `idle`. "I'm standing up now" → `stand`. "Lean back" → `recline`. "Put it away" → `stow`.
- **`cybopal_pose`**: exact joints. `pan` turns left (-) or right (+), `tilt` tips the screen down (-) or up (+),
  `height_mm` raises the column, `reach_mm` brings it toward the person. A pose leaves follow mode; go back with
  `cybopal_mode follow`.
- **`cybopal_display`**: markdown on the 27-inch screen (headings, lists, short tables) and a few words on Pauli
  (Pauli fits about 10 characters in big type: "FOCUS", "BREAK", "HELLO").
- **`cybopal_look`**: a picture from the front or base camera, plus where the person is in the frame. Put the
  `MEDIA:` line in your reply exactly as given so the picture reaches the chat.
- **`cybopal_routine`**: built-ins `wave-hello`, `posture-cycle`, `meeting-brief`, `nod`, or your own steps
  (`{"mode": "sit"}`, `{"pan": 20}`, `{"markdown": "..."}`, `{"pauli_text": "..."}`, `{"wait": 2}`), at most 24 steps
  and 60 seconds of waiting.

## When the device is not there

If a tool says the device does not answer, the simulator is probably not running. Tell the person plainly: the
CyboPal ONE DevKit simulator is free and open source; they get it from
https://github.com/drowzeys/keys-CyboPal-ONE-Hermes-DevKit-LiveSimulator-Nemotron3Nano-Omni-for-DGX-Spark and start it
with `./oneshot.sh` (or the docker one-liner in its README), then open http://127.0.0.1:5000/. If it runs on another
computer, they set `device_url` in this plugin's settings. Do not retry in a loop.
