# Limbus Micro Manager

A configurable, template-driven Python GUI starter for Limbus Company farming workflows. It is inspired by the *feature categories* of LixAssistantLimbusCompany, but does not copy its source code or ship its game assets.

> **Important:** This remains a user-calibrated automation framework, not a verified turnkey bot. It does not include proprietary game screenshots, read game memory, or know the game's full route/gift database. You must capture UI templates from your own game client. Automating gameplay may carry account/game-rule risk; check the current rules and use your own judgement.

## Added in this build

- Tkinter GUI with saved JSON settings and activity log.
- Mirror Dungeon, Luxcavation EXP, and Luxcavation Thread profiles.
- Win Rate (`P`), custom key sequence, or manual battle policy.
- Guided calibration wizard: choose a role, capture the current desktop, drag a crop in the app, and automatically link it to a state, button, node, gift, upgrade, event option, or blacklist entry.
- Template library: preview and remove templates, see where each filename is linked, and refresh the list after a capture.
- Screen analyzer: takes a screenshot, displays raw match scores and coordinates for configured states, and highlights matches. It sends **no** input.
- Template file check across all configured states, actions, route nodes and priority lists.
- Import/export farming profiles as JSON. Profiles include settings and template filenames, not the actual image files.
- Route priorities plus a spatial tie-breaker: highest-confidence, leftmost, rightmost, or closest to screen center among matches for the highest-priority node category.
- Ordered gift, shop upgrade, and event-option templates; gift blacklist; optional calibrated fallbacks.
- Optional gift-upgrade state handling and a calibrated upgrade-confirm button.
- Repeat-action guard: after an action, it waits for a material screen change before acting again on the same recognized state. Full-run completion is counted only once until another known state is seen.
- Start/stop through GUI; F8 toggles and F9 stops under supported Windows/X11 global-hook environments. Under Wayland, use a desktop global shortcut calling `--toggle` or `--stop`.
- `--doctor` command to inspect Python dependencies and desktop capture/input backends.
- Unknown screens are ignored; unexpected worker errors stop the automation.

## Requirements

- Python 3.10+
- Python packages in `requirements.txt`
- Tkinter for the GUI and PyAutoGUI's MouseInfo helper on Linux. It is normally included with Python on Windows.
- A supported screen-capture method: PyAutoGUI outside Wayland; on Wayland, a compositor-friendly screenshot utility such as Spectacle, `gnome-screenshot`, or `grim`.
- On Wayland, input injection needs `ydotool` and a correctly configured daemon/service. Exact setup depends on the distribution and compositor; the app does not change privileged device permissions for you.

### Debian / Ubuntu

The Tkinter and Python development packages named in the MouseInfo warning are:

```bash
sudo apt-get update
sudo apt-get install python3-tk python3-dev
```

If using GNOME Wayland, also install the screenshot utility:

```bash
sudo apt-get install gnome-screenshot
```

### Fedora (including Fedora KDE Wayland)

Fedora uses different package names:

```bash
sudo dnf install python3-tkinter python3-devel spectacle
```

`python3-tkinter` provides Tkinter. `python3-devel` provides development headers for packages that may need them during installation. Spectacle is a preferred screenshot tool on KDE Plasma; on GNOME, use `gnome-screenshot` instead.

Use a virtual environment after installing the system Tk package. The venv uses the matching system Python's standard library, including Tkinter. If you're using a custom Python build (for example, from pyenv), its Tkinter support must be built/configured for that interpreter separately.

### Windows (PowerShell)

```powershell
cd path\to\limbus_micro_manager
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py micro_manager.py
```

### Fedora Linux

```bash
sudo dnf install python3 python3-tkinter python3-devel spectacle
cd /path/to/limbus_micro_manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python micro_manager.py
```

### Debian / Ubuntu Linux

```bash
sudo apt-get install python3 python3-tk python3-dev gnome-screenshot
cd /path/to/limbus_micro_manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python micro_manager.py
```

## First launch and preflight

1. Run `python micro_manager.py --doctor` in the venv. It reports Python, Tkinter, OpenCV, Pillow, screenshot programs, and input backends.
2. Launch with `python micro_manager.py`. The app creates a config directory and template folder:
   - Windows: `%APPDATA%\limbus-micro-manager\templates`
   - Linux: `~/.config/limbus-micro-manager/templates`
3. Keep **Allow mouse/keyboard input** off while calibrating. This is the default for new installs.
4. Set your Identity/team and desired farming stage in game yourself. The manager does not change team composition.
5. Capture and test the relevant state and action templates before enabling input.

## Template calibration workflow

Use **Calibrate / capture…**:

1. Choose the role: state screen, action button, route node, E.G.O Gift priority, shop upgrade priority, event option priority, or gift blacklist.
2. For state/button/node roles, choose the key to link (for example `battle`, `continue`, or `shop`). For gift/upgrade/event cards, enter a short name.
3. Bring the game to the foreground after the main window minimizes. The app captures the desktop.
4. Drag a tight rectangle around a distinctive UI element in the in-app crop window, then save it. The crop is saved at full screenshot resolution and linked to the role you chose.
5. Click **Analyze current screen**. Check the confidence, coordinates, and green rectangles. The analyzer never clicks or types.
6. Click **Check templates** to see if configured references point to existing files. Use **Manage templates…** to preview and remove image files.
7. Test with dry-run enabled and watch the log. Only enable real input after checking every action on the actual screen. Begin with one run and supervise it.

