"""
Tests for automation/video_aa.py — the AA (adversarial-attack) wrapper.

These run on the MAIN repo venv, which CANNOT import the aa-video subproject's
deps. So every test here exercises the wrapper's resolver / normalize / graceful-
skip logic WITHOUT actually launching the isolated venv (the launcher subprocess
is mocked or absent). The real end-to-end run is exercised by the standalone
aa-video launcher, not pytest.
"""

import os
from pathlib import Path

from automation.video_aa import (
    AA_PIPELINES,
    AA_SUFFIXES,
    DEFAULT_AA_ATTACKS,
    aa_suffix,
    build_aa_output_path,
    is_aa_artifact,
    normalize_aa_attacks,
    resolve_aa_launcher,
    resolve_produced_aa_output,
    run_aa,
)


# ---------------------------------------------------------------------------
# normalize_aa_attacks — the single source of truth
# ---------------------------------------------------------------------------
class TestNormalizeAaAttacks:
    def test_bare_default_is_prime(self):
        # With no args at all, normalize returns the recommended default.
        assert normalize_aa_attacks() == ["prime"]

    def test_explicit_empty_is_off(self):
        assert normalize_aa_attacks(attacks=[]) == []

    def test_explicit_list_wins_and_orders_highest_first(self):
        # prime(3) > scenario1(2) > scenario3(1)
        assert normalize_aa_attacks(attacks=["scenario3", "prime", "scenario1"]) == [
            "prime",
            "scenario1",
            "scenario3",
        ]

    def test_legacy_true_migrates_to_prime(self):
        assert normalize_aa_attacks(legacy_enabled=True) == ["prime"]

    def test_legacy_false_is_off(self):
        assert normalize_aa_attacks(legacy_enabled=False) == []

    def test_dedup(self):
        assert normalize_aa_attacks(attacks=["prime", "prime"]) == ["prime"]

    def test_loose_aliases(self):
        assert normalize_aa_attacks(attacks=["s1", "s3"]) == ["scenario1", "scenario3"]

    def test_unknown_token_dropped(self):
        assert normalize_aa_attacks(attacks=["prime", "bogus"]) == ["prime"]

    def test_str_coerced_to_list(self):
        assert normalize_aa_attacks(attacks="prime") == ["prime"]

    def test_list_takes_precedence_over_legacy(self):
        # An explicit (non-_UNSET) attacks list wins even with legacy present.
        assert normalize_aa_attacks(attacks=["scenario1"], legacy_enabled=True) == [
            "scenario1"
        ]


# ---------------------------------------------------------------------------
# Suffix + output-path helpers
# ---------------------------------------------------------------------------
class TestSuffixAndPaths:
    def test_known_suffixes(self):
        assert aa_suffix("prime") == "_aa-prime"
        assert aa_suffix("scenario1") == "_aa-s1"
        assert aa_suffix("scenario3") == "_aa-s3"

    def test_unknown_suffix_fallback(self):
        assert aa_suffix("nope") == "_aa"

    def test_suffixes_distinct_per_pipeline(self):
        vals = list(AA_SUFFIXES.values())
        assert len(vals) == len(set(vals)), "AA suffixes must be unique"

    def test_default_attacks_constant(self):
        assert DEFAULT_AA_ATTACKS == ["prime"]

    def test_pipelines_registry(self):
        for key in ("prime", "scenario1", "scenario3"):
            assert key in AA_PIPELINES

    def test_build_aa_output_path(self, tmp_path):
        src = tmp_path / "clip.mp4"
        out = build_aa_output_path(src, "prime", 0.5)
        assert out.name == "clip_aa-prime.mp4"


# ---------------------------------------------------------------------------
# is_aa_artifact
# ---------------------------------------------------------------------------
class TestIsAaArtifact:
    def test_end_of_stem(self):
        assert is_aa_artifact(Path("clip_aa.mp4"))

    def test_hyphen_form(self):
        assert is_aa_artifact(Path("clip_aa-prime.mp4"))

    def test_negative(self):
        assert not is_aa_artifact(Path("clip.mp4"))
        assert not is_aa_artifact(Path("clip_crush720.mp4"))


