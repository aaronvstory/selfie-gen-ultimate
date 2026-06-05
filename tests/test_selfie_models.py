import types
import unittest

from kling_gui.tabs.selfie_tab import SelfieTab


class SelfieModelParsingTests(unittest.TestCase):
    def test_kontext_pro_is_available(self):
        from selfie_generator import SelfieGenerator
        models = SelfieGenerator.get_available_models()
        endpoints = {m["endpoint"] for m in models}
        self.assertIn("fal-ai/flux-pro/kontext", endpoints)
        kontext = next(m for m in models if m["endpoint"] == "fal-ai/flux-pro/kontext")
        self.assertEqual(kontext["label"], "Kontext Pro")
        self.assertEqual(kontext["provider"], "fal")

    def test_derive_slug(self):
        # Last TWO path segments, to avoid collisions on a shared final segment.
        self.assertEqual(SelfieTab._derive_slug("fal-ai/flux-pro/kontext"), "flux-pro-kontext")
        self.assertEqual(SelfieTab._derive_slug("vendor/Some_Model.v2"), "vendor-some-model-v2")
        self.assertEqual(SelfieTab._derive_slug(""), "model")

    def test_derive_slug_avoids_collision(self):
        # Different vendors, same final segment → distinct slugs.
        self.assertNotEqual(
            SelfieTab._derive_slug("vendor/model"),
            SelfieTab._derive_slug("vendor2/model"),
        )

    def test_parse_rejects_query_string_endpoints(self):
        out = SelfieTab.parse_model_lines("vendor/model?key=val\nvendor/ok\nvendor/frag#x")
        self.assertEqual([m["endpoint"] for m in out], ["vendor/ok"])

    def test_prettify_label(self):
        self.assertEqual(SelfieTab._prettify_label("fal-ai/nano-banana-2/edit"), "Edit")
        self.assertEqual(SelfieTab._prettify_label("vendor/cool_model"), "Cool Model")

    def test_parse_model_lines_basic(self):
        out = SelfieTab.parse_model_lines(
            "fal-ai/flux-pro/kontext | Kontext Pro\n"
            "vendor/plain-model\n"
            "   \n"            # blank → skipped
            "no-slash-here\n"  # invalid → skipped
            "/leading-slash\n" # invalid → skipped
        )
        endpoints = [m["endpoint"] for m in out]
        self.assertEqual(endpoints, ["fal-ai/flux-pro/kontext", "vendor/plain-model"])
        self.assertEqual(out[0]["label"], "Kontext Pro")
        self.assertEqual(out[0]["slug"], "flux-pro-kontext")
        self.assertEqual(out[0]["provider"], "fal")
        self.assertEqual(out[0]["api_url"], "https://fal.ai/models/fal-ai/flux-pro/kontext/api")
        # Derived label when none supplied.
        self.assertEqual(out[1]["label"], "Plain Model")

    def test_parse_model_lines_dedup(self):
        out = SelfieTab.parse_model_lines(
            "vendor/x\nvendor/x | Dupe\nvendor/y\n"
        )
        self.assertEqual([m["endpoint"] for m in out], ["vendor/x", "vendor/y"])

    def test_load_custom_models_validates(self):
        # Bind the unbound method to a stub carrying just `config`.
        stub = types.SimpleNamespace(config={
            "selfie_custom_models": [
                {"endpoint": "vendor/good", "label": "Good"},
                {"endpoint": "", "label": "empty-skip"},
                {"endpoint": "noslash", "label": "invalid-skip"},
                {"endpoint": "vendor/good", "label": "dupe-skip"},
                "not-a-dict",
            ]
        })
        loaded = SelfieTab._load_custom_models(stub)
        self.assertEqual([m["endpoint"] for m in loaded], ["vendor/good"])
        self.assertEqual(loaded[0]["provider"], "fal")
        self.assertEqual(loaded[0]["slug"], "vendor-good")

    def test_load_custom_models_handles_bad_config(self):
        stub = types.SimpleNamespace(config={"selfie_custom_models": "not-a-list"})
        self.assertEqual(SelfieTab._load_custom_models(stub), [])
        stub2 = types.SimpleNamespace(config={})
        self.assertEqual(SelfieTab._load_custom_models(stub2), [])

    def test_merge_custom_models_skips_builtin_dupes(self):
        stub = types.SimpleNamespace(
            _model_options=[{"endpoint": "vendor/builtin", "label": "Builtin"}],
            _custom_models=[
                {"endpoint": "vendor/builtin", "label": "dupe"},
                {"endpoint": "vendor/new", "label": "New"},
            ],
        )
        SelfieTab._merge_custom_models(stub)
        self.assertEqual(
            [m["endpoint"] for m in stub._model_options],
            ["vendor/builtin", "vendor/new"],
        )


if __name__ == "__main__":
    unittest.main()
