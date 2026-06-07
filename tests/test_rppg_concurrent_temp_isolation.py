"""Regression guard for the rPPG concurrent-injector temp-file bleed
(fix/rppg-self-heal-baseline-and-encode).

USER-REPORTED FAILURE: rPPG intermittently produced ``-NORPPG`` files.
Two surface symptoms — a ``FileNotFoundError: ... temp_iteration_0.mp4``
crash mid-loop, and a ``-rppg.mp4`` that failed ffprobe and got
quarantined — shared ONE root cause:

The iterative enhancer wrote its numbered intermediate files as bare
relative names (``temp_iteration_<N>.mp4``) in the shared working
directory (``rPPG/``). Every concurrent injector (batch runs, or the
GUI plus a batch) wrote the SAME filenames. Whichever process finished
FIRST deleted the others' in-flight files in its end-of-run cleanup —
so a still-running injector either crashed reading its now-deleted temp
(→ NORPPG) or finalized a half-clobbered temp into a structurally broken
``-rppg.mp4`` (→ ffprobe quarantine → NORPPG).

The snapshot file was ALREADY namespaced per-process+per-input
(``best_iteration_snapshot_<PID>_<hash>.mp4``); the numbered temps were
the remaining hole. The fix namespaces BOTH off one ``_run_token`` and
makes the two previously-fatal "best file missing" branches degrade
(skip the iteration, keep going) instead of aborting the run.

Because the naming lives inside the giant ``iterative_enhancement``
method (needs the full mediapipe + opencv stack to instantiate), these
are source-pins on the injector — a refactor that reintroduces a bare
``temp_iteration_<N>.mp4`` or a hard ``raise`` on the missing-file path
fails here.
"""

from pathlib import Path
import re


def _injector_src() -> str:
    return (Path(__file__).resolve().parent.parent
            / "rPPG" / "rppg_injector.py").read_text(encoding="utf-8")


def test_run_token_is_per_process_and_per_input():
    """The per-run token must include the PID (and a per-input hash) so
    two concurrent injectors never share a temp namespace."""
    src = _injector_src()
    assert "_run_token = f\"{os.getpid()}_{_video_id:06x}\"" in src, (
        "the per-run token must combine os.getpid() with the per-input "
        "video hash so concurrent injectors get distinct temp namespaces"
    )


def test_temp_iteration_files_are_namespaced():
    """No bare ``temp_iteration_<N>.mp4`` may be CONSTRUCTED (f-string)
    for a write target — it must carry the per-run token. Comments and
    log strings referencing the old name for documentation are fine; an
    actual assignment is not."""
    src = _injector_src()
    # The fix: the write target is f"temp_iteration_{_run_token}_{iteration}.mp4".
    assert 'f"temp_iteration_{_run_token}_{iteration}.mp4"' in src, (
        "temp_iteration write target must be namespaced by _run_token"
    )
    # Guard against a bare-name regression: no f-string that builds
    # temp_iteration_<something>.mp4 WITHOUT _run_token, on a code line.
    bad = []
    for ln in src.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        # find f-string constructions of temp_iteration_*.mp4
        if re.search(r'f"temp_iteration_\{', ln) and "_run_token" not in ln:
            bad.append(ln.strip())
    assert not bad, f"bare temp_iteration f-string(s) reintroduced: {bad}"


def test_snapshot_name_uses_run_token():
    src = _injector_src()
    assert 'f"best_iteration_snapshot_{_run_token}.mp4"' in src, (
        "the best-iteration snapshot must share the same per-run token"
    )


def test_missing_best_file_paths_self_heal_not_raise():
    """The two 'best file missing and no prior snapshot' branches must
    NOT hard-raise FileNotFoundError (which aborted the whole injection
    → NORPPG). They must degrade. We assert no FileNotFoundError is
    raised from the iterative loop's snapshot-promotion block."""
    src = _injector_src()
    # The old hard-fail messages must be gone.
    assert "Best iteration output missing and no " not in src, (
        "the inner missing-file hard-raise must be replaced with a "
        "graceful skip-promotion (self-heal)"
    )
    assert "Best iteration output missing after copy " not in src, (
        "the OSError-path missing-file hard-raise must be replaced with "
        "a graceful skip-promotion (self-heal)"
    )
    # And the self-heal warnings must be present.
    assert "skipping promotion for this iter and " in src


def test_end_of_loop_fallback_checks_file_exists():
    """The best_path fallback must verify on-disk existence and walk
    back to a real file, so the finalize copy never hits a missing
    path."""
    src = _injector_src()
    assert "best_path is None or not os.path.exists(best_path)" in src, (
        "end-of-loop fallback must handle BOTH None and a committed "
        "best_path that no longer exists on disk"
    )


