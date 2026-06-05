import json
import os
import shutil
import unittest
import uuid
from contextlib import contextmanager

from kling_gui import folder_identity as fi


class FolderIdentityTests(unittest.TestCase):
    @contextmanager
    def _tmp(self):
        root = os.path.join(os.getcwd(), "tests_tmp", f"fid-{uuid.uuid4().hex}")
        os.makedirs(root, exist_ok=True)
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_read_on_markerless_folder_returns_none(self):
        with self._tmp() as root:
            self.assertIsNone(fi.read_folder_id(root))

    def test_ensure_writes_once_and_is_idempotent(self):
        with self._tmp() as root:
            fid = fi.ensure_folder_id(root, seed_name="proj")
            self.assertTrue(fid and fid.startswith("sg-"))
            self.assertTrue(
                os.path.isfile(os.path.join(root, fi.MARKER_NAME)))
            # Re-call returns the same id, does not rewrite a new one.
            self.assertEqual(fi.ensure_folder_id(root), fid)
            self.assertEqual(fi.read_folder_id(root), fid)

    def test_corrupt_marker_self_heals_on_ensure(self):
        with self._tmp() as root:
            with open(os.path.join(root, fi.MARKER_NAME), "w", encoding="utf-8") as fh:
                fh.write("{ not valid json")
            # read is tolerant (None), ensure replaces it with a valid one.
            self.assertIsNone(fi.read_folder_id(root))
            fid = fi.ensure_folder_id(root)
            self.assertTrue(fid and fid.startswith("sg-"))
            self.assertEqual(fi.read_folder_id(root), fid)

    def test_ensure_on_missing_folder_returns_none(self):
        with self._tmp() as root:
            missing = os.path.join(root, "does-not-exist")
            self.assertIsNone(fi.ensure_folder_id(missing))

    def test_index_maps_id_to_path_across_children(self):
        with self._tmp() as root:
            a = os.path.join(root, "A")
            b = os.path.join(root, "B")
            os.makedirs(a)
            os.makedirs(b)
            fa = fi.ensure_folder_id(a)
            fb = fi.ensure_folder_id(b)
            # No marker in C — must be absent from the index.
            os.makedirs(os.path.join(root, "C"))
            index = fi.index_live_folder_ids([root])
            self.assertEqual(
                os.path.normcase(os.path.abspath(index[fa])),
                os.path.normcase(os.path.abspath(a)))
            self.assertEqual(
                os.path.normcase(os.path.abspath(index[fb])),
                os.path.normcase(os.path.abspath(b)))
            self.assertEqual(len(index), 2)

    def test_index_finds_renamed_folder_by_id(self):
        with self._tmp() as root:
            proj = os.path.join(root, "Original")
            os.makedirs(proj)
            fid = fi.ensure_folder_id(proj)
            os.rename(proj, os.path.join(root, "Renamed"))
            index = fi.index_live_folder_ids([root])
            self.assertEqual(
                os.path.basename(index[fid]), "Renamed")

    def test_marker_payload_shape(self):
        with self._tmp() as root:
            fi.ensure_folder_id(root, seed_name="MyProj")
            with open(os.path.join(root, fi.MARKER_NAME), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("id", data)
            self.assertEqual(data["seed_name"], "MyProj")
            self.assertIn("created_at", data)


if __name__ == "__main__":
    unittest.main()
