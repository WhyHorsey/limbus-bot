# Limbus Micro Manager

A small, original Python GUI starter for configurable Limbus Company farming workflows. It is inspired by the *feature categories* of LixAssistantLimbusCompany, but does not copy its source code or ship its game assets.

> **Important:** This is a template-driven foundation, not a verified turnkey bot. It does not include proprietary game screenshots, and the UI/state templates must be calibrated by you. Never run unattended until you've tested the templates with input disabled. Automation may carry account/game-rule risk; review the game's current rules and use at your own discretion.

## Features in this build

- Tkinter GUI, JSON settings, activity log.
- Mirror Dungeon / Luxcavation EXP / Luxcavation Thread mode profiles.
- Win Rate policy sends `P` when the configured battle screen is detected; optional custom key sequence or manual mode.
- Route priority for recognized *available* node types.
- Ordered E.G.O Gift, shop upgrade, and event-option image templates; optional gift blacklist.
- Stops after a configured number of completed runs if the run-complete template is recognized.
- Windows and X11 input path via PyAutoGUI.
- Wayland best-effort screen capture via `spectacle`, `gnome-screenshot`, or `grim`; input uses `ydotool` if configured.
- F8 toggles and F9 stops on Windows/X11. Wayland global shortcuts should be registered in the desktop environment and call this script with `--toggle` or `--stop`.
- Unknown screens are ignored; template/input errors stop the worker rather than continuing to click.

## Requirements

- Python 3.10+
- Tkinter (normally included on Windows; on Fedora install `python3-tkinter` if your Python environment provides it as a separate package)
- Python packages in `requirements.txt`
- On KDE Wayland, `spectacle` is the preferred capture backend; on GNOME Wayland, `gnome-screenshot`; on wlroots desktops, `grim` may work.
- On Wayland, install and configure `ydotool` and its service/daemon for automated input. This varies by distro and desktop; the program does not attempt to change device permissions or enable a privileged daemon for you.

## Install

### Windows (PowerShell)

```powershell
cd path\to\limbus_micro_manager
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py micro_manager.py
```

### Fedora Linux

```bash
sudo dnf install python3 python3-tkinter spectacle
cd /path/to/limbus_micro_manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python micro_manager.py
```

On Fedora, package names can vary depending on enabled repositories/Python version. You may use your system Python if Tkinter isn't available inside your venv.

## Configure templates

1. Launch the app once; it creates its user config and templates folder.
2. Click **Open templates folder**. The default folders are:
   - Windows: `%APPDATA%\limbus-micro-manager\templates`
   - Linux: `~/.config/limbus-micro-manager/templates`
3. Capture your own screenshots with the game in its normal display mode, then crop stable labels/panels into the filenames listed in `templates/README.txt`. Copy the image files into the user templates folder.
4. Keep the game language and UI scale consistent between calibration and use.
5. Review `config.json` in the user config folder to map extra state or action templates if needed.
6. Turn off **Allow mouse/keyboard input** and start the app. Check the activity log and template matches before enabling input.
7. Re-enable input only after every screen/action is reliably detected. Start with one run, and stay nearby to supervise it.

Template matching searches the whole display and clicks the center of the matched crop. Avoid templates that appear in more than one UI location. If your screen resolution or UI scale changes, recapture the templates.

### Decision lists

Entries are comma-separated and ordered from highest to lowest priority.

- **Route:** names such as `event, shop, rest, focused, normal, risky`. Each name maps to `node_<name>_available.png` in `node_templates`.
- **E.G.O Gifts:** exact template filenames for gift cards, e.g. `gift_my_priority.png, gift_second_choice.png`.
- **Blacklist:** filenames that the gift selector should skip.
- **Shop upgrades:** exact template filenames, highest priority first.
- **Event options:** exact template filenames, highest priority first.

This build chooses the first matched configured template. It does not infer game mechanics, evaluate gift synergies, reconstruct every dungeon path, or calculate combat odds. Your templates and priority rules supply those decisions. Team/Identity composition remains manual.

## Keyboard shortcuts

### Windows / X11

- `F8`: toggle automation.
- `F9`: stop automation.

### KDE/GNOME Wayland

Global keyboard hooks are restricted on Wayland, so register a desktop shortcut through **System Settings → Keyboard → Shortcuts** (exact labels vary) and use a command like:

```bash
/path/to/limbus_micro_manager/.venv/bin/python /path/to/limbus_micro_manager/micro_manager.py --toggle
```

Use `--stop` for an emergency stop shortcut. Launch the GUI normally first. The `--toggle` command sends a local-only command over `127.0.0.1`; it doesn't expose a listening service to your network.

## Safety and known limitations

- Do not use the bot on screens you have not calibrated.
- `P` is sent only after a screen matching `state_battle.png` is detected. The behavior of that key may change with game updates/settings.
- A generic `state_result.png` is not assumed to mean an entire run is complete; configure a distinct `state_run_complete.png` crop for a true full-run completion screen.
- Luxcavation stage selection is only triggered if a `state_lux_stage_select.png` template is detected and the matching stage-start template is found. Check that the template corresponds to the desired stage.
- Wayland permissions and screenshot/input tools differ by compositor. GUI start/stop works even when global hotkeys or input injection are unavailable.
- If the game updates, recapture the templates and re-test in dry-run mode.

## Troubleshooting

- **No screen detected:** confirm a state template exists, is cropped from the right UI, and is in the active user templates directory.
- **Wayland capture fails:** configure the desktop-appropriate screenshot utility or set `capture_command` in `config.json`, using `{output}` as the output-file placeholder.
- **Wayland clicks/keys fail:** configure `ydotool` with your distro's documentation; do not blindly change permissions on `/dev/uinput`.
- **A wrong button is selected:** immediately stop, disable clicks, and recapture a smaller/unique template with a more conservative threshold.

## License

MIT for this starter code. Limbus Company and all associated game assets belong to their respective owners. No game assets are included.
