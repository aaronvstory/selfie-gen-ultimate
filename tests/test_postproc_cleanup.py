"""Unit coverage for the fan-out intermediate pruner.

Locks the safety contract: real deliverables are preserved, true stepping-stone
intermediates are removed, and the helper never raises on missing/locked files.
"""
from pathlib import Path

from automation.postproc_cleanup import prune_strict_intermediates


def _touch(p: Path) -> Path:
    p.write_bytes(b"x")
    return p


def test_prunes_only_non_kept(tmp_path):
    deliverable = _touch(tmp_path / "final-oldcam.mp4")
    raw_base = _touch(tmp_path / "raw.mp4")
    rppg = _touch(tmp_path / "raw-rppg.mp4")
    intermediate = _touch(tmp_path / "raw-crush720.mp4")  # stepping stone only

    cache_values = [deliverable, raw_base, rppg, intermediate]
    keep = {deliverable, raw_base, rppg}

    pruned = prune_strict_intermediates(cache_values, keep)

    assert pruned == [intermediate.name]
    assert deliverable.exists() and raw_base.exists() and rppg.exists()
    assert not intermediate.exists()


def test_accepts_str_and_path_mixed(tmp_path):
    """GUI caches str, CLI caches Path — the helper must handle both."""
    keep_file = _touch(tmp_path / "keep.mp4")
    drop_file = _touch(tmp_path / "drop.mp4")
    # cache as str, keep as Path (and vice-versa) must still match.
    pruned = prune_strict_intermediates(
        [str(keep_file), str(drop_file)], {keep_file}
    )
    assert pruned == [drop_file.name]
    assert keep_file.exists()
    assert not drop_file.exists()


def test_full_powerset_is_noop(tmp_path):
    """When every cached file is also a deliverable (full powerset), nothing
    is deleted."""
    files = [_touch(tmp_path / f"v{i}.mp4") for i in range(4)]
    pruned = prune_strict_intermediates(files, set(files))
    assert pruned == []
    assert all(f.exists() for f in files)


def test_missing_file_does_not_raise(tmp_path):
    ghost = tmp_path / "never-existed.mp4"
    # Not in keep, doesn't exist -> skipped silently, no exception.
    assert prune_strict_intermediates([ghost], set()) == []


def test_on_pruned_callback_fires(tmp_path):
    drop = _touch(tmp_path / "drop.mp4")
    seen = []
    prune_strict_intermediates([drop], set(), on_pruned=seen.append)
    assert seen == [drop.name]
