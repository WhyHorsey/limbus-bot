import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import micro_manager as mm


class SignatureTests(unittest.TestCase):
    def test_identical_screens_have_zero_delta(self):
        screen = np.zeros((120, 160, 3), dtype=np.uint8)
        sig = mm.screen_signature(screen)
        self.assertEqual(mm.signature_delta(sig, sig.copy()), 0.0)

    def test_different_screens_have_positive_delta(self):
        first = np.zeros((120, 160, 3), dtype=np.uint8)
        second = np.full((120, 160, 3), 255, dtype=np.uint8)
        self.assertGreater(mm.signature_delta(mm.screen_signature(first), mm.screen_signature(second)), 0.9)


class RouteSelectionTests(unittest.TestCase):
    def setUp(self):
        # x, y, confidence, width, height
        self.matches = [
            (400, 200, 0.97, 30, 30),
            (100, 230, 0.91, 30, 30),
            (800, 240, 0.90, 30, 30),
        ]

    def test_default_picks_highest_confidence(self):
        self.assertEqual(mm.choose_route_match(self.matches, "priority_then_confidence", 1000, 600), self.matches[0])

    def test_leftmost_picks_leftmost_node(self):
        self.assertEqual(mm.choose_route_match(self.matches, "priority_then_leftmost", 1000, 600), self.matches[1])

    def test_rightmost_picks_rightmost_node(self):
        self.assertEqual(mm.choose_route_match(self.matches, "priority_then_rightmost", 1000, 600), self.matches[2])

    def test_center_picks_node_nearest_center(self):
        matches = [(100, 100, .99, 20, 20), (490, 290, .90, 20, 20), (900, 100, .95, 20, 20)]
        self.assertEqual(mm.choose_route_match(matches, "priority_then_center", 1000, 600), matches[1])

    def test_empty_matches_return_none(self):
        self.assertIsNone(mm.choose_route_match([], "priority_then_center", 1000, 600))


class TemplateMatcherTests(unittest.TestCase):
    def test_finds_exact_template_crop(self):
        rng = np.random.default_rng(7)
        template = rng.integers(0, 255, (16, 18), dtype=np.uint8)
        screen = rng.integers(0, 80, (100, 120), dtype=np.uint8)
        expected_x, expected_y = 47, 32
        screen[expected_y:expected_y + template.shape[0], expected_x:expected_x + template.shape[1]] = template
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "example.png"
            self.assertTrue(cv2.imwrite(str(path), template))
            with patch.object(mm, "TEMPLATES_DIR", Path(tmp)):
                matcher = mm.TemplateMatcher(0.95, lambda _message: None)
                match = matcher.best(screen, "example.png")
                self.assertIsNotNone(match)
                self.assertEqual(match[0:2], (expected_x, expected_y))
                self.assertGreaterEqual(match[2], 0.95)


class ConfigMigrationTests(unittest.TestCase):
    def test_old_profile_gains_new_default_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"mode":"Mirror Dungeon","route_priority":["shop"]}', encoding="utf-8")
            with patch.object(mm, "CONFIG_PATH", path):
                cfg = mm.load_config()
            self.assertEqual(cfg["route_priority"], ["shop"])
            self.assertIn("mode_select", cfg["state_templates"])
            self.assertIn("confirm_upgrade", cfg["action_templates"])
            self.assertIn("screen_change_threshold", cfg)
            self.assertFalse(cfg["clicks_enabled"])
            self.assertEqual(cfg["config_version"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
