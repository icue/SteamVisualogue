"""Tests for the automatic ribbon composer and multi-game ribbon pipeline."""

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from PIL import Image

from steam_visualogue.ribbon_composer import (
    select_ribbon_game_ids,
    prepare_ribbon_card,
    compose_dual_row_ribbon,
)
from steam_visualogue.assets import materialize_selected_assets


class TestRibbonComposer(unittest.TestCase):
    def test_select_ribbon_game_ids_ranking_and_deduplication(self):
        # Create a sample profile with 30 games with various playtimes
        profile = {
            "games": [
                {"appid": i, "name": f"Game {i}", "playtime_forever": i * 100}
                for i in range(1, 31)
            ]
        }
        opening, closing = select_ribbon_game_ids(profile, count_per_ribbon=12)
        
        # 1. Opening should have top 12 (AppIDs 30 down to 19)
        self.assertEqual(len(opening), 12)
        self.assertEqual(opening, list(range(30, 18, -1)))
        
        # 2. Closing should have next 12 (AppIDs 18 down to 7)
        self.assertEqual(len(closing), 12)
        self.assertEqual(closing, list(range(18, 6, -1)))
        
        # 3. Disjoint check: strictly no overlap between opening and closing
        self.assertEqual(len(set(opening).intersection(set(closing))), 0)
        self.assertEqual(len(set(opening)), 12)
        self.assertEqual(len(set(closing)), 12)

    def test_select_ribbon_game_ids_small_library(self):
        profile = {
            "games": [
                {"appid": 10, "name": "Game 10", "playtime_forever": 500},
                {"appid": 20, "name": "Game 20", "playtime_forever": 300},
                {"appid": 30, "name": "Game 30", "playtime_forever": 100},
            ]
        }
        opening, closing = select_ribbon_game_ids(profile, count_per_ribbon=12)
        self.assertEqual(opening, [10, 20, 30])
        self.assertEqual(closing, [10, 20, 30])

    def test_prepare_ribbon_card(self):
        test_img = Image.new("RGB", (300, 450), (255, 0, 0))
        card = prepare_ribbon_card(test_img, size=(280, 420), corner_radius=18)
        self.assertEqual(card.size, (280, 420))
        self.assertEqual(card.mode, "RGBA")
        # Corners should be transparent
        corner_alpha = card.getpixel((0, 0))[3]
        self.assertEqual(corner_alpha, 0)
        # Center should be opaque
        center_alpha = card.getpixel((140, 210))[3]
        self.assertGreater(center_alpha, 200)

    def test_compose_dual_row_ribbon(self):
        cards = [
            Image.new("RGB", (300, 450), ((i * 40) % 255, (i * 70) % 255, (i * 90) % 255))
            for i in range(12)
        ]
        target_size = (1872, 1440)
        ribbon = compose_dual_row_ribbon(cards, angle=-5.5, target_size=target_size)
        self.assertEqual(ribbon.size, target_size)
        self.assertEqual(ribbon.mode, "RGBA")
        
        # Check edge feathering (leftmost and rightmost columns should be faded)
        left_edge_alpha = ribbon.getpixel((0, 720))[3]
        self.assertEqual(left_edge_alpha, 0)
        right_edge_alpha = ribbon.getpixel((1871, 720))[3]
        self.assertEqual(right_edge_alpha, 0)
        
        # Center should have non-zero alpha
        center_alpha = ribbon.getpixel((936, 720))[3]
        self.assertGreater(center_alpha, 0)

    def test_materialize_selected_assets_auto_ribbon(self):
        with TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir) / "assets"
            
            # Create a mock image payload
            buf = io.BytesIO()
            Image.new("RGB", (600, 900), (70, 130, 180)).save(buf, format="JPEG")
            img_payload = buf.getvalue()
            
            profile = {
                "games": [
                    {
                        "appid": i,
                        "name": f"Game {i}",
                        "playtime_forever": i * 100,
                        "artwork_url": f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{i}/header.jpg",
                    }
                    for i in range(1, 25)
                ]
            }
            deck_plan = {
                "format": "steam-visualogue-deck-plan",
                "pages": [
                    {"page": 1, "presentation": {"kind": "opening", "content": {}}},
                    {"page": 2, "presentation": {"kind": "hero", "content": {"subject": {"game_id": "game:24", "asset_id": "game:24:portrait"}}}},
                    {"page": 15, "presentation": {"kind": "closing", "content": {}}},
                ]
            }
            
            class MockResponse:
                def __init__(self, payload):
                    self.payload = payload
                    self.headers = {"Content-Type": "image/jpeg"}
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def read(self, *args):
                    return self.payload
            
            def mock_opener(request, timeout=30):
                return MockResponse(img_payload)
            
            manifest = materialize_selected_assets(
                profile,
                deck_plan,
                assets_dir,
                opener=mock_opener,
                sleeper=lambda _: None,
            )
            
            # Verify that opening_ribbon_asset_id and closing_ribbon_asset_id were generated and registered
            self.assertIn("opening_ribbon_asset_id", manifest)
            self.assertIn("closing_ribbon_asset_id", manifest)
            opening_id = manifest["opening_ribbon_asset_id"]
            closing_id = manifest["closing_ribbon_asset_id"]
            
            self.assertIn(opening_id, manifest["assets"])
            self.assertIn(closing_id, manifest["assets"])
            self.assertEqual(manifest["assets"][opening_id]["ribbon_role"], "opening")
            self.assertEqual(manifest["assets"][closing_id]["ribbon_role"], "closing")
            self.assertEqual(manifest["assets"][opening_id]["status"], "ready")
            self.assertEqual(manifest["assets"][closing_id]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
