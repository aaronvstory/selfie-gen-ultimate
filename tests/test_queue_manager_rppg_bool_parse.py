"""Regression test for the GUI queue rPPG config bool parse.

GPT-5.5 finding on PR #43: ``bool(cfg.get(key, default))`` silently
breaks for stringy config values — ``bool("false")`` returns ``True``
because non-empty strings are truthy. CodeRabbit caught the same class
of bug on PR #19 for ``automation_similarity_require_fas_pass``.

The GUI queue _rppg_video uses three iterative-mode flags:
``rppg_iterate_from_baseline``, ``rppg_skip_diagnosis``,
``rppg_skip_kinematic_gate``. All three default ON; users overriding to
OFF via a JSON config edit type "false" must NOT have it silently
re-enabled.

The fix (queue_manager.py PR #43 follow-up) routes through
``face_similarity._parse_bool`` — the canonical helper that already
backs ``automation.pipeline._read_bool``.
"""

from __future__ import annotations

from pathlib import Path

import unittest


def _read_queue_manager() -> str:
    return (
        Path(__file__).resolve().parent.parent / "kling_gui" / "queue_manager.py"
    ).read_text(encoding="utf-8")


class QueueManagerRppgBoolParseTests(unittest.TestCase):
    def test_uses_parse_bool_helper(self):
        """_rppg_video must reuse face_similarity._parse_bool for the
        3 iterative-mode flags so 'false' string config values resolve
        to False, not True."""
        src = _read_queue_manager()
        # The helper is imported lazily inside _rppg_video (avoids a
        # top-level circular import via face_similarity → DeepFace).
        self.assertIn("from face_similarity import _parse_bool", src)

    def test_all_three_iterative_flags_route_through_parse(self):
        """Each of the three iterative flag reads MUST go through the
        local _cfg_bool helper, NOT raw bool(cfg.get(...))."""
        src = _read_queue_manager()
        # Locate the _rppg_video body and verify each key goes through
        # _cfg_bool, not bool().
        start = src.index("def _rppg_video")
        # Bound the search to the function body (roughly: until the
        # next top-level method def, generous 6000-char window).
        end = src.find("\n    def ", start + 10)
        body = src[start:end] if end > 0 else src[start : start + 6000]
        # The 3 flags. All read through _cfg_bool with both the bare
        # GUI key (rppg_...) AND the automation_ fallback name (PR #43
        # code-reviewer P1 — GUI uses bare keys, automation/config.py
        # uses prefixed; we honor both so a JSON hand-edit in either
        # namespace is respected).
        for bare, auto in (
            ("rppg_iterate_from_baseline", "automation_rppg_iterate_from_baseline"),
            ("rppg_skip_diagnosis", "automation_rppg_skip_diagnosis"),
            ("rppg_skip_kinematic_gate", "automation_rppg_skip_kinematic_gate"),
        ):
            self.assertRegex(
                body,
                rf'_cfg_bool\(\s*\n?\s*"{bare}",\s*\n?\s*"{auto}",\s*\n?\s*True',
                f"_rppg_video must read {bare!r} via _cfg_bool with "
                f"both names ({bare} bare + {auto} fallback).",
            )
            # And NOT via raw bool(cfg.get(...)) — the original bug.
            self.assertNotRegex(
                body,
                rf'bool\(\s*cfg\.get\(\s*"{bare}"',
                f"_rppg_video must not use raw bool(cfg.get({bare!r}, ...)) — "
                f"bool('false') == True silently re-enables the flag.",
            )

    def test_rppg_mode_honors_both_namespaces(self):
        """rppg_mode (string, not bool) MUST also accept both the
        bare GUI key and the automation_ fallback. The code-reviewer
        finding on PR #43 was that the GUI namespace mismatch silently
        ignored automation_rppg_mode overrides."""
        src = _read_queue_manager()
        start = src.index("def _rppg_video")
        end = src.find("\n    def ", start + 10)
        body = src[start:end] if end > 0 else src[start : start + 6000]
        # _cfg_get is the string-value sibling of _cfg_bool.
        self.assertRegex(
            body,
            r'_cfg_get\(\s*"rppg_mode",\s*"automation_rppg_mode"',
            "rppg_mode must read both namespaces (GUI bare + "
            "automation_ prefix).",
        )

    def test_parse_bool_is_the_canonical_helper(self):
        """Sanity: face_similarity._parse_bool exists and is the same
        helper used by automation.pipeline._read_bool — single source
        of truth for str→bool coercion across the codebase."""
        from face_similarity import _parse_bool
        self.assertIs(_parse_bool("false"), False)
        self.assertIs(_parse_bool("no"), False)
        self.assertIs(_parse_bool("0"), False)
        self.assertIs(_parse_bool("true"), True)
        self.assertIs(_parse_bool("yes"), True)
        self.assertIs(_parse_bool("1"), True)
        self.assertIs(_parse_bool(True), True)
        self.assertIs(_parse_bool(False), False)
        # Unrecognized / garbage returns None — the GUI _cfg_bool
        # falls back to the default in that case.
        self.assertIsNone(_parse_bool("maybe"))


if __name__ == "__main__":
    unittest.main()