Capture all templates with the same game language, UI scale and display arrangement you plan to use. A state template should be a stable, distinctive heading or panel; an action template should cover only the intended button; a node template must show the **available/selectable** variant. Avoid common backgrounds, repeated decorations, numbers that change, and animations.

## Decision priorities

Priority fields are comma-separated; entries are evaluated from left to right.

- **Route:** values such as `event, shop, rest, focused, normal, risky`. The app evaluates categories in that order. The route tie-breaker is applied only when multiple matches exist for the same category.
- **E.G.O Gifts:** filenames of gift-card crops, best first. The configured blacklist is applied here.
- **Shop upgrades:** filenames of upgrade-card crops, best first.
- **Event options:** filenames of event-option crops, best first.

The manager uses exact image-template matches. It does not infer synergy from gift names, read text with OCR, evaluate all gift mechanics, or reconstruct the dungeon's node graph. Spatial route tie-breakers are heuristics, not a true pathfinding algorithm. If a desired gift/upgrade has not been calibrated and added to its priority list, it cannot intentionally prefer it.

For gift-upgrade interactions, capture `state_gift_upgrade.png` with the wizard (state key `gift_upgrade`), then add upgrade card templates. Only calibrate `button_confirm_upgrade.png` if clicking that specific confirmation is genuinely desired; this action may confirm an in-game choice.

## Repeated-action guard and timing

After a click/key action, the manager records a reduced screen signature. While it still recognizes the same state, it will not act again until the full screen's visual difference reaches the configured **Screen-change threshold**. This helps prevent repeated clicks on an unchanged screen and allows a new selection after a substantial UI change. It is still a heuristic: animated scenes or large visual effects can look like a change. For combat, crop `state_battle.png` around the skill-selection/Win Rate controls rather than a persistent background. Raise the screen-change threshold if actions repeat during animations; lower it cautiously if new choice cards are not handled.

The run-completion state has an additional latch: one visible completion screen counts as one completed run, not one run per polling loop. It resets after a different known state is recognized.

## Keyboard shortcuts

### Windows / X11

- `F8`: toggle automation on/off.
- `F9`: stop automation.

### KDE/GNOME Wayland

Wayland intentionally delegates global shortcuts to the desktop compositor. Register a global shortcut in your desktop settings and use a command such as:

```bash
/path/to/limbus_micro_manager/.venv/bin/python /path/to/limbus_micro_manager/micro_manager.py --toggle
```

For a stop-only shortcut, use `--stop`. The GUI must already be running. The command talks to the GUI over `127.0.0.1` only; it does not expose a listener to the network.

## Profiles

Use **Export profile…** and **Import profile…** to save/load JSON settings for different farming modes. Profiles store configuration and template filenames but do not bundle the template image files. Copy your template folder separately when moving profiles to another computer. Importing a profile deliberately switches input off, so review the local templates and re-enable input manually only after testing.

## Troubleshooting

- **`NOTE: You must install tkinter on Linux to use MouseInfo`:** Debian/Ubuntu: install `python3-tk python3-dev`; Fedora: install `python3-tkinter python3-devel`. Then retry `python -m pip install -r requirements.txt` using the same Python environment.
- **No state detected:** use **Analyze current screen**; verify files and scores, then recapture a tight/unique state crop. No input is sent by analysis.
- **Capture fails on Wayland:** install the screenshot backend appropriate to the desktop; `--doctor` lists commands it can find.
- **Mouse/keyboard input fails on Wayland:** install/configure `ydotool` and its daemon using your distribution's instructions. Do not blindly change permissions on `/dev/uinput`.
- **Wrong template target:** stop immediately, disable input, and recapture a smaller crop. Re-test the annotated screen before resuming.
- **Game update or UI scaling changes:** recapture affected templates and test again.
- **`--doctor` says PyAutoGUI is unavailable but you're in a headless terminal:** that can be normal without an accessible display. Run the app from your active desktop session, not from a headless SSH shell.

## Safety and known limitations

- Keep input disabled while calibrating and reviewing matches.
- The manager uses ordinary screen capture and desktop input; it does not interact with game memory.
- On unknown states or missing templates, it waits rather than guessing coordinates.
- It has not been end-to-end tested against a live game client on either platform. You must confirm the actual `P` mapping and verify every captured screen/button before using input automation.
- Automation may conflict with the game's terms or enforcement policies. Review current policies before deciding whether to use it.

## Tests

From the project folder:

```bash
python -m unittest discover -s tests -v
python -m py_compile micro_manager.py
python micro_manager.py --doctor
```

The unit tests use synthetic images and test core matching/route-selection behavior; they do not simulate a live game client.

## License

MIT for this starter code. Limbus Company and all associated game assets belong to their respective owners. No game assets are included.
