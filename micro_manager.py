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
import re
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

_PYAUTOGUI_IMPORT_ERROR = None
try:
    import pyautogui
except Exception as exc:
    # PyAutoGUI may fail during import on headless Linux or sessions without X11.
    pyautogui = None
    _PYAUTOGUI_IMPORT_ERROR = str(exc)

try:
    from pynput import keyboard as pynput_keyboard
except Exception:
    # Global hooks may be unsupported or inaccessible on Wayland/headless sessions.
    pynput_keyboard = None

APP_NAME = "LimbusMicroManager"
IPC_HOST = "127.0.0.1"
IPC_PORT = 49271

DEFAULT_CONFIG = {
    "config_version": 2,
    "mode": "Mirror Dungeon",
    "target_runs": 1,
    "battle_policy": "win_rate",
    "custom_battle_keys": "p",
    "loop_delay_seconds": 1.0,
    "transition_wait_seconds": 3.0,
    "template_threshold": 0.88,
    "screen_change_threshold": 0.08,
    "route_strategy": "priority_then_confidence",
    "route_priority": ["event", "shop", "rest", "focused", "normal", "risky"],
    "gift_priority": [],
    "gift_blacklist": [],
    "upgrade_priority": [],
    "event_priority": [],
    "state_templates": {
        "battle": "state_battle.png",
        "map": "state_map.png",
        "gift_select": "state_gift_select.png",
        "shop": "state_shop.png",
        "event": "state_event.png",
        "result": "state_result.png",
        "run_complete": "state_run_complete.png",
        "lux_stage_select": "state_lux_stage_select.png",
        "mode_select": "state_mode_select.png",
        "gift_upgrade": "state_gift_upgrade.png",
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
        "confirm_upgrade": "button_confirm_upgrade.png",
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
    "clicks_enabled": False,
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
    if not isinstance(saved, dict):
        saved = {}
    try:
        previous_version = int(saved.get("config_version", 1))
    except (TypeError, ValueError):
        previous_version = 1
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(saved)
    # Preserve new defaults and nested mappings when opening an older config.
    for key in ("state_templates", "action_templates", "node_templates"):
        merged = copy.deepcopy(DEFAULT_CONFIG[key])
        if isinstance(config.get(key), dict):
            merged.update(config[key])
        config[key] = merged
    # Older builds defaulted live input to enabled. On upgrade, move to a safe
    # dry-run until the user explicitly re-enables input in the GUI.
    if previous_version < 2:
        config["clicks_enabled"] = False
    config["config_version"] = 2
    if previous_version < 2 or saved.get("config_version") != 2:
        save_config(config)
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

    def best_unfiltered(self, screen, filename: str):
        """Return the best raw match even below threshold; diagnostics only."""
        template = self._read_template(filename)
        if template is None:
            return None
        screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if len(screen.shape) == 3 else screen
        th, tw = template.shape[:2]
        sh, sw = screen_gray.shape[:2]
        if th > sh or tw > sw:
            return None
        result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
        return (int(max_loc[0]), int(max_loc[1]), float(max_val), int(tw), int(th))

    def clear_cache(self) -> None:
        self._cache.clear()

    def find_state(self, screen, state_templates: dict) -> tuple[Optional[str], float]:
        best_state, best_score = None, -1.0
        for state, filename in state_templates.items():
            match = self.best(screen, filename)
            if match and match[2] > best_score:
                best_state, best_score = state, match[2]
        return best_state, best_score


def screen_signature(screen):
    """Small grayscale signature used to prevent repeat-clicking an unchanged screen."""
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if len(screen.shape) == 3 else screen
    reduced = cv2.resize(gray, (48, 27), interpolation=cv2.INTER_AREA)
    return reduced.astype(np.float32)


def signature_delta(first, second) -> float:
    if first is None or second is None or first.shape != second.shape:
        return 1.0
    return float(np.mean(cv2.absdiff(first, second)) / 255.0)


def choose_route_match(matches, strategy: str, screen_width: int, screen_height: int):
    """Choose one of a node type's image matches using a configurable tie-breaker."""
    if not matches:
        return None
    if strategy == "priority_then_leftmost":
        return min(matches, key=lambda item: item[0])
    if strategy == "priority_then_rightmost":
        return max(matches, key=lambda item: item[0] + item[3] // 2)
    if strategy == "priority_then_center":
        return min(matches, key=lambda item: (
            (item[0] + item[3] / 2 - screen_width / 2) ** 2
            + (item[1] + item[4] / 2 - screen_height / 2) ** 2
        ))
    # match_all returns matches in descending confidence order.
    return matches[0]


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
        self.last_action_state: Optional[str] = None
        self.last_action_signature = None
        self.current_state: Optional[str] = None
        self.run_complete_latched = False
        self.last_wait_log: dict[str, float] = {}
        self.started = False

    def start(self):
        if self.thread and self.thread.is_alive():
            self.log("Automation is already running.")
            return
        self.stop_event.clear()
        self.runs_done = 0
        self.last_state = None
        self.last_action_state = None
        self.last_action_signature = None
        self.current_state = None
        self.run_complete_latched = False
        self.started = True
        self.status("Starting")
        self.thread = threading.Thread(target=self._run, name="limbus-automation", daemon=True)
        self.thread.start()

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

    def _record_action(self, screen) -> None:
        """Latch the action until the recognized screen changes materially."""
        self.last_action_state = self.current_state
        self.last_action_signature = screen_signature(screen)
        self.last_action_time = time.monotonic()

    def _may_act_on_screen(self, state: str, screen, cfg: dict) -> bool:
        if self.last_action_state != state:
            return True
        # Never count/re-enter the full-run completion action twice until a
        # different known state appears. This avoids falsely counting one run
        # multiple times when the completion screen stays visible.
        if state == "run_complete":
            return False
        delta = signature_delta(screen_signature(screen), self.last_action_signature)
        threshold = max(0.05, min(0.5, float(cfg.get("screen_change_threshold", 0.15))))
        if delta >= threshold:
            self.log(f"Screen '{state}' changed (visual delta {delta:.2f}); allowing next configured action.")
            return True
        return False

    def _log_throttled(self, key: str, message: str, interval: float = 8.0) -> None:
        now = time.monotonic()
        if now - self.last_wait_log.get(key, 0.0) >= interval:
            self.last_wait_log[key] = now
            self.log(message)

    def _click_named(self, screen, action_name: str, actions: dict) -> bool:
        filename = actions.get(action_name, "")
        match = self.matcher.best(screen, filename)
        if not match:
            self._log_throttled(f"action:{action_name}", f"Can't find action template '{filename}' for {action_name}; waiting instead of guessing.")
            return False
        x, y, score, w, h = match
        self.io.click(x + w // 2, y + h // 2)
        self._record_action(screen)
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
                self._record_action(screen)
                self.log(f"Selected {label}: {filename} (match {score:.2f}).")
                self._cooldown()
                return True
        self._log_throttled(f"priority:{label}", f"No configured {label} template matched; waiting instead of guessing.")
        return False

    def _handle_state(self, state: str, screen, cfg: dict) -> None:
        actions = cfg.get("action_templates", {})
        mode = cfg.get("mode", "Mirror Dungeon")
        if state == "battle":
            policy = cfg.get("battle_policy", "win_rate")
            if policy == "win_rate":
                self.io.press("p")
                self._record_action(screen)
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
                self._record_action(screen)
                self.log(f"Battle: sent custom key sequence {keys!r}.")
                self._cooldown()
            else:
                self._log_throttled("battle:manual", "Battle policy is manual; waiting for your input.")
                time.sleep(1)
            return

        if state == "map" and mode == "Mirror Dungeon":
            node_templates = cfg.get("node_templates", {})
            strategy = cfg.get("route_strategy", "priority_then_confidence")
            screen_h, screen_w = screen.shape[:2]
            for node_type in cfg.get("route_priority", []):
                filename = node_templates.get(node_type)
                matches = self.matcher.match_all(screen, filename or "")
                if not matches:
                    continue
                # Route type priority is applied first. If several available
                # nodes share that type, this second policy picks the location.
                match = choose_route_match(matches, strategy, screen_w, screen_h)
                x, y, score, w, h = match
                self.io.click(x + w // 2, y + h // 2)
                self._record_action(screen)
                self.log(f"Route: selected {node_type} node using {strategy} (match {score:.2f}).")
                self._cooldown()
                return
            self._log_throttled("map:no-node", "No reachable-node template matched; map left untouched.")
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
            if self._click_priority_template(screen, upgrades, "upgrade"):
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
            if self.run_complete_latched:
                self._log_throttled("runcomplete:latched", "Run-complete screen already counted; waiting for a different recognized screen.")
                return
            self.run_complete_latched = True
            self.runs_done += 1
            target = max(1, int(cfg.get("target_runs", 1)))
            self.log(f"Completed run {self.runs_done}/{target}.")
            if self.runs_done >= target:
                self.stop("Target run count reached.")
                return
            if mode in ("Luxcavation (EXP)", "Luxcavation (Thread)"):
                self._click_named(screen, "retry_lux", actions)
            else:
                self._click_named(screen, "enter_mirror_again", actions)
            return

        if state == "mode_select" and mode == "Mirror Dungeon":
            self._click_named(screen, "start_mirror", actions)
            return

        if state == "gift_upgrade":
            if self._click_priority_template(screen, cfg.get("upgrade_priority", []), "E.G.O Gift upgrade"):
                return
            # Confirmation is only attempted if the user has explicitly calibrated it.
            self._click_named(screen, "confirm_upgrade", actions)
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
        present_states = [name for name in cfg.get("state_templates", {}).values() if name and (TEMPLATES_DIR / name).is_file()]
        if cfg.get("clicks_enabled", False) and not present_states:
            self.log("Preflight failed: no state templates are available. Input automation was not started.")
            self.status("Preflight failed")
            self.stop_event.set()
            self.started = False
            return
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
                    if state != "run_complete":
                        self.run_complete_latched = False
                self.current_state = state
                if not self._may_act_on_screen(state, screen, cfg):
                    self.status(f"Waiting for screen change · {state}")
                    time.sleep(max(0.3, float(cfg.get("loop_delay_seconds", 1.0))))
                    continue
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
        from tkinter import ttk, messagebox, simpledialog, filedialog

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.simpledialog = simpledialog
        self.filedialog = filedialog
        self.root = root
        self.root.title("Limbus Micro Manager")
        available_w = max(850, self.root.winfo_screenwidth() - 60)
        available_h = max(660, self.root.winfo_screenheight() - 80)
        initial_w = min(1080, available_w)
        initial_h = min(800, available_h)
        self.root.geometry(f"{initial_w}x{initial_h}")
        self.root.minsize(min(900, initial_w), min(660, initial_h))
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
        self.screen_change_var = tk.StringVar(value=str(self.config.get("screen_change_threshold", 0.08)))
        self.route_strategy_var = tk.StringVar(value=self.config.get("route_strategy", "priority_then_confidence"))
        self.route_var = tk.StringVar(value=", ".join(self.config.get("route_priority", [])))
        self.gift_var = tk.StringVar(value=", ".join(self.config.get("gift_priority", [])))
        self.blacklist_var = tk.StringVar(value=", ".join(self.config.get("gift_blacklist", [])))
        self.upgrade_var = tk.StringVar(value=", ".join(self.config.get("upgrade_priority", [])))
        self.event_var = tk.StringVar(value=", ".join(self.config.get("event_priority", [])))
        self.clicks_enabled_var = tk.BooleanVar(value=self.config.get("clicks_enabled", False))

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

        label("Route tie-breaker", 4)
        ttk.Combobox(
            settings, textvariable=self.route_strategy_var,
            values=["priority_then_confidence", "priority_then_leftmost", "priority_then_rightmost", "priority_then_center"],
            state="readonly", width=26,
        ).grid(row=4, column=1, sticky="ew", pady=4)
        label("Screen-change threshold", 4, 2)
        ttk.Entry(settings, textvariable=self.screen_change_var, width=10).grid(row=4, column=3, sticky="w", pady=4)
        ttk.Label(settings, text="Higher values require a bigger visual change before the same screen can trigger another action.", foreground="#666666").grid(row=5, column=0, columnspan=4, sticky="w", pady=(0, 2))

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

        buttons_primary = ttk.Frame(outer)
        buttons_primary.pack(fill="x", pady=(0, 4))
        ttk.Button(buttons_primary, text="Start (F8 toggles)", command=self.start).pack(side="left")
        ttk.Button(buttons_primary, text="Stop (F9)", command=self.stop).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_primary, text="Save settings", command=self.save_from_ui).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_primary, text="Export profile…", command=self.export_profile).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_primary, text="Import profile…", command=self.import_profile).pack(side="left", padx=(6, 0))

        buttons_tools = ttk.Frame(outer)
        buttons_tools.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons_tools, text="Open templates folder", command=self.open_templates).pack(side="left")
        ttk.Button(buttons_tools, text="Calibrate / capture…", command=self.capture_template).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_tools, text="Manage templates…", command=self.manage_templates).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_tools, text="Analyze current screen", command=self.test_current_screen).pack(side="left", padx=(6, 0))
        ttk.Button(buttons_tools, text="Check templates", command=self.check_templates).pack(side="left", padx=(6, 0))

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
            cfg["screen_change_threshold"] = max(0.05, min(0.5, float(self.screen_change_var.get())))
            cfg["route_strategy"] = self.route_strategy_var.get()
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

    def export_profile(self):
        if not self.save_from_ui(quiet=True):
            return
        path = self.filedialog.asksaveasfilename(
            title="Export farming profile",
            defaultextension=".json",
            filetypes=[("JSON profile", "*.json")],
            initialfile=f"limbus-{self.config.get('mode', 'profile').lower().replace(' ', '-').replace('(', '').replace(')', '')}.json",
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self.enqueue_log(f"Exported settings profile to {path}. Template image files are not bundled with profiles.")
        except OSError as exc:
            self.messagebox.showerror("Profile export failed", str(exc))

    def import_profile(self):
        path = self.filedialog.askopenfilename(
            title="Import farming profile",
            filetypes=[("JSON profile", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            profile = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(profile, dict):
                raise ValueError("The profile root must be a JSON object.")
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg.update(profile)
            for key in ("state_templates", "action_templates", "node_templates"):
                merged = copy.deepcopy(DEFAULT_CONFIG[key])
                if isinstance(cfg.get(key), dict):
                    merged.update(cfg[key])
                cfg[key] = merged
            if cfg.get("mode") not in ("Mirror Dungeon", "Luxcavation (EXP)", "Luxcavation (Thread)"):
                raise ValueError("Profile has an unsupported farming mode.")
            if cfg.get("battle_policy") not in ("win_rate", "custom_keys", "manual"):
                raise ValueError("Profile has an unsupported battle policy.")
            cfg["clicks_enabled"] = False
            cfg["config_version"] = 2
            self.config = cfg
            self.mode_var.set(cfg["mode"])
            self.runs_var.set(str(cfg.get("target_runs", 1)))
            self.policy_var.set(cfg.get("battle_policy", "win_rate"))
            self.keys_var.set(cfg.get("custom_battle_keys", "p"))
            self.loop_delay_var.set(str(cfg.get("loop_delay_seconds", 1.0)))
            self.transition_var.set(str(cfg.get("transition_wait_seconds", 3.0)))
            self.threshold_var.set(str(cfg.get("template_threshold", 0.88)))
            self.screen_change_var.set(str(cfg.get("screen_change_threshold", 0.08)))
            self.route_strategy_var.set(cfg.get("route_strategy", "priority_then_confidence"))
            self.route_var.set(", ".join(cfg.get("route_priority", [])))
            self.gift_var.set(", ".join(cfg.get("gift_priority", [])))
            self.blacklist_var.set(", ".join(cfg.get("gift_blacklist", [])))
            self.upgrade_var.set(", ".join(cfg.get("upgrade_priority", [])))
            self.event_var.set(", ".join(cfg.get("event_priority", [])))
            self.clicks_enabled_var.set(False)
            if self.save_from_ui(quiet=True):
                self.enqueue_log(f"Imported profile from {path}. Confirm the template files exist locally before enabling input.")
                self.worker.matcher = TemplateMatcher(float(self.config.get("template_threshold", 0.88)), self.enqueue_log)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.messagebox.showerror("Profile import failed", str(exc))

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
        """Open a guided, role-aware template calibration wizard."""
        if self.worker.thread and self.worker.thread.is_alive() and not self.worker.stop_event.is_set():
            self.messagebox.showwarning("Stop automation first", "Stop the farming loop before calibrating templates so the screen is not being changed by automation.")
            return
        tk, ttk = self.tk, self.ttk
        if cv2 is None or np is None:
            self.messagebox.showerror("Missing dependency", "Install OpenCV with: python -m pip install -r requirements.txt")
            return

        cfg = self.get_config()
        wizard = tk.Toplevel(self.root)
        wizard.title("Template calibration wizard")
        wizard.transient(self.root)
        wizard.resizable(False, False)
        frame = ttk.Frame(wizard, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Calibrate a UI template", font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Choose what the crop represents. State and button captures are automatically linked to the config.", wraplength=500).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        category_var = tk.StringVar(value="State screen")
        target_var = tk.StringVar(value="battle")
        name_var = tk.StringVar(value="")
        ttk.Label(frame, text="Template role").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        category_box = ttk.Combobox(frame, textvariable=category_var, state="readonly", width=28, values=[
            "State screen", "Action button", "Route node", "E.G.O Gift priority",
            "Shop upgrade priority", "Event option priority", "Gift blacklist",
        ])
        category_box.grid(row=2, column=1, sticky="ew", pady=5)
        target_label = ttk.Label(frame, text="State key")
        target_label.grid(row=3, column=0, sticky="w", padx=(0, 10), pady=5)
        target_box = ttk.Combobox(frame, textvariable=target_var, state="readonly", width=28)
        target_box.grid(row=3, column=1, sticky="ew", pady=5)
        name_label = ttk.Label(frame, text="Gift/upgrade name")
        name_label.grid(row=4, column=0, sticky="w", padx=(0, 10), pady=5)
        name_entry = ttk.Entry(frame, textvariable=name_var, width=30)
        name_entry.grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Tip: crop a unique heading, button, or card edge; avoid animated text and common backgrounds.", wraplength=500, foreground="#666666").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 12))

        mapped_categories = {
            "State screen": ("state_templates", "State key"),
            "Action button": ("action_templates", "Action key"),
            "Route node": ("node_templates", "Node type"),
        }
        def refresh_target(_event=None):
            cat = category_var.get()
            if cat in mapped_categories:
                dict_key, label_text = mapped_categories[cat]
                keys = list(cfg.get(dict_key, {}).keys())
                target_box.configure(values=keys, state="readonly")
                target_label.configure(text=label_text)
                if target_var.get() not in keys:
                    target_var.set(keys[0] if keys else "")
                name_entry.configure(state="disabled")
                name_label.configure(text="Filename generated")
                name_var.set("")
            else:
                target_box.configure(values=[], state="disabled")
                target_var.set("")
                target_label.configure(text="Target")
                name_entry.configure(state="normal")
                name_label.configure(text="Short name / filename")
                if not name_var.get():
                    name_var.set("priority_1")
        category_box.bind("<<ComboboxSelected>>", refresh_target)
        refresh_target()

        def start_crop():
            cat = category_var.get()
            target = target_var.get().strip()
            raw_name = name_var.get().strip()
            if cat in mapped_categories and not target:
                self.messagebox.showerror("Missing target", "Choose a target key.", parent=wizard)
                return
            if cat not in mapped_categories and not raw_name:
                self.messagebox.showerror("Missing name", "Enter a short name for this gift/upgrade/event option.", parent=wizard)
                return

            if cat == "State screen":
                filename = f"state_{target}.png"
            elif cat == "Action button":
                filename = f"button_{target}.png"
            elif cat == "Route node":
                filename = f"node_{target}_available.png"
            else:
                stem = Path(raw_name).stem.lower()
                stem = re.sub(r"[^a-z0-9_-]+", "_", stem).strip("_") or "priority_1"
                prefix = {
                    "E.G.O Gift priority": "gift_",
                    "Shop upgrade priority": "upgrade_",
                    "Event option priority": "event_",
                    "Gift blacklist": "gift_",
                }[cat]
                if not stem.startswith(prefix):
                    stem = prefix + stem
                filename = stem + ".png"
            filename = Path(filename).name
            dest = TEMPLATES_DIR / filename
            if dest.exists() and not self.messagebox.askyesno("Overwrite template?", f"{filename} already exists. Replace it?", parent=wizard):
                return
            wizard.destroy()
            self._capture_crop_and_bind(cat, target, filename)

        footer = ttk.Frame(frame)
        footer.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(footer, text="Cancel", command=wizard.destroy).pack(side="right")
        ttk.Button(footer, text="Capture screen and crop…", command=start_crop).pack(side="right", padx=(0, 8))
        wizard.grab_set()
        wizard.focus_set()

    def _capture_crop_and_bind(self, category: str, target: str, filename: str) -> None:
        """Capture the game without the main window, then let the user drag a crop in Tk."""
        self.root.iconify()
        self.enqueue_log(f"Preparing template capture for {filename}; bring the game to the foreground now.")

        def do_capture():
            crop_window = None
            try:
                from PIL import Image, ImageTk
                cfg = self.get_config()
                io = DesktopIO(lambda: cfg, self.enqueue_log)
                screen = io.screenshot()
                # Restore the application after the screenshot has already been taken.
                self.root.deiconify()
                self.root.lift()

                original_h, original_w = screen.shape[:2]
                max_w = max(480, min(1160, self.root.winfo_screenwidth() - 80))
                max_h = max(300, min(650, self.root.winfo_screenheight() - 160))
                scale = min(1.0, max_w / original_w, max_h / original_h)
                display_w = max(1, int(original_w * scale))
                display_h = max(1, int(original_h * scale))
                rgb = cv2.cvtColor(screen, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb).resize((display_w, display_h), Image.Resampling.LANCZOS)

                crop_window = self.tk.Toplevel(self.root)
                crop_window.title(f"Crop template — {filename}")
                crop_window.geometry(f"{display_w + 32}x{display_h + 100}")
                crop_window.transient(self.root)
                outer = self.ttk.Frame(crop_window, padding=8)
                outer.pack(fill="both", expand=True)
                self.ttk.Label(outer, text="Drag a rectangle around the smallest distinctive UI element. The crop is mapped back to full-resolution pixels.").pack(anchor="w", pady=(0, 6))
                canvas = self.tk.Canvas(outer, width=display_w, height=display_h, cursor="crosshair", highlightthickness=1, highlightbackground="#888888")
                canvas.pack(fill="none", expand=False)
                photo = ImageTk.PhotoImage(image)
                canvas.create_image(0, 0, image=photo, anchor="nw")
                canvas.image = photo
                selection = {"start": None, "rect": None, "end": None}

                def on_down(event):
                    x = max(0, min(display_w, event.x))
                    y = max(0, min(display_h, event.y))
                    selection["start"] = (x, y)
                    selection["end"] = (x, y)
                    if selection["rect"] is not None:
                        canvas.delete(selection["rect"])
                    selection["rect"] = canvas.create_rectangle(x, y, x, y, outline="#ff3434", width=2)

                def on_motion(event):
                    if selection["start"] is None:
                        return
                    x = max(0, min(display_w, event.x))
                    y = max(0, min(display_h, event.y))
                    selection["end"] = (x, y)
                    x0, y0 = selection["start"]
                    canvas.coords(selection["rect"], x0, y0, x, y)

                def save_crop():
                    if not selection["start"] or not selection["end"]:
                        self.messagebox.showwarning("No crop selected", "Drag to select a crop first.", parent=crop_window)
                        return
                    x0, y0 = selection["start"]
                    x1, y1 = selection["end"]
                    left, right = sorted((int(x0 / scale), int(x1 / scale)))
                    top, bottom = sorted((int(y0 / scale), int(y1 / scale)))
                    left, top = max(0, left), max(0, top)
                    right, bottom = min(original_w, right), min(original_h, bottom)
                    if right - left < 4 or bottom - top < 4:
                        self.messagebox.showwarning("Crop too small", "Select an area at least 4×4 pixels.", parent=crop_window)
                        return
                    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                    destination = TEMPLATES_DIR / filename
                    if not cv2.imwrite(str(destination), screen[top:bottom, left:right]):
                        raise RuntimeError("OpenCV could not write the selected image.")
                    crop_window.destroy()
                    self._bind_template(category, target, filename)
                    self.enqueue_log(f"Saved {filename} ({right-left}×{bottom-top} pixels).")

                canvas.bind("<ButtonPress-1>", on_down)
                canvas.bind("<B1-Motion>", on_motion)
                canvas.bind("<ButtonRelease-1>", on_motion)
                btns = self.ttk.Frame(outer)
                btns.pack(fill="x", pady=(8, 0))
                self.ttk.Button(btns, text="Cancel", command=crop_window.destroy).pack(side="right")
                self.ttk.Button(btns, text="Save crop and link template", command=save_crop).pack(side="right", padx=(0, 8))
                crop_window.lift()
                crop_window.focus_force()
            except Exception as exc:
                self.enqueue_log(f"Template capture failed: {exc}")
                self.messagebox.showerror("Template capture failed", str(exc))
                try:
                    if crop_window is not None and crop_window.winfo_exists():
                        crop_window.destroy()
                except Exception:
                    pass
                try:
                    self.root.deiconify()
                    self.root.lift()
                except Exception:
                    pass

        self.root.after(1300, do_capture)

    def _bind_template(self, category: str, target: str, filename: str) -> None:
        cfg = self.get_config()
        if category == "State screen":
            cfg.setdefault("state_templates", {})[target] = filename
        elif category == "Action button":
            cfg.setdefault("action_templates", {})[target] = filename
        elif category == "Route node":
            cfg.setdefault("node_templates", {})[target] = filename
        elif category == "E.G.O Gift priority":
            cfg["gift_priority"] = [filename] + [f for f in cfg.get("gift_priority", []) if f != filename]
            self.gift_var.set(", ".join(cfg["gift_priority"]))
        elif category == "Shop upgrade priority":
            cfg["upgrade_priority"] = [filename] + [f for f in cfg.get("upgrade_priority", []) if f != filename]
            self.upgrade_var.set(", ".join(cfg["upgrade_priority"]))
        elif category == "Event option priority":
            cfg["event_priority"] = [filename] + [f for f in cfg.get("event_priority", []) if f != filename]
            self.event_var.set(", ".join(cfg["event_priority"]))
        elif category == "Gift blacklist":
            cfg["gift_blacklist"] = list(dict.fromkeys(cfg.get("gift_blacklist", []) + [filename]))
            self.blacklist_var.set(", ".join(cfg["gift_blacklist"]))
        self.config = cfg
        if not self.save_from_ui(quiet=True):
            self.messagebox.showwarning("Settings not fully saved", "Template image was saved, but check the settings file.")
        self.worker.matcher.clear_cache()
        self.enqueue_log(f"Linked template '{filename}' as {category}" + (f" → {target}" if target else "") + ".")

    def manage_templates(self) -> None:
        """Show a small template gallery with preview and safe removal."""
        from PIL import Image, ImageTk
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("Template library")
        win.geometry("850x560")
        win.minsize(700, 450)
        outer = ttk.Frame(win, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        tree = ttk.Treeview(outer, columns=("filename", "role", "dimensions"), show="headings", height=16)
        tree.heading("filename", text="Filename")
        tree.heading("role", text="Linked role")
        tree.heading("dimensions", text="Size")
        tree.column("filename", width=190, stretch=False)
        tree.column("role", width=240, stretch=True)
        tree.column("dimensions", width=100, stretch=False)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        preview_label = ttk.Label(outer, text="Select a template to preview it.", anchor="center")
        preview_label.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        outer.columnconfigure(2, weight=1)
        outer.rowconfigure(0, weight=1)
        cfg = self.get_config()

        def roles_for(filename):
            roles = []
            for group, mapping in (("state", cfg.get("state_templates", {})), ("button", cfg.get("action_templates", {})), ("node", cfg.get("node_templates", {}))):
                roles.extend(f"{group}: {key}" for key, value in mapping.items() if value == filename)
            for key, title in (("gift_priority", "gift"), ("upgrade_priority", "upgrade"), ("event_priority", "event"), ("gift_blacklist", "blacklist")):
                if filename in cfg.get(key, []):
                    roles.append(title)
            return ", ".join(roles) if roles else "unlinked"

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            for path in sorted(TEMPLATES_DIR.iterdir() if TEMPLATES_DIR.exists() else []):
                if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"} or not path.is_file():
                    continue
                dimensions = "unreadable"
                try:
                    with Image.open(path) as im:
                        dimensions = f"{im.width} × {im.height}"
                except Exception:
                    pass
                tree.insert("", "end", iid=path.name, values=(path.name, roles_for(path.name), dimensions))

        def preview(_event=None):
            selected = tree.selection()
            if not selected:
                return
            path = TEMPLATES_DIR / selected[0]
            try:
                image = Image.open(path).convert("RGB")
                image.thumbnail((360, 320), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                preview_label.configure(image=photo, text=f"{path.name}\n{roles_for(path.name)}", compound="top", wraplength=360)
                preview_label.image = photo
            except Exception as exc:
                preview_label.configure(image="", text=f"Could not preview {path.name}: {exc}")
                preview_label.image = None

        def delete_selected():
            if self.worker.thread and self.worker.thread.is_alive() and not self.worker.stop_event.is_set():
                self.messagebox.showwarning("Stop automation first", "Stop automation before deleting templates.", parent=win)
                return
            selected = tree.selection()
            if not selected:
                return
            filename = selected[0]
            if not self.messagebox.askyesno("Delete template?", f"Delete {filename}?\n\nConfig references will remain and will need to be recaptured or relinked.", parent=win):
                return
            try:
                (TEMPLATES_DIR / filename).unlink()
                self.worker.matcher.clear_cache()
                self.enqueue_log(f"Deleted template file: {filename}")
                preview_label.configure(image="", text="Select a template to preview it.")
                preview_label.image = None
                refresh()
            except OSError as exc:
                self.messagebox.showerror("Delete failed", str(exc), parent=win)

        tree.bind("<<TreeviewSelect>>", preview)
        buttons = ttk.Frame(outer)
        buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Delete selected…", command=delete_selected).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        refresh()

    def test_current_screen(self):
        """Analyze every configured template role and show locations; never sends input."""
        if self.worker.thread and self.worker.thread.is_alive() and not self.worker.stop_event.is_set():
            self.messagebox.showwarning(
                "Stop automation first",
                "Stop automation before analyzing the screen so the report reflects a stable screen.",
            )
            return
        try:
            from PIL import Image, ImageTk

            cfg = self.get_config()
            io = DesktopIO(lambda: cfg, self.enqueue_log)

            # Hide the manager so it does not appear in the screenshot.
            try:
                self.root.iconify()
                self.root.update_idletasks()
                time.sleep(0.5)
                screen = io.screenshot()
            finally:
                self.root.deiconify()
                self.root.lift()
                self.root.update_idletasks()

            threshold = max(0.5, min(0.99, float(cfg.get("template_threshold", 0.88))))
            matcher = TemplateMatcher(threshold, self.enqueue_log)
            screen_height, screen_width = screen.shape[:2]
            if len(screen.shape) == 2:
                annotated = cv2.cvtColor(screen, cv2.COLOR_GRAY2BGR)
            else:
                annotated = screen.copy()

            def mapping_items(value):
                """Convert a config mapping of display name -> filename to valid items."""
                if not isinstance(value, dict):
                    return []
                items = []
                for name, filename in value.items():
                    if isinstance(filename, str) and filename.strip():
                        items.append((str(name), filename.strip()))
                return items

            def priority_items(value):
                """Preserve strategy order while ignoring invalid and duplicate entries."""
                if not isinstance(value, (list, tuple)):
                    return []
                items = []
                seen = set()
                for index, filename in enumerate(value, start=1):
                    if not isinstance(filename, str) or not filename.strip():
                        continue
                    filename = filename.strip()
                    if filename in seen:
                        continue
                    seen.add(filename)
                    items.append((f"{index:02d} {Path(filename).stem}", filename))
                return items

            groups = [
                ("State screens", "state", mapping_items(cfg.get("state_templates", {}))),
                ("Action buttons", "action", mapping_items(cfg.get("action_templates", {}))),
                ("Route nodes", "node", mapping_items(cfg.get("node_templates", {}))),
                ("E.G.O Gift priority", "gift", priority_items(cfg.get("gift_priority", []))),
                ("Gift blacklist", "blacklist", priority_items(cfg.get("gift_blacklist", []))),
                ("Shop upgrade priority", "upgrade", priority_items(cfg.get("upgrade_priority", []))),
                ("Event option priority", "event", priority_items(cfg.get("event_priority", []))),
            ]

            # OpenCV uses BGR colors. Labels include role + template name so overlapping
            # matches from different categories can still be distinguished in the report.
            colors = {
                "state": (40, 205, 80),
                "action": (0, 165, 255),
                "node": (255, 145, 20),
                "gift": (200, 70, 210),
                "blacklist": (30, 30, 245),
                "upgrade": (0, 220, 255),
                "event": (255, 220, 40),
            }

            def match_location(match):
                x, y, score, width, height = match
                center = (x + width // 2, y + height // 2)
                return (
                    f"x={x}, y={y}, w={width}, h={height}, "
                    f"center=({center[0]}, {center[1]})"
                )

            def draw_match(role, name, match, index):
                x, y, score, width, height = match
                color = colors.get(role, (220, 220, 220))
                cv2.rectangle(annotated, (x, y), (x + width, y + height), color, 2)
                short_name = name if len(name) <= 24 else name[:21] + "..."
                label = f"{role}:{short_name} {score:.2f}"
                if index > 1:
                    label += f" #{index}"
                label_y = y - 7 if y >= 23 else y + height + 18
                label_y = max(14, min(screen_height - 4, label_y))
                cv2.putText(
                    annotated,
                    label,
                    (max(0, x), label_y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    color,
                    1,
                    cv2.LINE_AA,
                )

            scanned_groups = []
            configured_filenames = set()
            for title, role, items in groups:
                records = []
                for name, filename in items:
                    configured_filenames.add(Path(filename).name)
                    template_path = TEMPLATES_DIR / filename
                    template = matcher._read_template(filename)

                    if template is None:
                        status = "MISSING / unreadable"
                        if template_path.is_file():
                            status = "UNREADABLE image"
                        records.append({
                            "name": name,
                            "filename": filename,
                            "status": status,
                            "matches": [],
                            "best": None,
                        })
                        continue

                    template_height, template_width = template.shape[:2]
                    if template_height > screen_height or template_width > screen_width:
                        records.append({
                            "name": name,
                            "filename": filename,
                            "status": "LARGER THAN SCREENSHOT",
                            "matches": [],
                            "best": None,
                        })
                        continue

                    # match_all gives every threshold-passing location after non-maximum
                    # suppression. If there are none, best_unfiltered records the strongest
                    # below-threshold candidate for calibration diagnostics.
                    matches = matcher.match_all(screen, filename, threshold)
                    if matches:
                        for index, match in enumerate(matches, start=1):
                            draw_match(role, name, match, index)
                        records.append({
                            "name": name,
                            "filename": filename,
                            "status": "PASS",
                            "matches": matches,
                            "best": matches[0],
                        })
                    else:
                        candidate = matcher.best_unfiltered(screen, filename)
                        records.append({
                            "name": name,
                            "filename": filename,
                            "status": "BELOW THRESHOLD" if candidate is not None else "NO MATCH DATA",
                            "matches": [],
                            "best": candidate,
                        })
                scanned_groups.append({"title": title, "role": role, "records": records})

            state_records = scanned_groups[0]["records"] if scanned_groups else []
            passing_states = [
                (record["best"][2], record["name"])
                for record in state_records
                if record["matches"] and record["best"] is not None
            ]
            raw_states = [
                (record["best"][2], record["name"])
                for record in state_records
                if record["best"] is not None
            ]
            detected_state = max(passing_states, default=None)
            top_raw_state = max(raw_states, default=None)

            report = [
                "READ-ONLY SCREEN ANALYSIS — no mouse clicks or key presses were sent.",
                f"Screenshot: {screen_width} x {screen_height} pixels",
                f"Active match threshold: {threshold:.2f}",
            ]
            if detected_state:
                report.append(
                    f"Detected state: {detected_state[1]} "
                    f"(score {detected_state[0]:.3f}, passed threshold)"
                )
            elif top_raw_state:
                report.append(
                    f"Detected state: NONE passed threshold; strongest state candidate: "
                    f"{top_raw_state[1]} ({top_raw_state[0]:.3f})"
                )
            else:
                report.append("Detected state: NONE (no state template could be evaluated)")
            report.extend([
                "",
                "SUMMARY — template counts refer to configured files; detections are screen locations.",
            ])
            for group in scanned_groups:
                records = group["records"]
                matched_templates = sum(1 for record in records if record["matches"])
                detection_count = sum(len(record["matches"]) for record in records)
                report.append(
                    f"  {group['title']}: {matched_templates}/{len(records)} templates matched; "
                    f"{detection_count} location(s)"
                )

            report.append("")
            report.append("DETAILS — coordinates and box sizes are in screenshot pixels.")
            for group in scanned_groups:
                records = group["records"]
                report.append("")
                report.append(f"[{group['title']}]")
                if not records:
                    report.append("  No templates configured for this category.")
                    continue
                for record in records:
                    name = record["name"]
                    filename = record["filename"]
                    matches = record["matches"]
                    best = record["best"]
                    if matches:
                        report.append(
                            f"  PASS  {name} ({filename}) — {len(matches)} location(s)"
                        )
                        for index, match in enumerate(matches, start=1):
                            report.append(
                                f"    #{index} score={match[2]:.3f}; "
                                f"{match_location(match)}"
                            )
                    elif best is not None:
                        report.append(
                            f"  BELOW {name} ({filename}) — best score={best[2]:.3f}; "
                            f"{match_location(best)}"
                        )
                    else:
                        report.append(
                            f"  {record['status']}: {name} ({filename})"
                        )

            # A saved PNG/JPEG is not active merely because it exists on disk. It needs
            # to be linked from one of the categories above before the automation can use it.
            image_suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
            try:
                library_files = sorted(
                    path.name
                    for path in TEMPLATES_DIR.iterdir()
                    if path.is_file() and path.suffix.lower() in image_suffixes
                )
            except OSError:
                library_files = []
            unlinked_files = [
                filename for filename in library_files
                if filename not in configured_filenames
            ]
            report.append("")
            report.append("[Saved image files not linked to a configured role]")
            if unlinked_files:
                report.append(
                    "  These files were not scanned and will not guide automation until linked."
                )
                report.extend(f"  UNLINKED: {filename}" for filename in unlinked_files)
            else:
                report.append("  None found (or the template library is empty).")

            report.extend([
                "",
                "Color legend: state=green, action=orange, node=blue, gift=purple, "
                "blacklist=red, upgrade=yellow, event option=cyan.",
                "Priority templates are listed in the order currently saved in your config.",
            ])
            self.enqueue_log(
                f"Screen analysis: {detected_state[1] if detected_state else 'no state passed threshold'}; "
                f"threshold {threshold:.2f}; "
                f"{sum(len(record['matches']) for group in scanned_groups for record in group['records'])} "
                "template location(s); no input sent."
            )

            win = self.tk.Toplevel(self.root)
            win.title("Current screen analysis — read-only")
            win.geometry("1280x920")
            outer = self.ttk.Frame(win, padding=10)
            outer.pack(fill="both", expand=True)
            self.ttk.Label(
                outer,
                text="Template analysis report",
                font=("TkDefaultFont", 12, "bold"),
            ).pack(anchor="w")

            report_frame = self.ttk.Frame(outer)
            report_frame.pack(fill="x", pady=(5, 8))
            report_frame.columnconfigure(0, weight=1)
            report_frame.rowconfigure(0, weight=1)
            text_box = self.tk.Text(
                report_frame,
                height=15,
                wrap="none",
                font=("TkFixedFont", 9),
            )
            vertical_scroll = self.ttk.Scrollbar(
                report_frame, orient="vertical", command=text_box.yview
            )
            horizontal_scroll = self.ttk.Scrollbar(
                report_frame, orient="horizontal", command=text_box.xview
            )
            text_box.configure(
                yscrollcommand=vertical_scroll.set,
                xscrollcommand=horizontal_scroll.set,
            )
            text_box.grid(row=0, column=0, sticky="nsew")
            vertical_scroll.grid(row=0, column=1, sticky="ns")
            horizontal_scroll.grid(row=1, column=0, sticky="ew")
            text_box.insert("1.0", "\n".join(report))
            text_box.configure(state="disabled")

            sh, sw = annotated.shape[:2]
            scale = min(1.0, 1240 / sw, 570 / sh)
            view = cv2.resize(
                annotated,
                (max(1, int(sw * scale)), max(1, int(sh * scale))),
                interpolation=cv2.INTER_AREA,
            )
            pil = Image.fromarray(cv2.cvtColor(view, cv2.COLOR_BGR2RGB))
            image_label = self.ttk.Label(outer, image=None, anchor="center")
            photo = ImageTk.PhotoImage(pil)
            image_label.configure(image=photo)
            image_label.image = photo
            image_label.pack(fill="both", expand=True)
            self.ttk.Button(outer, text="Close", command=win.destroy).pack(
                anchor="e", pady=(6, 0)
            )
        except Exception as exc:
            self.enqueue_log(f"Screen analysis failed: {exc}")
            self.messagebox.showerror("Screen analysis failed", str(exc))

    def check_templates(self):
        config = self.get_config()
        groups = {
            "State screens": list(config.get("state_templates", {}).values()),
            "Action buttons": list(config.get("action_templates", {}).values()),
            "Route nodes": list(config.get("node_templates", {}).values()),
            "Gift priority": list(config.get("gift_priority", [])),
            "Shop upgrades": list(config.get("upgrade_priority", [])),
            "Event options": list(config.get("event_priority", [])),
            "Gift blacklist": list(config.get("gift_blacklist", [])),
        }
        lines = [f"Template folder: {TEMPLATES_DIR}", ""]
        missing_all = []
        for title, names in groups.items():
            names = [name for name in names if name]
            found = [name for name in names if (TEMPLATES_DIR / name).is_file()]
            missing = [name for name in names if not (TEMPLATES_DIR / name).is_file()]
            lines.append(f"{title}: {len(found)}/{len(names)} found")
            missing_all.extend((title, name) for name in missing)
        if missing_all:
            lines.append("\nMissing files:")
            lines.extend(f"  [{group}] {name}" for group, name in missing_all[:35])
            if len(missing_all) > 35:
                lines.append(f"  … and {len(missing_all) - 35} more")
        else:
            lines.append("\nAll configured template references have files.")
        lines.append("\nThis checks files only; use Analyze current screen to verify matching confidence and location.")
        self.messagebox.showinfo("Template check", "\n".join(lines))

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
        try:
            self.hotkey_listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
        except Exception as exc:
            self.enqueue_log(f"Could not start global hotkeys: {exc}. GUI controls still work.")

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


def run_environment_doctor() -> int:
    """Print non-invasive dependency and desktop-backend diagnostics."""
    print("Limbus Micro Manager — environment check")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Platform: {platform.platform()}")
    print(f"Desktop: {'Wayland' if is_wayland() else 'X11/Windows/macOS or unknown'}")
    print(f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '') or '(unset)'}")
    print(f"DISPLAY={os.environ.get('DISPLAY', '') or '(unset)'}")
    print(f"OpenCV: {'available' if cv2 is not None else 'MISSING'}")
    print(f"NumPy: {'available' if np is not None else 'MISSING'}")
    print(f"PyAutoGUI: {'available' if pyautogui is not None else 'unavailable'}")
    if _PYAUTOGUI_IMPORT_ERROR:
        print(f"  PyAutoGUI import detail: {_PYAUTOGUI_IMPORT_ERROR}")
    try:
        import tkinter
        print(f"Tkinter: available (Tk {tkinter.TkVersion})")
    except Exception as exc:
        print(f"Tkinter: MISSING ({exc})")
    try:
        import PIL
        print(f"Pillow: available ({PIL.__version__})")
    except Exception as exc:
        print(f"Pillow: MISSING ({exc})")
    print(f"pynput global shortcut: {'available' if pynput_keyboard is not None else 'unavailable'}")
    for executable in ("spectacle", "gnome-screenshot", "grim", "ydotool", "xdg-open"):
        print(f"{executable}: {shutil.which(executable) or 'not found'}")
    print(f"Config: {CONFIG_PATH}")
    print(f"Templates: {TEMPLATES_DIR}")
    if sys.platform.startswith("linux"):
        print("Linux Tk fix: Debian/Ubuntu: sudo apt-get install python3-tk python3-dev")
        print("              Fedora: sudo dnf install python3-tkinter python3-devel")
    if is_wayland():
        print("Wayland note: capture needs a supported screenshot utility; input needs a configured ydotool service/daemon.")
        print("Global shortcut note: create a desktop shortcut invoking this script with --toggle or --stop.")
    return 0 if cv2 is not None and np is not None else 1


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="Limbus Micro Manager")
    parser.add_argument("--toggle", action="store_true", help="toggle Start/Stop in an already running app")
    parser.add_argument("--start", action="store_true", help="start automation in an already running app")
    parser.add_argument("--stop", action="store_true", help="stop automation in an already running app")
    parser.add_argument("--doctor", action="store_true", help="check Python dependencies and desktop input/capture backends")
    args = parser.parse_args()
    setup_logging()
    if args.doctor:
        return run_environment_doctor()
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