# ---------------------------------------------------------------------------
# resolve_produced_aa_output — injector-rename safety net
# ---------------------------------------------------------------------------
class TestResolveProducedOutput:
    def test_exact_requested_wins(self, tmp_path):
        requested = tmp_path / "clip_aa-prime.mp4"
        requested.write_bytes(b"x")
        assert resolve_produced_aa_output(requested, "prime", 0.5) == requested

    def test_glob_fallback_newest(self, tmp_path):
        # Requested path absent; tool wrote its default-naming form instead.
        requested = tmp_path / "clip_aa-prime.mp4"
        produced = tmp_path / "clip_prime_0.5.mp4"
        produced.write_bytes(b"x")
        result = resolve_produced_aa_output(requested, "prime", 0.5)
        assert result == produced

    def test_returns_none_when_nothing(self, tmp_path):
        requested = tmp_path / "missing_aa-prime.mp4"
        assert resolve_produced_aa_output(requested, "prime", 0.5) is None

    def test_glob_metachar_filename_is_literal(self, tmp_path):
        # A base stem with glob metacharacters ([, ], *, ?) must NOT be
        # interpreted as a glob pattern (codex WARNING). The literal
        # iterdir() scan finds the real produced file.
        requested = tmp_path / "clip[01]_aa-prime.mp4"
        produced = tmp_path / "clip[01]_prime_0.5.mp4"
        produced.write_bytes(b"x")
        # A decoy that a naive glob ("clip[01]_prime*") would wrongly match
        # (glob treats [01] as a char class → matches "clip0..."/"clip1...").
        decoy = tmp_path / "clip0_prime_0.5.mp4"
        decoy.write_bytes(b"y")
        result = resolve_produced_aa_output(requested, "prime", 0.5)
        assert result == produced, f"literal match expected {produced}, got {result}"


# ---------------------------------------------------------------------------
# resolve_aa_launcher
# ---------------------------------------------------------------------------
class TestResolveLauncher:
    def test_none_when_subproject_absent(self, tmp_path):
        # An empty repo_root has no aa-video/ — must resolve to None (graceful).
        assert resolve_aa_launcher(tmp_path) is None

    def test_finds_launcher_when_present(self, tmp_path):
        aa_dir = tmp_path / "aa-video"
        aa_dir.mkdir()
        (aa_dir / "main.py").write_text("# stub")
        launcher_name = "aa_launcher.bat" if os.name == "nt" else "aa_launcher.sh"
        (aa_dir / launcher_name).write_text("# stub")
        resolved = resolve_aa_launcher(tmp_path)
        assert resolved is not None
        assert resolved.name == launcher_name

    def test_none_when_launcher_present_but_entry_missing(self, tmp_path):
        aa_dir = tmp_path / "aa-video"
        aa_dir.mkdir()
        launcher_name = "aa_launcher.bat" if os.name == "nt" else "aa_launcher.sh"
        (aa_dir / launcher_name).write_text("# stub")
        # No main.py → None.
        assert resolve_aa_launcher(tmp_path) is None


# ---------------------------------------------------------------------------
# run_aa — graceful-skip paths (no real subprocess / venv)
# ---------------------------------------------------------------------------
class TestRunAaGracefulSkip:
    def test_missing_input_returns_none(self, tmp_path):
        logs = []
        result = run_aa(
            str(tmp_path / "nope.mp4"),
            attack="prime",
            log_callback=lambda m, l: logs.append((l, m)),
            repo_root=tmp_path,
        )
        assert result is None
        assert any("not found" in m.lower() for _l, m in logs)

    def test_launcher_absent_returns_none(self, tmp_path):
        # Input exists but the aa-video subproject does not → graceful skip.
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        logs = []
        result = run_aa(
            str(clip),
            attack="prime",
            log_callback=lambda m, l: logs.append((l, m)),
            repo_root=tmp_path,  # tmp_path has no aa-video/
        )
        assert result is None
        assert any("skip" in m.lower() for _l, m in logs)

    def test_never_raises_on_failure(self, tmp_path):
        # The contract: run_aa always degrades to None, never propagates.
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        # No launcher, no ffmpeg assumptions — must just return None.
        assert run_aa(str(clip), repo_root=tmp_path) is None
