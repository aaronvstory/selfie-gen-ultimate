import types
import unittest

from kling_gui.tabs.selfie_tab import SelfieTab


class SelfieModelParsingTests(unittest.TestCase):
    def test_build_payload_supports_kontext_and_custom(self):
        # Codex P1: exposing Kontext / custom endpoints must not hard-fail in
        # _build_payload. Kontext gets a flux-kontext payload; an arbitrary
        # custom endpoint gets a generic fal edit payload (no ValueError).
        from selfie_generator import SelfieGenerator
        kontext = SelfieGenerator._build_payload(
            "fal-ai/flux-pro/kontext", "p", "http://x/y.png", 1.0, 1024, 1024, 7)
        self.assertEqual(kontext["image_url"], "http://x/y.png")
        self.assertIn("guidance_scale", kontext)
        custom = SelfieGenerator._build_payload(
            "some-vendor/some-model", "p", "http://x/y.png", 1.0, 1024, 1024, 7)
        self.assertEqual(custom["image_urls"], ["http://x/y.png"])
        # Built-ins still behave.
        gpt = SelfieGenerator._build_payload(
            "openai/gpt-image-2/edit", "p", "http://x/y.png", 1.0, 1024, 1024, 7)
        self.assertEqual(gpt["image_urls"], ["http://x/y.png"])

    def test_model_short_name_collision_resistant_for_custom(self):
        # Output filenames for custom (unregistered) endpoints must not collide
        # when they share a final segment (code-review MEDIUM round 2, PR #77).
        from selfie_generator import SelfieGenerator
        a = SelfieGenerator._model_short_name("vendor-a/edit")
        b = SelfieGenerator._model_short_name("vendor-b/edit")
        self.assertNotEqual(a, b)
        # Built-ins still resolve via their models.json slug.
        self.assertEqual(
            SelfieGenerator._model_short_name("fal-ai/flux-pro/kontext"), "kontext-pro")

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

    def test_prettify_label_intelligent_derivation(self):
        """v2.25: label derivation must use the MODEL name, not the trailing
        action suffix (`/edit`, `/text-to-image`, …). Drop the fal-ai/ vendor
        prefix too — the app is fal.ai-focused and `fal-ai/` adds no signal.
        Other vendors are KEPT because they disambiguate (OpenAI vs Anthropic
        vs fal hosted-by-default). Brand fix-ups for known names (PuLID, GPT,
        OpenAI) — title-case alone gets these wrong.
        """
        # Trailing action suffix dropped; fal-ai/ prefix dropped.
        self.assertEqual(
            SelfieTab._prettify_label("fal-ai/nano-banana-2/edit"),
            "Nano Banana 2",
        )
        # No action suffix; full path preserved minus fal-ai/.
        self.assertEqual(
            SelfieTab._prettify_label("fal-ai/flux-pro/kontext"),
            "Flux Pro Kontext",
        )
        # Non-fal-ai vendor preserved (disambiguates from fal-hosted models)
        # + brand fix-ups (OpenAI, GPT).
        self.assertEqual(
            SelfieTab._prettify_label("openai/gpt-image-2/edit"),
            "OpenAI GPT Image 2",
        )
        # Brand fix-up: PuLID stays PuLID, not Pulid.
        self.assertEqual(
            SelfieTab._prettify_label("fal-ai/flux-pulid/text-to-image"),
            "Flux PuLID",
        )
        # Unknown-vendor + meaningful tail: vendor preserved (it disambiguates).
        self.assertEqual(
            SelfieTab._prettify_label("vendor/cool_model"),
            "Vendor Cool Model",
        )
        # Version segments stay lowercase v: kling-video/v3/pro → Kling Video v3 Pro.
        self.assertEqual(
            SelfieTab._prettify_label("fal-ai/kling-video/v3/pro/image-to-video"),
            "Kling Video v3 Pro",
        )
        # Empty / malformed fall back to a safe placeholder.
        self.assertEqual(SelfieTab._prettify_label(""), "Model")

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
        # Derived label when none supplied — v2.25 intelligent derivation
        # keeps the unknown vendor prefix to disambiguate (it would otherwise
        # collide with any other unrelated `*/plain-model`).
        self.assertEqual(out[1]["label"], "Vendor Plain Model")

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


