# End-frame lock + dynamic per-model API schemas

> How the selfie/video models advertise their API capabilities and how
> the end-frame lock, negative prompt and cfg_scale flow from the GUI /
> CLI to the fal.ai payload. Single source of truth: `models.json` +
> `model_metadata.get_model_capabilities()`.

## What it is

fal.ai's Kling family diverges per model: v3 renamed the start image to
`start_image_url`; Kling 2.5 Turbo Pro uses `tail_image_url` for the end
frame while v3 / o3 / seedance use `end_image_url`; o3 and seedance
dropped `negative_prompt` and `cfg_scale` entirely. Hard-coding one
shape silently produces broken requests on the others.

Each model now carries capability flags. The dispatcher builds the
payload from those flags; the GUI shows/grays the matching controls
from the **same** flags, so the UI and the wire can never disagree.

## Capability flags (models.json — single source of truth)

Each `models[]` entry has:

| Flag | Meaning |
|------|---------|
| `start_image_param` | API name of the start image (`image_url` or `start_image_url`) |
| `end_image_param` | API name of the end frame, or `null` if the model has none |
| `supports_negative_prompt` | bool — send `negative_prompt`? |
| `supports_cfg_scale` | bool — send `cfg_scale`? |
| `duration_options` / `duration_default` | allowed durations |

Read them ONLY via `model_metadata.get_model_capabilities(endpoint)` —
it always returns a fully-populated dict (conservative defaults for
legacy / custom / unknown models so an unflagged model degrades to a
plain prompt+image submit, never a KeyError or a rejected param).

### Verified roster (fal.ai, 2026-05-19)

| Endpoint | start | end | neg | cfg |
|----------|-------|-----|-----|-----|
| `…/kling-video/v2.5-turbo/standard/image-to-video` | `image_url` | — | ✓ | ✓ |
| `…/kling-video/v2.5-turbo/pro/image-to-video` (default) | `image_url` | `tail_image_url` | ✓ | ✓ |
| `…/kling-video/v3/pro/image-to-video` | `start_image_url` | `end_image_url` | ✓ | ✓ |
| `…/kling-video/v3/standard/image-to-video` | `start_image_url` | `end_image_url` | ✓ | ✓ |
| `…/kling-video/o3/standard/image-to-video` | `image_url` | `end_image_url` | ✗ | ✗ |
| `bytedance/seedance-2.0/image-to-video` (hidden) | `image_url` | `end_image_url` | ✗ | ✗ |

`seedance-2.0` keeps the `bytedance/` prefix (NOT `fal-ai/`) — do not
"correct" it. The default model is unchanged: Kling 2.5 Turbo Pro.

## Payload assembly (kling_generator_falai.create_kling_generation)

1. Resolve `caps = get_model_capabilities(self.model_endpoint)`.
2. Validate the start image URL is non-empty (clear early error).
3. Start image -> `payload[caps["start_image_param"]]`.
4. End frame: an explicit `end_image_url` wins; else if `lock_end_frame`
   and `caps["end_image_param"]` is set -> that param = the start url
   (mechanical return-to-pose). Models with no end param get nothing.
5. `negative_prompt` only if `caps["supports_negative_prompt"]`;
   `cfg_scale` only if `caps["supports_cfg_scale"]`.
6. `schema_manager.validate_parameters` is a second defensive filter
   against the live fal.ai OpenAPI schema.

## Wiring surfaces (all required)

| Layer | File | What |
|-------|------|------|
| Capability flags | `models.json` | per-model flags + the two new roster entries |
| Capability helper | `model_metadata.get_model_capabilities` | single source of truth (dispatcher + GUI + CLI) |
| Dispatcher | `kling_generator_falai.py` | cap-driven payload + `cfg_scale` / `lock_end_frame` kwargs + URL validation |
| GUI queue | `kling_gui/queue_manager.py` | unified on `get_model_capabilities`; passes `cfg_scale` / `lock_end_frame` to both `create_kling_generation` calls |
| GUI controls | `kling_gui/config_panel.py` | "Motion:" row (end-frame checkbox grayed when unsupported, cfg entry, caps label); split positive/negative prompt editor |
| Defaults | `default_config_template.json`, `kling_automation_ui.py` | minimal-motion prompt + negative; `cfg_scale_value` 0.7, `lock_end_frame` true; `RECOMMENDED_DEFAULTS_VERSION` 4 |
| CLI pipeline | `automation/pipeline.py` | passes negative / cfg / lock from the same config keys (CLI⇄GUI parity) |
| Tests | `tests/test_endframe_dynamic_schema.py` | per-model payload matrix + capability-helper safety |

## Prompt-editor split (GUI)

For models that accept `negative_prompt` the prompt editor splits
horizontally: the existing box is the POSITIVE prompt, a NEGATIVE box
appears below it. Backed by `config["negative_prompts"][slot]` (the
same dict the queue reads). For models that dropped negative_prompt the
negative half is `pack_forget`-collapsed (created once — its text
survives toggling). Capability is resolved via the single source helper.
