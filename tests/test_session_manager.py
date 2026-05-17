import json
import os
import shutil
import unittest
import uuid
from contextlib import contextmanager

from kling_gui import session_manager as sm


class _DummyEntry:
    def __init__(self, path: str):
        self.path = path


class _DummySession:
    def __init__(self, source_path: str):
        self.reference_entry = _DummyEntry(source_path)
        self.input_images = [(0, _DummyEntry(source_path))]
        self.images = [_DummyEntry(source_path)]
        self.count = 1

    def to_dict(self) -> dict:
        return {
            "images": [{"path": self.reference_entry.path, "source_type": "input"}],
            "current_index": 0,
            "reference_index": 0,
            "similarity_ref_index": -1,
        }


class SessionManagerTests(unittest.TestCase):
    @contextmanager
    def _workspace(self):
        root = os.path.join(os.getcwd(), "tests_tmp", f"sessions-{uuid.uuid4().hex}")
        os.makedirs(root, exist_ok=True)
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _make_session(self, root: str, project_name: str) -> _DummySession:
        project_dir = os.path.join(root, project_name)
        os.makedirs(project_dir, exist_ok=True)
        source_path = os.path.join(project_dir, f"{project_name}_front.png")
        with open(source_path, "wb") as handle:
            handle.write(b"x")
        return _DummySession(source_path)

    def test_legacy_file_lists_with_inferred_metadata(self):
        with self._workspace() as app_dir:
            sessions_dir = os.path.join(app_dir, "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            legacy_path = os.path.join(sessions_dir, "alpha_autosave.json")
            payload = {
                "name": "alpha_autosave",
                "session": {"images": [{"path": "x.png"}]},
            }
            with open(legacy_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            listed = sm.list_sessions(app_dir)
            self.assertEqual(len(listed), 1)
            rec = listed[0]
            self.assertEqual(rec.session_kind, sm.SESSION_KIND_AUTOSAVE)
            self.assertEqual(rec.project_key, "alpha")
            self.assertEqual(rec.image_count, 1)
            self.assertTrue(rec.updated_at)

    def test_autosave_is_single_rolling_file(self):
        with self._workspace() as app_dir:
            session = self._make_session(app_dir, "project_one")
            for _ in range(12):
                sm.save_session(
                    app_dir,
                    session,
                    config={},
                    session_kind=sm.SESSION_KIND_AUTOSAVE,
                )

            listed = sm.list_sessions(app_dir)
            autosaves = [
                rec for rec in listed
                if rec.session_kind == sm.SESSION_KIND_AUTOSAVE and rec.project_key == "project_one"
            ]
            # Exactly one rolling autosave file, deterministically named.
            self.assertEqual(len(autosaves), 1)
            self.assertEqual(
                os.path.basename(autosaves[0].path), "project_one_autosave.json"
            )
            with open(autosaves[0].path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertIn("content_fingerprint", data)

    def test_autosave_skips_write_when_unchanged(self):
        with self._workspace() as app_dir:
            session = self._make_session(app_dir, "stable_proj")
            first = sm.save_session(
                app_dir, session, config={},
                session_kind=sm.SESSION_KIND_AUTOSAVE, skip_if_unchanged=True,
            )
            self.assertIsNotNone(first)
            # Nothing changed → second call is skipped (returns None, no rewrite).
            second = sm.save_session(
                app_dir, session, config={},
                session_kind=sm.SESSION_KIND_AUTOSAVE, skip_if_unchanged=True,
            )
            self.assertIsNone(second)
            # Content changes → write happens again.
            session.reference_entry.path = session.reference_entry.path + "_v2"
            third = sm.save_session(
                app_dir, session, config={},
                session_kind=sm.SESSION_KIND_AUTOSAVE, skip_if_unchanged=True,
            )
            self.assertIsNotNone(third)

    def test_failed_write_preserves_previous_autosave(self):
        # The single rolling file is the only safety net: a write that blows
        # up mid-serialization must NOT destroy the prior good autosave, and
        # must not leave a .tmp_ turd behind.
        import json as _json
        with self._workspace() as app_dir:
            session = self._make_session(app_dir, "durable_proj")
            first = sm.save_session(
                app_dir, session, config={}, session_kind=sm.SESSION_KIND_AUTOSAVE,
            )
            self.assertIsNotNone(first)
            assert first is not None
            with open(first, "r", encoding="utf-8") as h:
                good = _json.load(h)

            # Make json.dump blow up *inside* _atomic_write_json (after the
            # temp file is opened) by returning a non-serializable object that
            # still survives fingerprinting (fingerprint catches & falls back).
            session.to_dict = lambda: {"images": [{"path": object()}]}  # type: ignore[assignment]
            with self.assertRaises(TypeError):
                sm.save_session(
                    app_dir, session, config={},
                    session_kind=sm.SESSION_KIND_AUTOSAVE,
                )
            # Previous autosave intact, no temp files left.
            with open(first, "r", encoding="utf-8") as h:
                self.assertEqual(_json.load(h), good)
            leftovers = [
                n for n in os.listdir(os.path.join(app_dir, "sessions"))
                if n.startswith(".tmp_")
            ]
            self.assertEqual(leftovers, [])

    def test_collapse_legacy_autosaves_keeps_one_newest(self):
        with self._workspace() as app_dir:
            sessions_dir = os.path.join(app_dir, "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            for i in range(5):
                p = os.path.join(sessions_dir, f"gamma_autosave_2026010{i}_010101.json")
                with open(p, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "name": f"gamma_autosave_2026010{i}_010101",
                            "session_kind": "autosave",
                            "project_key": "gamma",
                            "updated_at": f"2026-01-0{i}T01:01:01",
                            "session": {"images": [{"path": "g.png"}]},
                        },
                        handle,
                    )
            # Newest content is written to the rolling file, then all 5 legacy
            # timestamped files are purged (none is the rolling file).
            removed = sm.collapse_legacy_autosaves(app_dir)
            self.assertEqual(removed, 5)
            listed = [
                r for r in sm.list_sessions(app_dir)
                if r.session_kind == sm.SESSION_KIND_AUTOSAVE
            ]
            self.assertEqual(len(listed), 1)
            self.assertEqual(
                os.path.basename(listed[0].path), "gamma_autosave.json"
            )
            # Newest (i=4) content was the one preserved into the rolling file.
            self.assertEqual(listed[0].updated_at, "2026-01-04T01:01:01")
            # Idempotent: a second pass finds only the rolling file, removes nothing.
            self.assertEqual(sm.collapse_legacy_autosaves(app_dir), 0)

    def test_purge_legacy_autosaves_is_filename_scoped(self):
        # Hot-path purge must (a) only touch the target project's autosaves,
        # (b) keep the rolling file, (c) leave manual saves alone, and
        # (d) not depend on JSON contents (corrupt files still get purged).
        with self._workspace() as app_dir:
            sessions_dir = os.path.join(app_dir, "sessions")
            os.makedirs(sessions_dir, exist_ok=True)
            keep = os.path.join(sessions_dir, "alpha_autosave.json")
            others = [
                "alpha_autosave_20260101_010101.json",
                "alpha_autosave_20260102_010101_2.json",
                "beta_autosave_20260101_010101.json",   # different project
                "alpha_manual.json",                     # manual save
            ]
            for fn in [os.path.basename(keep), *others]:
                with open(os.path.join(sessions_dir, fn), "w", encoding="utf-8") as h:
                    h.write("not even valid json {{{")  # contents must not matter

            removed = sm._purge_legacy_autosaves(app_dir, "alpha", keep)
            self.assertEqual(removed, 2)  # the two timestamped alpha autosaves
            remaining = sorted(os.listdir(sessions_dir))
            self.assertEqual(
                remaining,
                ["alpha_autosave.json", "alpha_manual.json",
                 "beta_autosave_20260101_010101.json"],
            )

    def test_infer_project_key_handles_no_timestamp_autosave(self):
        self.assertEqual(
            sm._infer_project_key({}, "myproj_autosave.json"), "myproj"
        )

    def test_build_session_from_folder_scans_recognized_images(self):
        with self._workspace() as root:
            proj = os.path.join(root, "RenamedProject")
            sub = os.path.join(proj, "gen-images")
            os.makedirs(sub, exist_ok=True)
            for fn in ("a.png", "b.JPG", "notes.txt", "c.webp"):
                with open(os.path.join(proj if fn != "c.webp" else sub, fn), "wb") as h:
                    h.write(b"x")
            data = sm.build_session_from_folder(proj)
            assert data is not None
            imgs = data["session"]["images"]
            paths = sorted(os.path.basename(i["path"]) for i in imgs)
            self.assertEqual(paths, ["a.png", "b.JPG", "c.webp"])
            self.assertEqual(data["project_key"], "RenamedProject")
            self.assertTrue(all(i["source_type"] == "input" for i in imgs))

    def test_build_session_from_folder_returns_none_when_no_images(self):
        with self._workspace() as root:
            empty = os.path.join(root, "empty_proj")
            os.makedirs(empty, exist_ok=True)
            with open(os.path.join(empty, "readme.txt"), "wb") as h:
                h.write(b"x")
            self.assertIsNone(sm.build_session_from_folder(empty))

    def test_manual_saves_are_not_touched_by_rolling_autosave(self):
        with self._workspace() as app_dir:
            session = self._make_session(app_dir, "project_two")
            manual_path = sm.save_session(
                app_dir,
                session,
                config={},
                name="project_two_manual",
                session_kind=sm.SESSION_KIND_MANUAL,
            )
            for _ in range(11):
                sm.save_session(
                    app_dir,
                    session,
                    config={},
                    session_kind=sm.SESSION_KIND_AUTOSAVE,
                )

            self.assertTrue(manual_path and os.path.isfile(manual_path))
            listed = sm.list_sessions(app_dir)
            manual = [rec for rec in listed if rec.session_kind == sm.SESSION_KIND_MANUAL]
            autosave = [rec for rec in listed if rec.session_kind == sm.SESSION_KIND_AUTOSAVE]
            # Manual save survives; autosave collapsed to a single rolling file.
            self.assertEqual(len(manual), 1)
            self.assertEqual(len(autosave), 1)

    def test_delete_project_sessions_removes_only_target_project(self):
        with self._workspace() as app_dir:
            session_a = self._make_session(app_dir, "alpha")
            session_b = self._make_session(app_dir, "bravo")

            sm.save_session(app_dir, session_a, {}, session_kind=sm.SESSION_KIND_AUTOSAVE)
            sm.save_session(app_dir, session_a, {}, name="alpha_manual", session_kind=sm.SESSION_KIND_MANUAL)
            sm.save_session(app_dir, session_b, {}, name="bravo_manual", session_kind=sm.SESSION_KIND_MANUAL)

            removed = sm.delete_project_sessions(app_dir, "alpha")
            self.assertGreaterEqual(removed, 2)
            remaining = sm.list_sessions(app_dir)
            remaining_projects = {rec.project_key for rec in remaining}
            self.assertEqual(remaining_projects, {"bravo"})

    def test_sort_uses_updated_at_then_timestamp_then_mtime(self):
        with self._workspace() as app_dir:
            sessions_dir = os.path.join(app_dir, "sessions")
            os.makedirs(sessions_dir, exist_ok=True)

            older_path = os.path.join(sessions_dir, "older_manual.json")
            newer_path = os.path.join(sessions_dir, "newer_manual.json")
            with open(older_path, "w", encoding="utf-8") as handle:
                json.dump({"name": "older", "session_kind": "manual", "session": {"images": []}}, handle)
            with open(newer_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "name": "newer",
                        "session_kind": "manual",
                        "updated_at": "2099-01-01T00:00:00",
                        "session": {"images": []},
                    },
                    handle,
                )
            os.utime(older_path, (1_600_000_000, 1_600_000_000))
            os.utime(newer_path, (1_500_000_000, 1_500_000_000))

            listed = sm.list_sessions(app_dir)
            self.assertEqual(listed[0].name, "newer")
            self.assertEqual(listed[1].name, "older")


if __name__ == "__main__":
    unittest.main()