class EditModelsDialogTextFormatTests(unittest.TestCase):
    """v2.25: the Add-Models modal became the Edit-Models modal — it pre-fills
    with the user's existing custom models so they can edit labels, fix typos,
    or remove entries. These tests pin the format roundtrip without spinning
    up Tk (which test_selfie_models.py historically avoids).
    """

    def test_format_custom_models_for_editing_roundtrip(self):
        """``SelfieTab._format_models_for_editing`` produces ``endpoint | label``
        lines, and ``parse_model_lines`` reads them back to equivalent dicts
        (endpoint + label preserved; provider / slug / api_url re-derived
        deterministically from the endpoint). So the user can open the modal,
        save without changes, and get the exact same list back.
        """
        models = [
            {"endpoint": "fal-ai/flux-pro/kontext", "label": "Kontext Pro"},
            {"endpoint": "openai/gpt-image-2/edit",  "label": "OpenAI GPT Image 2"},
            {"endpoint": "vendor/no-label",          "label": ""},
        ]
        text = SelfieTab._format_models_for_editing(models)
        # Lines come out in the same order with `endpoint | label` format,
        # one model per line. Empty-label entries appear as bare endpoints
        # (so the next save re-derives the label intelligently).
        self.assertEqual(
            text.splitlines(),
            [
                "fal-ai/flux-pro/kontext | Kontext Pro",
                "openai/gpt-image-2/edit | OpenAI GPT Image 2",
                "vendor/no-label",
            ],
        )
        # Roundtrip: parsing the formatted text reproduces the endpoint + label
        # for the two with labels; the third gets its label re-derived.
        parsed = SelfieTab.parse_model_lines(text)
        self.assertEqual(
            [m["endpoint"] for m in parsed],
            ["fal-ai/flux-pro/kontext", "openai/gpt-image-2/edit", "vendor/no-label"],
        )
        self.assertEqual(parsed[0]["label"], "Kontext Pro")
        self.assertEqual(parsed[1]["label"], "OpenAI GPT Image 2")
        # Empty-label model gets the intelligent derivation.
        self.assertEqual(parsed[2]["label"], "Vendor No Label")

    def test_format_models_for_editing_empty_and_skips_invalid(self):
        """Empty input → empty string. Entries with no endpoint (corrupt
        config, partial dict) are silently skipped — the modal should
        never show a line whose endpoint is missing, since that line
        couldn't parse back into a model anyway.

        Note: the dialog is responsible for passing custom-only models
        (not built-ins) — the helper does no built-in filtering itself
        since it has no way to know which endpoints are built-in. That
        filter is verified by the dialog tests in
        tests/test_edit_models_dialog.py.
        """
        self.assertEqual(SelfieTab._format_models_for_editing([]), "")
        out = SelfieTab._format_models_for_editing([
            {"endpoint": "", "label": "no-endpoint"},
            {"endpoint": "  ", "label": "whitespace-endpoint"},
            {"label": "missing-endpoint-key"},
            {"endpoint": "vendor/real", "label": "Real"},
        ])
        self.assertEqual(out, "vendor/real | Real")

    def test_parse_skips_comment_lines(self):
        """Lines starting with `#` are reference / comment lines (used to
        annotate built-ins in the modal). They must NOT be parsed as
        endpoints, even if they happen to contain `/`. PR #77's
        placeholder relied on this; the edit-mode modal relies on it
        more heavily.
        """
        out = SelfieTab.parse_model_lines(
            "# fal-ai/builtin-comment | Built-in, not editable\n"
            "fal-ai/real-one/edit | My Real One\n"
            "# vendor/another-comment\n"
        )
        self.assertEqual([m["endpoint"] for m in out], ["fal-ai/real-one/edit"])
        self.assertEqual(out[0]["label"], "My Real One")


