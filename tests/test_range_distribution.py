import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.preview.range_distribution import generate_range_value


class TestRangeDistribution(unittest.TestCase):
    def test_custom_segments_can_return_fixed_value(self):
        segment = {"weight": "100", "mode": "fixed", "value": "1000"}
        with patch("services.preview.range_distribution.random.choices", return_value=[segment]):
            value = generate_range_value(
                var_type="integer",
                vmin=1,
                vmax=100,
                params={"distribution": "custom_segments", "segments": [segment]},
            )
        self.assertEqual(value, 1000)

    def test_custom_segments_range_uses_segment_bounds(self):
        segment = {"weight": "100", "mode": "range", "min": "10", "max": "20"}
        with patch("services.preview.range_distribution.random.choices", return_value=[segment]):
            with patch("services.preview.range_distribution.random.randint", return_value=17) as mocked_randint:
                value = generate_range_value(
                    var_type="integer",
                    vmin=1,
                    vmax=100,
                    params={"distribution": "custom_segments", "segments": [segment]},
                )
        self.assertEqual(value, 17)
        mocked_randint.assert_called_once_with(10, 20)

    def test_custom_segments_edge_uses_default_boundary_candidates(self):
        segment = {"weight": "100", "mode": "edge"}
        captured = {}

        def _pick(candidates):
            captured["candidates"] = list(candidates)
            return candidates[-1]

        with patch("services.preview.range_distribution.random.choices", return_value=[segment]):
            with patch("services.preview.range_distribution.random.choice", side_effect=_pick):
                value = generate_range_value(
                    var_type="integer",
                    vmin=1,
                    vmax=10,
                    params={"distribution": "custom_segments", "segments": [segment]},
                )
        self.assertEqual(value, 9)
        self.assertEqual(captured["candidates"], [1, 10, 2, 9])

    def test_custom_segments_invalid_weights_fall_back_to_uniform(self):
        segments = [
            {"weight": "0", "mode": "fixed", "value": "1000"},
            {"weight": "bad", "mode": "fixed", "value": "2000"},
        ]
        with patch("services.preview.range_distribution.random.randint", return_value=42) as mocked_randint:
            value = generate_range_value(
                var_type="integer",
                vmin=1,
                vmax=100,
                params={"distribution": "custom_segments", "segments": segments},
            )
        self.assertEqual(value, 42)
        mocked_randint.assert_called_once_with(1, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
