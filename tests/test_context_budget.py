import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from steam_visualogue.context_budget import (  # noqa: E402
    AgentPacketItemTooLarge,
    MAX_IMAGES_PER_PACKET,
    PACKET_MAX_ESTIMATED_TOKENS,
    PACKET_MAX_UTF8_BYTES,
    MAX_SOURCE_PIXELS_PER_PACKET,
    RESULT_MAX_UTF8_BYTES,
    assert_merge_budget,
    assert_packet_budget,
    assert_result_budget,
    canonical_json,
    measure_serialized,
)


class ContextBudgetTests(unittest.TestCase):
    def test_canonical_json_is_stable_and_utf8_aware(self) -> None:
        value = {"中文": "档案", "z": [2, 1]}
        self.assertEqual(canonical_json(value), '{"z":[2,1],"中文":"档案"}\n')
        first = measure_serialized(canonical_json(value))
        second = measure_serialized(canonical_json(value))
        self.assertEqual(first, second)
        self.assertGreater(first.utf8_bytes, first.character_count)

    def test_token_boundary_is_checked_conservatively(self) -> None:
        self.assertTrue(measure_serialized("中" * PACKET_MAX_ESTIMATED_TOKENS).safe_to_dispatch)
        self.assertEqual(
            PACKET_MAX_ESTIMATED_TOKENS,
            measure_serialized("中" * PACKET_MAX_ESTIMATED_TOKENS).estimated_tokens,
        )
        self.assertIn(
            "packet_tokens",
            measure_serialized("中" * (PACKET_MAX_ESTIMATED_TOKENS + 1)).failures,
        )

    def test_packet_result_and_merge_limits_are_distinct(self) -> None:
        self.assertIn("packet_bytes", measure_serialized(b"x" * (PACKET_MAX_UTF8_BYTES + 1)).failures)
        with self.assertRaisesRegex(ValueError, "result_bytes"):
            assert_result_budget({"value": "x" * (RESULT_MAX_UTF8_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "merge_bytes"):
            assert_merge_budget({"value": "x" * 30_000})

    def test_image_and_pixel_limits_are_hard_gates(self) -> None:
        with self.assertRaisesRegex(ValueError, "packet_images"):
            assert_packet_budget({}, image_count=MAX_IMAGES_PER_PACKET + 1)
        with self.assertRaisesRegex(ValueError, "packet_pixels"):
            assert_packet_budget({}, total_pixels=MAX_SOURCE_PIXELS_PER_PACKET + 1)

    def test_oversized_item_error_does_not_leak_content(self) -> None:
        with self.assertRaises(AgentPacketItemTooLarge) as context:
            assert_packet_budget({"value": "secret" * 20_000}, item_id="card:1")
        self.assertEqual(context.exception.item_id, "card:1")
        self.assertNotIn("secret", str(context.exception))


if __name__ == "__main__":
    unittest.main()