class SelfieTabReplaceCustomModelsTests(unittest.TestCase):
    """v2.25: editing models REPLACES the custom-models list. The old
    add-only flow appended; the new flow must let the user remove an
    entry by deleting its line. ``SelfieTab._apply_edited_custom_models``
    is the new method that swaps the list in place.
    """

    def test_apply_replaces_custom_list_and_drops_removed(self):
        """If the user opens the modal with [A, B] and saves [A, C], the
        result must be [A, C] (B removed, C added). Built-in models stay
        in ``_model_options`` untouched. The selected-state map prunes
        endpoints that no longer exist."""
        stub = types.SimpleNamespace(
            _model_options=[
                {"endpoint": "builtin/one", "label": "Built In"},
                {"endpoint": "vendor/a", "label": "A"},
                {"endpoint": "vendor/b", "label": "B"},
            ],
            _custom_models=[
                {"endpoint": "vendor/a", "label": "A",
                 "slug": "vendor-a", "provider": "fal",
                 "api_url": "https://fal.ai/models/vendor/a/api"},
                {"endpoint": "vendor/b", "label": "B",
                 "slug": "vendor-b", "provider": "fal",
                 "api_url": "https://fal.ai/models/vendor/b/api"},
            ],
            _supported_model_endpoints={"builtin/one", "vendor/a", "vendor/b"},
            _model_vars={},
            config={"selfie_selected_models": {
                "builtin/one": True, "vendor/a": True, "vendor/b": True}},
        )
        # Built-ins so the method knows which endpoints to PRESERVE in
        # _model_options.
        builtin_endpoints = {"builtin/one"}
        new_custom = SelfieTab.parse_model_lines(
            "vendor/a | A Renamed\n"
            "vendor/c | C\n"
        )
        SelfieTab._apply_edited_custom_models(stub, new_custom, builtin_endpoints)
        self.assertEqual(
            [m["endpoint"] for m in stub._custom_models],
            ["vendor/a", "vendor/c"],
        )
        # Built-in still present at the front; B removed; C added at the back.
        self.assertEqual(
            [m["endpoint"] for m in stub._model_options],
            ["builtin/one", "vendor/a", "vendor/c"],
        )
        # vendor/a label was updated in the merge.
        a = next(m for m in stub._model_options if m["endpoint"] == "vendor/a")
        self.assertEqual(a["label"], "A Renamed")
        # selected-state map drops the now-removed endpoint.
        self.assertNotIn("vendor/b", stub.config["selfie_selected_models"])
        self.assertIn("builtin/one", stub.config["selfie_selected_models"])

    def test_apply_with_empty_list_clears_custom(self):
        """Saving an empty modal removes ALL custom models (legitimate
        user intent: 'clear my customizations')."""
        stub = types.SimpleNamespace(
            _model_options=[
                {"endpoint": "builtin/one", "label": "Built In"},
                {"endpoint": "vendor/a", "label": "A"},
            ],
            _custom_models=[
                {"endpoint": "vendor/a", "label": "A",
                 "slug": "vendor-a", "provider": "fal",
                 "api_url": "https://fal.ai/models/vendor/a/api"},
            ],
            _supported_model_endpoints={"builtin/one", "vendor/a"},
            _model_vars={},
            config={"selfie_selected_models": {"vendor/a": True}},
        )
        SelfieTab._apply_edited_custom_models(stub, [], {"builtin/one"})
        self.assertEqual(stub._custom_models, [])
        self.assertEqual(
            [m["endpoint"] for m in stub._model_options],
            ["builtin/one"],
        )


if __name__ == "__main__":
    unittest.main()
