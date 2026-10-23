import unittest
from unittest import mock

from mopa_luiz.cli import MarkConfig
from mopa_luiz.layers import (
    LayerSpec,
    default_layers,
    emitting_jobs,
    framing_jobs,
    group_by_layer,
    layer_from_dict,
    validate_layers,
)


def fake_cfg() -> MarkConfig:
    return MarkConfig(path=mock.MagicMock(),
                      values={"MINPWMFREQ": "1000", "MAXPWMFREQ": "4000000"})


class DefaultLayersTests(unittest.TestCase):
    def test_default_layers_pass_validation(self):
        errors = validate_layers(default_layers(), fake_cfg())
        self.assertEqual(errors, {})

    def test_raster_layer_with_invalid_pitch_is_flagged(self):
        layers = default_layers()
        for layer in layers:
            if layer.operation == "raster_engrave":
                layer.output = True
                layer.raster_pitch_mm = 0
        errors = validate_layers(layers, fake_cfg())
        self.assertIn("raster", errors)
        self.assertTrue(any("pitch" in e for e in errors["raster"]))

    def test_layer_above_100_percent_flagged(self):
        layers = default_layers()
        for layer in layers:
            if layer.operation == "vector_cut":
                layer.power_percent = 101
        errors = validate_layers(layers, fake_cfg())
        self.assertIn("vector-cut", errors)


class GroupingTests(unittest.TestCase):
    def test_group_by_layer_keeps_order(self):
        layers = default_layers()
        objects = [
            {"layer_id": "vector-cut", "polylines": [[(0, 0), (1, 1)]]},
            {"layer_id": "vector-engrave", "polylines": [[(0, 0), (2, 2)]]},
            {"layer_id": "vector-engrave", "polylines": [[(0, 0), (3, 3)]]},
        ]
        jobs = group_by_layer(objects, layers)
        ids = [job.layer.layer_id for job in jobs]
        # Order must match layer-list order, not object insertion order.
        self.assertEqual(ids, ["vector-engrave", "vector-cut", "raster", "frame-only"])
        eng = next(j for j in jobs if j.layer.layer_id == "vector-engrave")
        self.assertEqual(len(eng.polylines), 2)

    def test_emitting_jobs_filters_disabled_and_frame_only(self):
        layers = default_layers()
        objects = [
            {"layer_id": "vector-engrave", "polylines": [[(0, 0), (1, 1)]]},
            {"layer_id": "raster",         "polylines": [[(0, 0), (2, 2)]]},
            {"layer_id": "frame-only",     "polylines": [[(0, 0), (3, 3)]]},
        ]
        jobs = group_by_layer(objects, layers)
        emitting = emitting_jobs(jobs)
        # Raster now emits (its hatch lines are generated at mark time).
        self.assertEqual(
            sorted(j.layer.layer_id for j in emitting),
            ["raster", "vector-engrave"],
        )
        framing = framing_jobs(jobs)
        # Frame-only and raster (visible) should still be available for framing,
        # but disabled is excluded — and there is no disabled object here.
        self.assertGreaterEqual(len(framing), 1)

    def test_object_on_an_unknown_layer_is_refused_not_dropped(self):
        objects = [
            {"layer_id": "vector-engrave", "polylines": [[(0, 0), (1, 1)]]},
            {"name": "Logo", "layer_id": "deleted-layer", "polylines": [[(0, 0), (2, 2)]]},
        ]
        with self.assertRaisesRegex(ValueError, "Logo is on layer 'deleted-layer', which does not exist"):
            group_by_layer(objects, default_layers())

    def test_object_without_a_layer_is_refused(self):
        with self.assertRaisesRegex(ValueError, "which does not exist"):
            group_by_layer([{"polylines": [[(0, 0), (1, 1)]]}], default_layers())

    def test_three_coordinate_point_is_refused_not_a_crash(self):
        objects = [{"name": "Mesh", "layer_id": "vector-engrave", "polylines": [[(0, 0, 0), (1, 1, 0)]]}]
        with self.assertRaisesRegex(ValueError, "Mesh has a point with 3 coordinate"):
            group_by_layer(objects, default_layers())

    def test_one_coordinate_or_scalar_point_is_refused(self):
        for bad in ([(0,), (1, 1)], [0, (1, 1)]):
            with self.assertRaisesRegex(ValueError, "only 2-D"):
                group_by_layer([{"layer_id": "vector-engrave", "polylines": [bad]}], default_layers())

    def test_layer_from_dict_round_trip(self):
        spec = LayerSpec(
            layer_id="custom", name="Custom", operation="vector_engrave",
            power_percent=2.0, frequency_khz=40, pulse_width_ns=200,
            speed_mm_s=500, passes=2,
        )
        clone = layer_from_dict(spec.to_dict())
        self.assertEqual(clone.layer_id, "custom")
        self.assertEqual(clone.passes, 2)
        self.assertEqual(clone.operation, "vector_engrave")


if __name__ == "__main__":
    unittest.main()