def test_recovered_iter_metrics_are_adopted_unconditionally():
    """code-review + Gemini HIGH on PR #89: when the fallback recovers a
    DIFFERENT iter than the (now-missing) committed best, best_metrics
    must be set to the recovered iter's metrics UNCONDITIONALLY — not
    gated on ``best_metrics is None or == baseline``. Otherwise the
    delivered file gets the wrong metric suffix + wrong returned
    metadata. Guard against the buggy conditional creeping back."""
    src = _injector_src()
    # The unconditional assignment must be present right after best_iter.
    assert "best_metrics = dict(iter_files[recovered[1]]['metrics'])" in src
    # And the buggy guard must NOT gate that assignment anymore.
    assert "if best_metrics is None or best_metrics == dict(baseline):" not in src, (
        "the recovered-iter metric adoption must be unconditional; the "
        "None-or-baseline guard missed the committed-then-vanished case"
    )


def test_unpulsed_fallback_stamps_norppg_into_stem():
    """Codex PR #91 HIGH: when the end-of-loop self-heal exhausts every
    on-disk iteration and falls back to the UN-PULSED source, the delivered
    file must NOT be selected as the rPPG deliverable — otherwise it ships a
    pulse-free video that passes the decode-only gate with no -NORPPG marker
    (silent no-pulse delivery). The fix sets ``delivered_unpulsed`` and stamps
    ``-NORPPG`` INTO the output stem before ``add_metric_suffix``. Because
    ``automation.rppg.resolve_produced_output`` globs ``{stem} - *``, the
    resulting ``{stem}-NORPPG - <metrics>{ext}`` deliberately won't match ->
    the resolver returns None -> the caller marks the pre-rPPG video -NORPPG."""
    src = _injector_src()
    # The flag is initialised, set on the un-pulsed fallback, and consumed.
    assert "delivered_unpulsed = False" in src
    assert "delivered_unpulsed = True" in src
    assert "if delivered_unpulsed:" in src
    # The stem stamp uses the same terminal-token idempotency as the queue's
    # _mark_norppg, and feeds add_metric_suffix via metric_source_path.
    assert 'f"{_base}-NORPPG{_ext}"' in src, (
        "the un-pulsed deliverable must carry a -NORPPG stem stamp"
    )
    assert "add_metric_suffix(metric_source_path" in src, (
        "the metric suffix must be built from the (possibly -NORPPG-stamped) "
        "stem, not the raw output_path"
    )


def test_norppg_stamped_stem_is_missed_by_resolver_glob():
    """Behavioral guard for the contract the fix relies on: a
    ``{stem}-NORPPG - <metrics>{ext}`` file must NOT match the
    ``{stem} - *{ext}`` glob that ``resolve_produced_output`` uses to find the
    deliverable. If a future refactor made the glob looser, the un-pulsed file
    would be re-selected and the silent-no-pulse bug would reopen."""
    import fnmatch

    stem = "clip-rppg"
    ext = ".mp4"
    resolver_pattern = f"{stem} - *{ext}"  # mirrors automation/rppg.py

    pulsed = f"{stem} - 11.19-3.3-0.57-0.00-0.85{ext}"
    unpulsed = f"{stem}-NORPPG - 0.00-0.00-0.00-0.00-0.00{ext}"

    assert fnmatch.fnmatch(pulsed, resolver_pattern), (
        "a normal pulsed deliverable must still match the resolver glob"
    )
    assert not fnmatch.fnmatch(unpulsed, resolver_pattern), (
        "the -NORPPG-stamped un-pulsed file must NOT match -> resolver returns "
        "None -> caller marks the pre-rPPG video -NORPPG"
    )


def test_iteration_history_json_is_namespaced_by_run_token():
    """v2.29 follow-up to PR #89: PR #89 namespaced the temp mp4s + snapshot
    by ``_run_token`` but left the iteration-history JSON keyed on only
    ``{stem}_{stamp}`` (output stem + wall-clock SECOND). Two concurrent
    injectors processing the same input within the same second wrote to an
    IDENTICAL history path and silently overwrote each other's JSON. The
    history filename must carry ``_run_token`` like the temps do; the paired
    ``_metrics_summary.tsv`` (derived via ``.replace``) then inherits it."""
    src = _injector_src()
    assert (
        "f'{stem}_{stamp}_{_run_token}_iteration_history.json'" in src
        or 'f"{stem}_{stamp}_{_run_token}_iteration_history.json"' in src
    ), "the iteration-history JSON path must include _run_token"
    # Guard against the bare, collision-prone name creeping back.
    bad = []
    for ln in src.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        if "_iteration_history.json" in ln and "history_dir" in ln:
            # This is the path-construction line; it MUST carry _run_token.
            if "_run_token" not in ln:
                bad.append(ln.strip())
    assert not bad, f"bare iteration-history path (no _run_token): {bad}"
