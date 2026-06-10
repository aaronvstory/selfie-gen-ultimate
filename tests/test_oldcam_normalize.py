"""Contract tests for the canonical oldcam version-selection form.

``automation_oldcam_version`` historically held a single string ("v13" or
"all"). The multi-select work canonicalizes it to a list (``["v13", "v24"]``,
``["all"]``, ``[]``) with ``normalize_oldcam_versions`` as the single
coercion choke point. These tests pin that contract: every consumer
(fan-out, manifest fingerprint, UI, headless overrides) relies on the
normalizer's deterministic output.
"""

from pathlib import Path

import pytest

from automation.oldcam import (
    normalize_oldcam_versions,
    run_oldcam_all,
)


class TestNormalizeOldcamVersions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("v13", ["v13"]),
            ("V13", ["v13"]),
            (" v13 ", ["v13"]),
            ("all", ["all"]),
            ("ALL", ["all"]),
            ("none", []),
            ("", []),
            (None, []),
            ("v13,v24", ["v13", "v24"]),
            ("v24,v13", ["v13", "v24"]),
            ("v13, v13", ["v13"]),
            ("v13,all", ["all"]),
            ("v13,none,v24", ["v13", "v24"]),
            (["v13"], ["v13"]),
            (["v24", "v13"], ["v13", "v24"]),
            (["v13", "V13"], ["v13"]),
            (["all"], ["all"]),
            (["v13", "all", "v24"], ["all"]),
            ([], []),
            (["none"], []),
            (("v9", "v10"), ["v9", "v10"]),
        ],
    )
    def test_normalize_matrix(self, value, expected):
        assert normalize_oldcam_versions(value) == expected

    def test_numeric_sort_not_lexicographic(self):
        # v9 < v10 numerically; lexicographic sort would yield v10 first.
        assert normalize_oldcam_versions("v10,v9") == ["v9", "v10"]

    def test_idempotent(self):
        once = normalize_oldcam_versions("v24, v13")
        assert normalize_oldcam_versions(once) == once


class TestRunOldcamAllSelection:
    @pytest.fixture
    def fake_versions(self, monkeypatch):
        monkeypatch.setattr(
            "automation.oldcam.discover_oldcam_versions",
            lambda repo_root: ["v7", "v13", "v24"],
        )

    @pytest.fixture
    def run_calls(self, monkeypatch):
        calls = []

        def fake_run_version(*, video_path, version, repo_root, progress_cb=None, **kwargs):
            calls.append(version)
            return Path(f"{video_path.stem}-oldcam-{version}.mp4")

        monkeypatch.setattr("automation.oldcam.run_oldcam_version", fake_run_version)
        return calls

    def _run(self, setting):
        return run_oldcam_all(
            video_path=Path("video.mp4"),
            version_setting=setting,
            repo_root=Path("."),
        )

    def test_subset_list_runs_only_selected(self, fake_versions, run_calls):
        outputs = self._run(["v13", "v24"])
        assert run_calls == ["v13", "v24"]
        assert [version for version, _ in outputs] == ["v13", "v24"]

    def test_legacy_string_still_works(self, fake_versions, run_calls):
        outputs = self._run("v13")
        assert run_calls == ["v13"]
        assert [version for version, _ in outputs] == ["v13"]

    def test_all_runs_every_discovered_version(self, fake_versions, run_calls):
        self._run(["all"])
        assert run_calls == ["v7", "v13", "v24"]

    def test_empty_selection_runs_nothing(self, fake_versions, run_calls):
        messages = []
        outputs = run_oldcam_all(
            video_path=Path("video.mp4"),
            version_setting=[],
            repo_root=Path("."),
            progress_cb=lambda msg, level: messages.append((msg, level)),
        )
        assert outputs == []
        assert run_calls == []
        assert any("No oldcam versions selected" in msg for msg, _ in messages)

    def test_partially_unavailable_warns_and_runs_rest(self, fake_versions, run_calls):
        messages = []
        outputs = run_oldcam_all(
            video_path=Path("video.mp4"),
            version_setting=["v13", "v99"],
            repo_root=Path("."),
            progress_cb=lambda msg, level: messages.append((msg, level)),
        )
        assert run_calls == ["v13"]
        assert [version for version, _ in outputs] == ["v13"]
        assert any("v99" in msg and level == "warning" for msg, level in messages)

    def test_fully_unavailable_warns_and_runs_nothing(self, fake_versions, run_calls):
        messages = []
        outputs = run_oldcam_all(
            video_path=Path("video.mp4"),
            version_setting=["v98", "v99"],
            repo_root=Path("."),
            progress_cb=lambda msg, level: messages.append((msg, level)),
        )
        assert outputs == []
        assert run_calls == []
        assert any(level == "warning" for _, level in messages)
