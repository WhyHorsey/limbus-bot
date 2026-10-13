#!/usr/bin/env python3
"""Limbus Micro Manager — configurable, template-driven farming assistant.

This project does not read or modify game memory. It detects user-provided UI
image templates and issues ordinary keyboard/mouse input. It is intentionally
conservative: unknown screens are ignored.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    import pyautogui
except Exception:
    # PyAutoGUI may fail during import on headless Linux or sessions without X11.
    pyautogui = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:
    # Global hooks may be unsupported or inaccessible on Wayland/headless sessions.
    pynput_keyboard = None

APP_NAME = "LimbusMicroManager"
IPC_HOST = "127.0.0.1"
IPC_PORT = 49271

DEFAULT_CONFIG = {
    "mode": "Mirror Dungeon",
    "target_runs": 1,
    "battle_policy": "win_rate",
    "custom_battle_keys": "p",
    "loop_delay_seconds": 1.0,
    "transition_wait_seconds": 3.0,
    "template_threshold": 0.88,
    "route_priority": ["event", "shop", "rest", "focused", "normal", "risky"],
    "gift_priority": ["gift_priority_1.png", "gift_priority_2.png"],
    "gift_blacklist": [],
    "upgrade_priority": ["upgrade_priority_1.png", "upgrade_priority_2.png"],
    "event_priority": ["event_best.png", "event_safe.png", "event_skip.png"],
    "state_templates": {
        "battle": "state_battle.png",
        "map": "state_map.png",
        "gift_select": "state_gift_select.png",
        "shop": "state_shop.png",
        "event": "state_event.png",
        "result": "state_result.png",
        "run_complete": "state_run_complete.png",
        "lux_stage_select": "state_lux_stage_select.png",
    },
    "action_templates": {
        "continue": "button_continue.png",
        "start_mirror": "button_start_mirror.png",
        "start_lux_exp": "button_start_lux_exp.png",
        "start_lux_thread": "button_start_lux_thread.png",
        "retry_lux": "button_retry_lux.png",
        "enter_mirror_again": "button_enter_mirror_again.png",
        "leave_shop": "button_leave_shop.png",
        "gift_fallback": "button_gift_fallback.png",
        "event_fallback": "button_event_fallback.png",
    },
    "node_templates": {
        "event": "node_event_available.png",
        "shop": "node_shop_available.png",
        "rest": "node_rest_available.png",
        "focused": "node_focused_available.png",
        "normal": "node_normal_available.png",
        "risky": "node_risky_available.png",
    },
    "capture_command": "",
    "clicks_enabled": True,
}


def app_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = root / "limbus-micro-manager"
    path.mkdir(parents=True, exist_ok=True)
    (path / "templates").mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = app_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
TEMPLATES_DIR = DATA_DIR / "templates"
LOG_PATH = DATA_DIR / "micro-manager.log"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(copy.deepcopy(DEFAULT_CONFIG))
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    config = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(saved, dict):
        config.update(saved)
    # Preserve new default keys when opening a config made by an older version.
    for key in ("state_templates", "action_templates", "node_templates"):
        merged = copy.deepcopy(DEFAULT_CONFIG[key])
        if isinstance(config.get(key), dict):
            merged.update(config[key])
        config[key] = merged
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(CONFIG_PATH)


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def is_wayland() -> bool:
    return sys.platform.startswith("linux") and bool(os.environ.get("WAYLAND_DISPLAY"))


class DesktopIO:
    """Input/screenshot wrapper with a best-effort Wayland path."""

    LINUX_KEYCODES = {
        "esc": 1, "escape": 1, "enter": 28, "return": 28, "space": 57,
        "p": 25, "a": 30, "s": 31, "d": 32, "w": 17,
        "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7,
        "tab": 15, "f8": 66, "f9": 67,
    }

    def __init__(self, config_getter: Callable[[], dict], log: Callable[[str], None]):
        self.config_getter = config_getter
        self.log = log

    def _run_capture_command(self, output: Path) -> bool:
        cfg = self.config_getter()
        custom = str(cfg.get("capture_command", "")).strip()
        if custom:
            # {output} is replaced with the output filename; shlex is avoided intentionally.
            cmd = custom.replace("{output}", str(output))
            try:
                result = subprocess.run(cmd, shell=True, timeout=12, capture_output=True)
                return result.returncode == 0 and output.exists() and output.stat().st_size > 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        candidates: list[list[str]] = []
        if shutil.which("spectacle"):
            candidates.append([shutil.which("spectacle"), "-b", "-n", "-o", str(output)])
        if shutil.which("gnome-screenshot"):
            candidates.append([shutil.which("gnome-screenshot"), "-f", str(output)])
        if shutil.which("grim"):
            candidates.append([shutil.which("grim"), str(output)])
        for command in candidates:
            try:
                output.unlink(missing_ok=True)
                proc = subprocess.run(command, timeout=12, capture_output=True)
                if proc.returncode == 0 and output.exists() and output.stat().st_size > 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                continue
        return False

    def screenshot(self):
        if cv2 is None or np is None:
            raise RuntimeError("OpenCV isn't installed. Run: python -m pip install -r requirements.txt")
        if is_wayland():
            temp = DATA_DIR / "last-screen.png"
            if self._run_capture_command(temp):
                image = cv2.imread(str(temp), cv2.IMREAD_COLOR)
                if image is not None:
                    return image
            # Last resort: some setups allow the underlying screenshot backend.
            if pyautogui is not None:
                try:
                    shot = pyautogui.screenshot()
                    return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
                except Exception as exc:
                    raise RuntimeError(
                        "Could not capture the screen on Wayland. Install/use spectacle (KDE), "
                        "gnome-screenshot (GNOME), or grim (wlroots), then try again."
                    ) from exc
            raise RuntimeError(
                "Could not capture the screen on Wayland. Install/use spectacle (KDE), "
                "gnome-screenshot (GNOME), or grim (wlroots)."
            )
        if pyautogui is None:
            raise RuntimeError("pyautogui isn't installed. Run: python -m pip install -r requirements.txt")
        try:
            shot = pyautogui.screenshot()
            return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        except Exception as exc:
            raise RuntimeError(f"Screen capture failed: {exc}") from exc

    def click(self, x: int, y: int) -> None:
        if not self.config_getter().get("clicks_enabled", True):
            self.log(f"DRY RUN: would click ({x}, {y})")
            return
        if is_wayland():
            ydotool = shutil.which("ydotool")
            if not ydotool:
                raise RuntimeError(
                    "Wayland input requires ydotool. Install/configure its daemon, or disable clicks for dry-run."
                )
            # ydotool uses absolute screen coordinates with --absolute.
            subprocess.run([ydotool, "mousemove", "--absolute", str(x), str(y)], check=True, timeout=3)
            # ydotool's left-click code is 0xC0 on the common ydotool builds.
            subprocess.run([ydotool, "click", "0xC0"], check=True, timeout=3)
            return
        if pyautogui is None:
            raise RuntimeError("pyautogui isn't installed")
        pyautogui.click(x, y)

    def press(self, key: str) -> None:
        key = key.strip().lower()
        if not key:
            return
        if not self.config_getter().get("clicks_enabled", True):
            self.log(f"DRY RUN: would press {key}")
            return
        if is_wayland():
            ydotool = shutil.which("ydotool")
            if not ydotool:
                raise RuntimeError("Wayland key input requires ydotool and its daemon to be configured.")
            if key not in self.LINUX_KEYCODES:
                raise RuntimeError(f"Key '{key}' is not mapped for ydotool. Add it to LINUX_KEYCODES.")
            code = self.LINUX_KEYCODES[key]
            subprocess.run([ydotool, "key", f"{code}:1", f"{code}:0"], check=True, timeout=3)
            return
        if pyautogui is None:
            raise RuntimeError("pyautogui isn't installed")
        if len(key) == 1:
            pyautogui.press(key)
        else:
            pyautogui.press(key)


class TemplateMatcher:
    def __init__(self, threshold: float, log: Callable[[str], None]):
        self.threshold = max(0.5, min(0.99, float(threshold)))
        self.log = log
        self._cache: dict[str, object] = {}

    def _read_template(self, filename: str):
        if not filename:
            return None
        path = TEMPLATES_DIR / filename
        if not path.is_file():
            return None
        cached = self._cache.get(filename)
        if cached is not None:
            return cached
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            self._cache[filename] = image
        return image

    def match_all(self, screen, filename: str, threshold: Optional[float] = None) -> list[tuple[int, int, float, int, int]]:
        template = self._read_template(filename)
        if template is None:
            return []
        screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if len(screen.shape) == 3 else screen
        th, tw = template.shape[:2]
        sh, sw = screen_gray.shape[:2]
        if th > sh or tw > sw:
            return []
        result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
        threshold = self.threshold if threshold is None else threshold
        ys, xs = np.where(result >= threshold)
        candidates = sorted(((float(result[y, x]), int(x), int(y)) for x, y in zip(xs, ys)), reverse=True)
        # Greedy non-maximum suppression prevents one object becoming many candidates.
        found = []
        min_dist_x, min_dist_y = max(8, tw // 2), max(8, th // 2)
        for score, x, y in candidates:
            if any(abs(x - px) < min_dist_x and abs(y - py) < min_dist_y for px, py, *_ in found):
                continue
            found.append((x, y, score, tw, th))
            if len(found) >= 20:
                break
        return found

    def best(self, screen, filename: str, threshold: Optional[float] = None):
        matches = self.match_all(screen, filename, threshold)
        return matches[0] if matches else None

    def find_state(self, screen, state_templates: dict) -> tuple[Optional[str], float]:
        best_state, best_score = None, -1.0
        for state, filename in state_templates.items():
            match = self.best(screen, filename)
            if match and match[2] > best_score:
                best_state, best_score = state, match[2]
        return best_state, best_score


class AutomationWorker:
    def __init__(self, config_getter: Callable[[], dict], log: Callable[[str], None], status: Callable[[str], None]):
        self.config_getter = config_getter
        self.log = log
        self.status = status
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.io = DesktopIO(config_getter, log)
        self.matcher = TemplateMatcher(DEFAULT_CONFIG["template_threshold"], log)
        self.runs_done = 0
        self.last_action_time = 0.0
        self.last_state = None
        self.started = False

    def start(self):
        if self.thread and self.thread.is_alive():
            self.log("Automation is already running.")
            return
        self.stop_event.clear()
        self.runs_done = 0
        self.started = True
        self.thread = threading.Thread(target=self._run, name="limbus-automation", daemon=True)
        self.thread.start()
        self.status("Running")

    def stop(self, reason: str = "Stopped by user"):
        self.stop_event.set()
        self.status("Stopped")
        self.log(reason)

    def toggle(self):
        if self.thread and self.thread.is_alive() and not self.stop_event.is_set():
            self.stop("Stop requested.")
        else:
            self.start()

    def _cooldown(self):
        cfg = self.config_getter()
        self.last_action_time = time.monotonic()
        seconds = max(0.3, float(cfg.get("transition_wait_seconds", 3.0)))
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stop_event.is_set():
            time.sleep(min(0.1, end - time.monotonic()))

    def _click_named(self, screen, action_name: str, actions: dict) -> bool:
        filename = actions.get(action_name, "")
        match = self.matcher.best(screen, filename)
        if not match:
            self.log(f"Can't find action template '{filename}' for {action_name}; waiting instead of guessing.")
            return False
        x, y, score, w, h = match
        self.io.click(x + w // 2, y + h // 2)
        self.log(f"Clicked '{action_name}' (match {score:.2f}).")
        self._cooldown()
        return True

    def _click_priority_template(self, screen, filenames: list[str], label: str, blacklist: Optional[list[str]] = None) -> bool:
        excluded = set(blacklist or [])
        for filename in filenames:
            if filename in excluded or not filename:
                continue
            match = self.matcher.best(screen, filename)
            if match:
                x, y, score, w, h = match
                self.io.click(x + w // 2, y + h // 2)
                self.log(f"Selected {label}: {filename} (match {score:.2f}).")
                self._cooldown()
                return True
        self.log(f"No configured {label} template matched; waiting instead of guessing.")
        return False

    def _handle_state(self, state: str, screen, cfg: dict) -> None:
        actions = cfg.get("action_templates", {})
        mode = cfg.get("mode", "Mirror Dungeon")
        if state == "battle":
            policy = cfg.get("battle_policy", "win_rate")
            if policy == "win_rate":
                self.io.press("p")
                self.log("Battle: pressed P for the configured Win Rate action.")
                self._cooldown()
            elif policy == "custom_keys":
                keys = [x.strip() for x in str(cfg.get("custom_battle_keys", "p")).split(",") if x.strip()]
                if not keys:
                    self.log("Custom battle key list is empty; waiting.")
                    return
                for key in keys:
                    if self.stop_event.is_set():
                        return
                    self.io.press(key)
                    time.sleep(0.12)
                self.log(f"Battle: sent custom key sequence {keys!r}.")
                self._cooldown()
            else:
                self.log("Battle policy is manual; waiting for your input.")
                time.sleep(1)
            return

        if state == "map" and mode == "Mirror Dungeon":
            node_templates = cfg.get("node_templates", {})
            for node_type in cfg.get("route_priority", []):
                filename = node_templates.get(node_type)
                match = self.matcher.best(screen, filename or "")
                if match:
                    x, y, score, w, h = match
                    self.io.click(x + w // 2, y + h // 2)
                    self.log(f"Route: selected {node_type} node (match {score:.2f}).")
                    self._cooldown()
                    return
            self.log("No reachable-node template matched; map left untouched.")
            time.sleep(1)
            return

        if state == "gift_select":
            gifts = cfg.get("gift_priority", [])
            if self._click_priority_template(screen, gifts, "E.G.O Gift", cfg.get("gift_blacklist", [])):
                return
            # Deliberate fallback: only click if the user supplied a calibrated fallback image.
            self._click_named(screen, "gift_fallback", actions)
            return

        if state == "shop":
            upgrades = cfg.get("upgrade_priority", [])
            if self._click_priority_template(screen, upgrades, "upgrade", cfg.get("gift_blacklist", [])):
                return
            self._click_named(screen, "leave_shop", actions)
            return

        if state == "event":
            if self._click_priority_template(screen, cfg.get("event_priority", []), "event option"):
                return
            self._click_named(screen, "event_fallback", actions)
            return

        if state == "result":
            # Results from individual fights and floors are not necessarily full-run completion.
            self._click_named(screen, "continue", actions)
            return

        if state == "run_complete":
            self.runs_done += 1
            target = max(1, int(cfg.get("target_runs", 1)))
            self.log(f"Completed run {self.runs_done}/{target}.")
            if self.runs_done >= target:
                self.stop("Target run count reached.")
                return
            if mode == "Luxcavation (EXP)" or mode == "Luxcavation (Thread)":
                self._click_named(screen, "retry_lux", actions)
            else:
                self._click_named(screen, "enter_mirror_again", actions)
            return

        if state == "lux_stage_select" and mode.startswith("Luxcavation"):
            key = "start_lux_exp" if mode.endswith("EXP)") else "start_lux_thread"
            self._click_named(screen, key, actions)
            return

        self.log(f"Recognized screen '{state}', but no action is configured for it. Waiting safely.")
        time.sleep(1)

    def _run(self):
        cfg = self.config_getter()
        self.matcher = TemplateMatcher(float(cfg.get("template_threshold", 0.88)), self.log)
        missing = []
        for filename in list(cfg.get("state_templates", {}).values()):
            if filename and not (TEMPLATES_DIR / filename).exists():
                missing.append(filename)
        if missing:
            self.log(f"Template setup incomplete: {len(missing)} state templates are missing in {TEMPLATES_DIR}.")
            self.log("Capture the stable UI labels/panels described in README.md before enabling farming.")
        self.log(f"Started in mode: {cfg.get('mode')}; target runs: {cfg.get('target_runs')}.")
        self.log("Safety rule: screens that don't match a known template are never clicked.")

        while not self.stop_event.is_set():
            try:
                cfg = self.config_getter()
                screen = self.io.screenshot()
                state, score = self.matcher.find_state(screen, cfg.get("state_templates", {}))
                if state is None:
                    self.status("Waiting for known screen")
                    time.sleep(max(0.6, float(cfg.get("loop_delay_seconds", 1.0))))
                    continue
                if state != self.last_state:
                    self.log(f"Detected screen: {state} (confidence {score:.2f}).")
                    self.last_state = state
                self.status(f"Running · {state}")
                self._handle_state(state, screen, cfg)
                time.sleep(max(0.2, float(cfg.get("loop_delay_seconds", 1.0))))
            except Exception as exc:
                self.log(f"Automation paused after error: {exc}")
                self.status("Paused after error")
                # Stop instead of spamming possibly unsafe inputs after backend failure.
                self.stop_event.set()
                break
        self.status("Stopped")
        self.started = False


class MicroManagerApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, messagebox, simpledialog

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.simpledialog = simpledialog
        self.root = root
        self.root.title("Limbus Micro Manager")
        self.root.geometry("820x760")
        self.root.minsize(720, 640)
        self.config = load_config()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.status_var = tk.StringVar(value="Stopped")
        self.mode_var = tk.StringVar(value=self.config.get("mode", "Mirror Dungeon"))
        self.runs_var = tk.StringVar(value=str(self.config.get("target_runs", 1)))
        self.policy_var = tk.StringVar(value=self.config.get("battle_policy", "win_rate"))
        self.keys_var = tk.StringVar(value=self.config.get("custom_battle_keys", "p"))
        self.loop_delay_var = tk.StringVar(value=str(self.config.get("loop_delay_seconds", 1.0)))
        self.transition_var = tk.StringVar(value=str(self.config.get("transition_wait_seconds", 3.0)))
        self.threshold_var = tk.StringVar(value=str(self.config.get("template_threshold", 0.88)))
        self.route_var = tk.StringVar(value=", ".join(self.config.get("route_priority", [])))
        self.gift_var = tk.StringVar(value=", ".join(self.config.get("gift_priority", [])))
        self.blacklist_var = tk.StringVar(value=", ".join(self.config.get("gift_blacklist", [])))
        self.upgrade_var = tk.StringVar(value=", ".join(self.config.get("upgrade_priority", [])))
        self.event_var = tk.StringVar(value=", ".join(self.config.get("event_priority", [])))
        self.clicks_enabled_var = tk.BooleanVar(value=self.config.get("clicks_enabled", True))

        self.worker = AutomationWorker(self.get_config, self.enqueue_log, self.set_status)
        self._build_ui()
        self._start_ipc_server()
        self._start_hotkey_listener()
        self.root.after(120, self._flush_log_queue)
        self.enqueue_log(f"Config: {CONFIG_PATH}")
        self.enqueue_log(f"Templates: {TEMPLATES_DIR}")
        if is_wayland():
            self.enqueue_log("Wayland detected: use your desktop's global shortcut to run this script with --toggle.")
            self.enqueue_log("Screen capture needs a supported screenshot utility; mouse/keyboard input needs ydotool configured.")
        self._write_startup_notice()

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Limbus Micro Manager", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(header, text="Template-driven farming assistant · unknown screens are ignored").pack(anchor="w", pady=(2, 8))

        settings = ttk.LabelFrame(outer, text="Farming setup", padding=10)
        settings.pack(fill="x", pady=(0, 8))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        def label(text, row, col=0):
            ttk.Label(settings, text=text).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=4)

        label("Mode", 0)
        ttk.Combobox(settings, textvariable=self.mode_var, values=["Mirror Dungeon", "Luxcavation (EXP)", "Luxcavation (Thread)"], state="readonly", width=24).grid(row=0, column=1, sticky="ew", pady=4)
        label("Target full runs", 0, 2)
        ttk.Spinbox(settings, from_=1, to=999, textvariable=self.runs_var, width=8).grid(row=0, column=3, sticky="w", pady=4)

        label("Battle policy", 1)
        ttk.Combobox(settings, textvariable=self.policy_var, values=["win_rate", "custom_keys", "manual"], state="readonly", width=22).grid(row=1, column=1, sticky="ew", pady=4)
        label("Custom keys", 1, 2)
        ttk.Entry(settings, textvariable=self.keys_var).grid(row=1, column=3, sticky="ew", pady=4)

        label("Loop delay (s)", 2)
        ttk.Entry(settings, textvariable=self.loop_delay_var, width=10).grid(row=2, column=1, sticky="ew", pady=4)
        label("Transition wait (s)", 2, 2)
        ttk.Entry(settings, textvariable=self.transition_var, width=10).grid(row=2, column=3, sticky="ew", pady=4)

        label("Template threshold", 3)
        ttk.Entry(settings, textvariable=self.threshold_var, width=10).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(settings, text="Allow mouse/keyboard input (turn off for dry-run)", variable=self.clicks_enabled_var).grid(row=3, column=2, columnspan=2, sticky="w", pady=4)

        priorities = ttk.LabelFrame(outer, text="Decision priorities", padding=10)
        priorities.pack(fill="x", pady=(0, 8))
        priorities.columnconfigure(1, weight=1)
        for row, text, var, hint in [
            (0, "Route", self.route_var, "node types, comma-separated"),
            (1, "E.G.O Gifts", self.gift_var, "template filenames, best first"),
            (2, "Blacklist", self.blacklist_var, "gift template filenames to ignore"),
            (3, "Shop upgrades", self.upgrade_var, "upgrade template filenames, best first"),
            (4, "Event options", self.event_var, "event option templates, best first"),
        ]:
            ttk.Label(priorities, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(priorities, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Label(priorities, text=hint, foreground="#666666").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=3)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Start (F8)", command=self.start).pack(side="left")
        ttk.Button(buttons, text="Stop (F9)", command=self.stop).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Save settings", command=self.save_from_ui).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Open templates folder", command=self.open_templates).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Capture template…", command=self.capture_template).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Test current screen", command=self.test_current_screen).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Check templates", command=self.check_templates).pack(side="left", padx=(6, 0))

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(0, 6))
        ttk.Label(status, text="Status:").pack(side="left")
        ttk.Label(status, textvariable=self.status_var, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=(4, 0))

        log_frame = ttk.LabelFrame(outer, text="Activity log", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_box = tk.Text(log_frame, height=12, wrap="word", state="disabled", font=("TkFixedFont", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Label(outer, text=f"Log file: {LOG_PATH}", foreground="#666666").pack(anchor="w", pady=(6, 0))

    def _write_startup_notice(self):
        self.enqueue_log("Note: the team is not changed automatically. Set up the desired team/stage in-game first.")
        self.enqueue_log("This build needs calibrated templates; it is a configurable foundation, not a verified ready-made bot.")

    def get_config(self) -> dict:
        return copy.deepcopy(self.config)

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    def save_from_ui(self, quiet=False):
        try:
            cfg = copy.deepcopy(self.config)
            cfg["mode"] = self.mode_var.get()
            cfg["target_runs"] = max(1, min(999, int(self.runs_var.get())))
            cfg["battle_policy"] = self.policy_var.get()
            cfg["custom_battle_keys"] = self.keys_var.get().strip() or "p"
            cfg["loop_delay_seconds"] = max(0.2, min(10.0, float(self.loop_delay_var.get())))
            cfg["transition_wait_seconds"] = max(0.5, min(30.0, float(self.transition_var.get())))
            cfg["template_threshold"] = max(0.5, min(0.99, float(self.threshold_var.get())))
            cfg["route_priority"] = self._csv(self.route_var.get())
            cfg["gift_priority"] = self._csv(self.gift_var.get())
            cfg["gift_blacklist"] = self._csv(self.blacklist_var.get())
            cfg["upgrade_priority"] = self._csv(self.upgrade_var.get())
            cfg["event_priority"] = self._csv(self.event_var.get())
            cfg["clicks_enabled"] = bool(self.clicks_enabled_var.get())
            save_config(cfg)
            self.config = cfg
            if hasattr(self, "worker"):
                self.worker.matcher = TemplateMatcher(float(cfg["template_threshold"]), self.enqueue_log)
            if not quiet:
                self.enqueue_log("Settings saved.")
            return True
        except (ValueError, OSError) as exc:
            self.messagebox.showerror("Invalid settings", str(exc))
            return False

    def start(self):
        if not self.save_from_ui(quiet=True):
            return
        if not self.config.get("clicks_enabled", True):
            self.enqueue_log("DRY-RUN mode is active: no actual clicks or key presses will be sent.")
        self.worker.start()

    def stop(self):
        self.worker.stop("Stop button/hotkey pressed.")

    def set_status(self, value: str):
        self.root.after(0, lambda: self.status_var.set(value))

    def enqueue_log(self, message: str):
        logging.info(message)
        self.log_queue.put(message)

    def _flush_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", time.strftime("%H:%M:%S ") + message + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._flush_log_queue)

    def capture_template(self):
        """Minimize this window, capture the game screen, and crop a template via OpenCV."""
        if cv2 is None or np is None:
            self.messagebox.showerror("Missing dependency", "Install OpenCV with: python -m pip install -r requirements.txt")
            return
        filename = self.simpledialog.askstring(
            "Capture template",
            "Template filename (e.g. state_battle.png or node_shop_available.png):",
            parent=self.root,
        )
        if not filename:
            return
        filename = Path(filename.strip()).name
        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
            filename += ".png"
        if filename in {".", ".."}:
            self.messagebox.showerror("Invalid filename", "Choose a valid image filename.")
            return
        overwrite = (TEMPLATES_DIR / filename).exists()
        if overwrite and not self.messagebox.askyesno("Overwrite template?", f"{filename} already exists. Replace it?"):
            return

        self.root.iconify()
        self.enqueue_log(f"Capturing template '{filename}' in 1.2 seconds. Bring the game to the foreground now.")
        def do_capture():
            try:
                cfg = self.get_config()
                io = DesktopIO(lambda: cfg, self.enqueue_log)
                screen = io.screenshot()
                cv2.namedWindow("Select template crop", cv2.WINDOW_NORMAL)
                roi = cv2.selectROI("Select template crop", screen, showCrosshair=True, fromCenter=False)
                cv2.destroyWindow("Select template crop")
                x, y, w, h = [int(v) for v in roi]
                if w < 4 or h < 4:
                    self.enqueue_log("Template capture cancelled (empty selection).")
                    return
                TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                destination = TEMPLATES_DIR / filename
                if not cv2.imwrite(str(destination), screen[y:y+h, x:x+w]):
                    raise RuntimeError("OpenCV could not write the selected image.")
                self.enqueue_log(f"Saved template: {destination} ({w}×{h}).")
                self.enqueue_log("Make sure the chosen crop is unique and stable; use Test current screen / dry-run before enabling input.")
            except Exception as exc:
                self.enqueue_log(f"Template capture failed: {exc}")
                self.messagebox.showerror("Template capture failed", str(exc))
            finally:
                try:
                    self.root.deiconify()
                    self.root.lift()
                except Exception:
                    pass
        self.root.after(1200, do_capture)

    def test_current_screen(self):
        try:
            cfg = self.get_config()
            io = DesktopIO(lambda: cfg, self.enqueue_log)
            screen = io.screenshot()
            matcher = TemplateMatcher(float(cfg.get("template_threshold", 0.88)), self.enqueue_log)
            state, score = matcher.find_state(screen, cfg.get("state_templates", {}))
            if state is None:
                self.enqueue_log("Screen test: no state template matched. No input was sent.")
                self.messagebox.showwarning("Screen test", "No configured state template matched this screen. No input was sent.\n\nCapture/configure a state template for the current screen.")
            else:
                self.enqueue_log(f"Screen test matched: {state} (confidence {score:.2f}). No input was sent.")
                self.messagebox.showinfo("Screen test", f"Detected state: {state}\nConfidence: {score:.2f}\n\nNo input was sent.")
        except Exception as exc:
            self.enqueue_log(f"Screen test failed: {exc}")
            self.messagebox.showerror("Screen test failed", str(exc))

    def check_templates(self):
        config = self.get_config()
        required = set(config.get("state_templates", {}).values())
        missing = sorted(name for name in required if name and not (TEMPLATES_DIR / name).exists())
        found = sorted(name for name in required if name and (TEMPLATES_DIR / name).exists())
        message = f"State templates found: {len(found)}\nMissing: {len(missing)}\n\nFolder: {TEMPLATES_DIR}"
        if missing:
            message += "\n\nMissing files:\n" + "\n".join(missing[:20])
        self.messagebox.showinfo("Template check", message)

    def open_templates(self):
        try:
            if os.name == "nt":
                os.startfile(str(TEMPLATES_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(TEMPLATES_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(TEMPLATES_DIR)])
        except Exception as exc:
            self.messagebox.showerror("Could not open folder", str(exc))

    def _start_hotkey_listener(self):
        if is_wayland():
            # Global key listening is intentionally delegated to the compositor under Wayland.
            return
        if pynput_keyboard is None:
            self.enqueue_log("pynput not installed: F8/F9 global shortcuts unavailable; GUI buttons still work.")
            return

        pressed = set()
        def on_press(key):
            if key in pressed:
                return
            pressed.add(key)
            if key == pynput_keyboard.Key.f8:
                self.root.after(0, self.worker.toggle)
            elif key == pynput_keyboard.Key.f9:
                self.root.after(0, self.worker.stop)
        def on_release(key):
            pressed.discard(key)
        self.hotkey_listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

    def _start_ipc_server(self):
        self.ipc_stop = threading.Event()
        self.ipc_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ipc_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.ipc_socket.bind((IPC_HOST, IPC_PORT))
            self.ipc_socket.listen(5)
            self.ipc_socket.settimeout(0.5)
        except OSError as exc:
            self.enqueue_log(f"Could not start shortcut listener on {IPC_HOST}:{IPC_PORT}: {exc}")
            return

        def loop():
            while not self.ipc_stop.is_set():
                try:
                    conn, _ = self.ipc_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        command = conn.recv(100).decode("utf-8", "replace").strip().lower()
                        conn.sendall(b"ok")
                    except OSError:
                        continue
                if command == "toggle":
                    self.root.after(0, self.worker.toggle)
                elif command == "start":
                    self.root.after(0, self.start)
                elif command == "stop":
                    self.root.after(0, self.stop)
        self.ipc_thread = threading.Thread(target=loop, name="micro-manager-ipc", daemon=True)
        self.ipc_thread.start()

    def close(self):
        self.worker.stop("Application closing.")
        if hasattr(self, "hotkey_listener"):
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        if hasattr(self, "ipc_stop"):
            self.ipc_stop.set()
        if hasattr(self, "ipc_socket"):
            try:
                self.ipc_socket.close()
            except OSError:
                pass
        self.root.destroy()


def send_ipc_command(command: str) -> bool:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=0.6) as conn:
            conn.sendall(command.encode("ascii"))
            conn.settimeout(0.6)
            conn.recv(16)
        return True
    except OSError:
        return False


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="Limbus Micro Manager")
    parser.add_argument("--toggle", action="store_true", help="toggle Start/Stop in an already running app")
    parser.add_argument("--start", action="store_true", help="start automation in an already running app")
    parser.add_argument("--stop", action="store_true", help="stop automation in an already running app")
    args = parser.parse_args()
    setup_logging()
    command = "toggle" if args.toggle else "start" if args.start else "stop" if args.stop else None
    if command:
        # Retry briefly so desktop shortcuts issued while the app launches do not race IPC startup.
        for _ in range(4):
            if send_ipc_command(command):
                return 0
            time.sleep(0.15)
        print("Limbus Micro Manager is not running. Launch it normally first.", file=sys.stderr)
        return 2

    if cv2 is None or np is None:
        print("Missing dependencies. Install them with: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is not available in this Python installation.", file=sys.stderr)
        return 1
    root = tk.Tk()
    app = MicroManagerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
