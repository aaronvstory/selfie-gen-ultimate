import types
import unittest
from unittest import mock

import dependency_health_check as dhc


def _healthy_module_set():
    """Module stubs for the happy-path probe — full v2.17 runtime set.

    Shared by both DependencyHealthCheckTests and TorchCudaFallbackTests.
    Includes scipy/absl/mediapipe (+ the Tasks-API FaceLandmarker symbol) so a
    test that overrides only torch still passes the now-fuller probe.
    """
    return {
        "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
        "tensorflow.compat.v2": types.SimpleNamespace(),
        "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
        "retinaface": types.SimpleNamespace(RetinaFace=object()),
        "cv2": types.SimpleNamespace(),
        "numpy": types.SimpleNamespace(),
        "torch": types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
        ),
        "scipy": types.SimpleNamespace(),
        "absl": types.SimpleNamespace(),
        "mediapipe": types.SimpleNamespace(),
        "mediapipe.tasks.python.vision": types.SimpleNamespace(
            FaceLandmarker=object()
        ),
    }


def _classify_repair_call(cmd):
    """Classify a mocked ``subprocess.run`` pip command from run_repair into:
    'cpu_fallback' (torch CPU index), 'mediapipe' (--no-deps mediapipe step),
    'mediapipe_runtime' (the v2.17 matplotlib/opencv-contrib/sounddevice step
    that fixes the recurring Windows rPPG failure), or 'face_stack_repair'
    (the REPAIR_PACKAGES install).

    Order of checks matters: mediapipe_runtime + face_stack are both non-index,
    non--no-deps installs, so the matplotlib marker must be checked first.
    """
    if "--index-url" in cmd and dhc._TORCH_CPU_INDEX_URL in cmd:
        return "cpu_fallback"
    if "--no-deps" in cmd and any("mediapipe" in str(a) for a in cmd):
        return "mediapipe"
    if any("matplotlib" in str(a) for a in cmd):
        return "mediapipe_runtime"
    return "face_stack_repair"


class DependencyHealthCheckTests(unittest.TestCase):
    def test_fails_for_broken_tensorflow_namespace(self):
        tf_module = types.SimpleNamespace()
        tf_keras_module = types.SimpleNamespace(__version__="2.16.0")
        retinaface_module = types.SimpleNamespace()
        cv2_module = types.SimpleNamespace()
        numpy_module = types.SimpleNamespace()

        modules = {
            "tensorflow": tf_module,
            "tf_keras": tf_keras_module,
            "retinaface": types.SimpleNamespace(RetinaFace=retinaface_module),
            "cv2": cv2_module,
            "numpy": numpy_module,
        }

        def fake_importer(name: str):
            if name == "tensorflow.compat.v2":
                raise ModuleNotFoundError("tensorflow.compat.v2")
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertFalse(ok)
        combined = "\n".join(failures)
        self.assertIn("tensorflow missing __version__", combined)
        self.assertIn("tensorflow.compat.v2 import failed", combined)

    def _healthy_module_set(self):
        """Instance wrapper around the module-level helper (shared with
        TorchCudaFallbackTests). See _healthy_module_set() at module scope."""
        return _healthy_module_set()

    def test_passes_for_valid_import_set(self):
        modules = self._healthy_module_set()

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertTrue(ok, failures)
        self.assertEqual(failures, [])

    def test_fails_for_retinaface_runtime_probe_incompatibility(self):
        modules = self._healthy_module_set()

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (None, "RuntimeError: A KerasTensor cannot be used"),
        )
        self.assertFalse(ok)
        self.assertIn("retinaface runtime loader failed", "\n".join(failures))

    def test_verify_in_fresh_process_parses_failures(self):
        completed = types.SimpleNamespace(
            returncode=1,
            stdout="[dep-health] FAILED\n[dep-health] tensorflow missing __version__\n",
            stderr="",
        )
        with mock.patch("dependency_health_check.subprocess.run", return_value=completed):
            ok, failures = dhc.verify_in_fresh_process()

        self.assertFalse(ok)
        self.assertIn("tensorflow missing __version__", failures)

    def test_repair_mode_returns_zero_on_partial_success_face_stack_healthy(self):
        """Subagent PR #55 round 5 HIGH: when ``run_repair`` returns False
        because the CPU-torch fallback couldn't reach download.pytorch.org
        BUT the face-stack repair succeeded, ``main`` must still return 0
        so the launcher proceeds with GUI launch — face_crop / video paths
        work fine on the now-healthy face stack, and the app doesn't use
        torch.cuda.* in production. The previous shape collapsed any
        repair failure to exit 1, denying GUI launch unnecessarily.

        Round 8 update (Gemini #PRRT_kwDOSQUnmM6FQaDB): ``--mode check``
        now exits 0 on CUDA-only failure (``_is_cuda_only_failure``), so
        ``verify_in_fresh_process`` returns ``(True, [])`` when running
        that subprocess in this scenario. Updated the mock to reflect
        the real subprocess behavior — the previous mock returned
        ``(False, [...])`` which is no longer reachable in production.
        """
        # Initial check: torch CUDA failure + tf import failure
        initial = (
            False,
            ["torch_cuda_failure:cudart: ImportError", "tensorflow import failed: foo"],
        )
        # Repair returns False (CPU fallback failed) but messages capture
        # face-stack repair succeeded.
        repair_result = (
            False,
            "torch CPU fallback failed (code 1): network error; repair install completed",
        )
        # Fresh-process verify: ``--mode check`` subprocess exits 0 with
        # WARN lines (CUDA-only failure now tolerated at check time).
        # ``verify_in_fresh_process`` returns ``(True, [])`` for an
        # exit-0 subprocess regardless of WARN content.
        verify_result = (True, [])

        with mock.patch(
            "dependency_health_check.check_runtime_dependencies",
            return_value=initial,
        ), mock.patch(
            "dependency_health_check.run_repair",
            return_value=repair_result,
        ), mock.patch(
            "dependency_health_check.verify_in_fresh_process",
            return_value=verify_result,
        ):
            exit_code = dhc.main(["--mode", "repair"])

        self.assertEqual(exit_code, 0, "Partial success (face stack OK) must exit 0")

    def test_repair_mode_returns_one_when_non_cuda_failures_remain(self):
        """Counterpart to the partial-success case: if the fresh verify
        still shows TF/retinaface failures (i.e. face stack is broken),
        ``main`` must return 1 — those failures mean the GUI's face_crop
        tab cannot work, and the launcher SHOULD abort to surface the
        actionable error."""
        initial = (False, ["tensorflow import failed: foo"])
        repair_result = (False, "repair failed (code 1): pip resolution conflict")
        verify_result = (False, ["tensorflow import failed: still broken"])

        with mock.patch(
            "dependency_health_check.check_runtime_dependencies",
            return_value=initial,
        ), mock.patch(
            "dependency_health_check.run_repair",
            return_value=repair_result,
        ), mock.patch(
            "dependency_health_check.verify_in_fresh_process",
            return_value=verify_result,
        ):
            exit_code = dhc.main(["--mode", "repair"])

        self.assertEqual(exit_code, 1, "Non-CUDA failures remaining must exit 1")

    def test_repair_mode_uses_fresh_process_verification(self):
        with mock.patch(
            "dependency_health_check.check_runtime_dependencies",
            return_value=(False, ["tensorflow missing __version__"]),
        ), mock.patch(
            "dependency_health_check.run_repair",
            return_value=(True, "repair install completed"),
        ), mock.patch(
            "dependency_health_check.verify_in_fresh_process",
            return_value=(True, []),
        ) as mock_verify:
            exit_code = dhc.main(["--mode", "repair"])

        self.assertEqual(exit_code, 0)
        mock_verify.assert_called_once()


class TorchCudaFallbackTests(unittest.TestCase):
    """Coverage for the CPU-only-torch fallback path added for Windows nvidia
    boxes whose CUDA wheels fail to load (missing cudart64_*.dll, mismatched
    driver, etc). No production code uses torch.cuda.*, so CPU-only torch
    is functionally equivalent and the user gets a working app instead of
    the launcher dead-ending.
    """

    def test_signature_match_walks_exception_chain(self):
        """``_torch_cuda_load_failure_signature`` must walk ``__cause__`` so
        a CUDA failure deep inside ``torch._C`` is still caught even when
        the user-facing exception type is a plain ``ImportError``."""
        try:
            try:
                raise OSError(
                    "[WinError 126] cudart64_110.dll: The specified module could not be found."
                )
            except OSError as inner:
                raise ImportError("DLL load failed while importing _C") from inner
        except ImportError as exc:
            sig = dhc._torch_cuda_load_failure_signature(exc)

        self.assertEqual(sig, "cudart")

    def test_signature_match_returns_empty_for_unrelated_failures(self):
        try:
            raise ImportError("module 'protobuf' has no attribute 'BoltOnLoaded'")
        except ImportError as exc:
            sig = dhc._torch_cuda_load_failure_signature(exc)

        self.assertEqual(sig, "")

    def test_check_classifies_torch_cuda_failure_with_prefix(self):
        """A torch import that fails with a CUDA signature lands in failures
        as ``torch_cuda_failure:<sig>: ...``. This is the key signal
        ``run_repair`` reads to trigger the CPU fallback before the face
        stack reinstall."""
        modules = {
            "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
            "tensorflow.compat.v2": types.SimpleNamespace(),
            "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
            "retinaface": types.SimpleNamespace(RetinaFace=object()),
            "cv2": types.SimpleNamespace(),
            "numpy": types.SimpleNamespace(),
        }

        def fake_importer(name: str):
            if name == "torch":
                raise ImportError("DLL load failed: cudart64_110.dll missing")
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertFalse(ok)
        combined = "\n".join(failures)
        self.assertIn("torch_cuda_failure:cudart", combined, combined)
        self.assertTrue(dhc._failures_indicate_torch_cuda_break(failures))

    def test_check_classifies_eager_cuda_init_failure(self):
        """A torch import that succeeds but whose ``torch.cuda.is_available()``
        triggers a CUDA error must land as ``torch_cuda_failure:`` —
        production torch defers CUDA-runtime DLL load until first
        ``torch.cuda.*`` call (subagent PR #55 round 5 MED catch — the
        previous probe ``torch.zeros(1)`` defaulted to ``device='cpu'``
        and would have missed this entirely)."""

        def bad_is_available():
            raise RuntimeError("CUDA runtime error: device kernel image is invalid")

        modules = {
            "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
            "tensorflow.compat.v2": types.SimpleNamespace(),
            "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
            "retinaface": types.SimpleNamespace(RetinaFace=object()),
            "cv2": types.SimpleNamespace(),
            "numpy": types.SimpleNamespace(),
            "torch": types.SimpleNamespace(
                cuda=types.SimpleNamespace(is_available=bad_is_available),
            ),
        }

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertFalse(ok)
        combined = "\n".join(failures)
        self.assertIn("torch_cuda_failure:cuda runtime", combined, combined)

    def test_check_classifies_cuda_build_runtime_mismatch(self):
        """CodeRabbit PR #55 round 8 Major: a torch wheel built with CUDA
        support (``torch.version.cuda`` non-None) but whose runtime
        ``cuda.is_available()`` returns False is the dominant Windows
        nvidia failure mode — broken cudart DLLs, driver mismatch, AV
        quarantine. The previous probe missed this entirely because it
        ignored the return value of ``is_available()`` and only caught
        EXCEPTIONS.

        The probe must classify this as ``torch_cuda_failure:
        build_runtime_mismatch:`` so ``run_repair`` triggers the CPU
        fallback (which is the correct recovery — CPU torch is
        functionally equivalent for this app's production code).
        """
        modules = {
            "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
            "tensorflow.compat.v2": types.SimpleNamespace(),
            "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
            "retinaface": types.SimpleNamespace(RetinaFace=object()),
            "cv2": types.SimpleNamespace(),
            "numpy": types.SimpleNamespace(),
            # CUDA-built torch wheel (version.cuda is a string like "12.1")
            # whose runtime can't actually load CUDA libs (is_available=False).
            "torch": types.SimpleNamespace(
                cuda=types.SimpleNamespace(is_available=lambda: False),
                version=types.SimpleNamespace(cuda="12.1"),
            ),
        }

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertFalse(ok, failures)
        combined = "\n".join(failures)
        self.assertIn("torch_cuda_failure:build_runtime_mismatch", combined, combined)
        self.assertIn("CUDA 12.1", combined, "Build CUDA version must be in message")
        self.assertTrue(
            dhc._failures_indicate_torch_cuda_break(failures),
            "CPU fallback must trigger for build/runtime mismatch",
        )

    def test_check_does_not_flag_cpu_only_torch_as_cuda_failure(self):
        """Counterpart sanity check: a CPU-only torch wheel (``torch.version
        .cuda`` is None) returning ``is_available() == False`` is EXPECTED
        and must NOT be classified as a CUDA failure. The healthy stub set
        (``_healthy_module_set`` above) provides exactly this shape and
        passes the probe — but pin it explicitly to prevent a future edit
        from regressing the distinction.
        """
        # Start from the full healthy set (incl. v2.17 scipy/absl/mediapipe)
        # and override torch with the explicit CPU-only shape under test.
        modules = _healthy_module_set()
        modules["torch"] = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            version=types.SimpleNamespace(cuda=None),  # CPU-only torch
        )

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertTrue(ok, f"CPU-only torch must pass; got failures: {failures}")
        self.assertEqual(failures, [])

    def test_check_tolerates_torch_without_cuda_attribute(self):
        """Some torch builds (CPU-only nightlies on certain platforms) ship
        without a ``torch.cuda`` submodule. The probe must NOT crash on
        ``AttributeError: module has no attribute 'cuda'`` — it should
        just skip the eager probe (no failure to surface)."""
        modules = _healthy_module_set()
        modules["torch"] = types.SimpleNamespace()  # no .cuda attribute

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertTrue(ok, failures)
        self.assertEqual(failures, [])

    def test_check_classifies_plain_torch_import_failure_distinct_from_cuda(self):
        """A torch import that fails with NO CUDA signature should land as
        ``torch import failed`` and NOT trigger the CPU fallback (because
        the fix would be a different one — reinstall torch from PyPI, not
        from the CPU wheel index)."""
        modules = {
            "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
            "tensorflow.compat.v2": types.SimpleNamespace(),
            "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
            "retinaface": types.SimpleNamespace(RetinaFace=object()),
            "cv2": types.SimpleNamespace(),
            "numpy": types.SimpleNamespace(),
        }

        def fake_importer(name: str):
            if name == "torch":
                raise ImportError("DLL load failed: torch._C cannot find _torch.dll")
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=lambda: (object(), ""),
        )
        self.assertFalse(ok)
        self.assertFalse(dhc._failures_indicate_torch_cuda_break(failures))
        self.assertIn("torch import failed", "\n".join(failures))

    def test_run_repair_triggers_torch_cpu_fallback_first(self):
        """When failures contain a CUDA signature, ``run_repair`` must call
        the CPU-only torch reinstall BEFORE the face-stack repair — pulling
        TF wheels into a broken CUDA torch env is wasted work."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            # First call: torch CPU fallback (recognizable by the --index-url
            # arg + just `torch` as the package). Second call: face stack
            # repair (REPAIR_PACKAGES). Third (v2.17): mediapipe --no-deps.
            call_order.append(_classify_repair_call(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError: DLL load failed"]
            )

        self.assertTrue(ok, message)
        self.assertEqual(
            call_order,
            ["cpu_fallback", "face_stack_repair", "mediapipe", "mediapipe_runtime"],
        )
        self.assertIn("CPU-only fallback", message)

    def test_run_repair_skips_cpu_fallback_when_no_cuda_signature(self):
        """No CUDA failure in the failures list -> only the face-stack
        repair runs. CPU fallback is wasted work + a network round trip
        otherwise."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            call_order.append(_classify_repair_call(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, _ = dhc.run_repair(failures=["tensorflow missing __version__"])

        self.assertTrue(ok)
        self.assertEqual(call_order, ["face_stack_repair", "mediapipe", "mediapipe_runtime"])

    def test_run_repair_back_compat_no_failures_arg(self):
        """``run_repair()`` with no args (back-compat for external callers)
        runs just the face-stack repair, no CPU fallback."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            call_order.append(_classify_repair_call(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, _ = dhc.run_repair()  # no failures arg

        self.assertTrue(ok)
        self.assertEqual(call_order, ["face_stack_repair", "mediapipe", "mediapipe_runtime"])

    def test_run_repair_continues_face_stack_when_cpu_fallback_fails(self):
        """Codex PR #55 round 4 P2: if the CPU fallback fails (e.g.
        download.pytorch.org blocked or flaky), the face-stack repair
        MUST still run. The face stack is independently repairable; the
        user benefits from a working face_crop / video path even if
        torch stays broken. ``run_repair`` returns False overall only
        because CUDA fallback failed, but the message captures BOTH
        outcomes so the launcher's diagnostic log is clear."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            kind = _classify_repair_call(cmd)
            call_order.append(kind)
            if kind == "cpu_fallback":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ERROR: Could not find a version that satisfies torch==999",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError"]
            )

        # Overall NOT ok because cuda_ok was False, but face_stack DID run
        # (and mediapipe too, both independently repairable).
        self.assertFalse(ok)
        self.assertEqual(
            call_order,
            ["cpu_fallback", "face_stack_repair", "mediapipe", "mediapipe_runtime"],
        )
        self.assertIn("torch CPU fallback failed", message)
        self.assertIn("repair install completed", message)

    def test_run_repair_succeeds_only_when_both_paths_succeed(self):
        """Sanity guard: with a CUDA failure on input, the overall ``ok``
        return value is the AND of (cpu_fallback_ok, face_stack_ok). This
        prevents accidental future change that returns True when only one
        path succeeded — the launcher needs both for a fully-recovered env.
        """
        def all_ok(cmd, *args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=all_ok):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError"]
            )
        self.assertTrue(ok)
        self.assertIn("CPU-only fallback", message)
        self.assertIn("repair install completed", message)

        # Now face-stack fails; cuda fallback ok. Overall should be False.
        # v2.17: run_repair now makes FOUR pip calls (cpu fallback, face stack,
        # mediapipe --no-deps, mediapipe runtime deps) — seq supplies each.
        seq = [
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),  # cuda ok
            types.SimpleNamespace(returncode=1, stdout="", stderr="pip resolution conflict"),  # face fails
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),  # mediapipe --no-deps ok
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),  # mediapipe runtime deps ok
        ]
        with mock.patch("dependency_health_check.subprocess.run", side_effect=seq):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError"]
            )
        self.assertFalse(ok)
        self.assertIn("CPU-only fallback", message)
        self.assertIn("repair failed", message)

    def test_extract_pip_failure_detail_prefers_ERROR_over_trailing_warning(self):
        """Gemini PR #55 round 5 MED: pip's output often ends with a warning
        line (e.g. ``WARNING: You are using pip version X``). Naively
        ``splitlines()[-1]`` would surface that warning as the failure
        reason, masking the actual ``ERROR:`` line. ``_extract_pip_failure_
        detail`` must scan for ``ERROR:`` first.
        """
        completed = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Collecting torch\n"
                "ERROR: Could not find a version that satisfies the requirement torch==999\n"
                "ERROR: No matching distribution found for torch==999\n"
                "WARNING: You are using pip version 24.0; you should consider upgrading.\n"
            ),
        )
        detail = dhc._extract_pip_failure_detail(completed)
        self.assertTrue(
            detail.startswith("ERROR:"),
            f"Expected first ERROR: line, got {detail!r}",
        )
        self.assertIn("Could not find a version", detail)

    def test_extract_pip_failure_detail_falls_back_to_last_line_when_no_ERROR(self):
        """When pip output has no ``ERROR:`` prefix (e.g. raw subprocess
        crash), fall back to the last non-empty line of stderr/stdout."""
        completed = types.SimpleNamespace(
            returncode=1,
            stdout="Some progress chatter\nFinal status: failed for unknown reason",
            stderr="",
        )
        detail = dhc._extract_pip_failure_detail(completed)
        self.assertEqual(detail, "Final status: failed for unknown reason")

    def test_extract_pip_failure_detail_prefers_stderr_when_both_present(self):
        """Stderr is checked before stdout for ERROR: lines. If neither has
        an ERROR: line, the last stderr line is preferred over stdout."""
        completed = types.SimpleNamespace(
            returncode=1,
            stdout="some stdout chatter\nlast stdout line",
            stderr="ERROR: real pip error",
        )
        detail = dhc._extract_pip_failure_detail(completed)
        self.assertEqual(detail, "ERROR: real pip error")

    def test_extract_pip_failure_detail_empty_when_no_output(self):
        """No output at all returns empty string (e.g. SIGKILL'd subprocess)."""
        completed = types.SimpleNamespace(returncode=1, stdout="", stderr="")
        detail = dhc._extract_pip_failure_detail(completed)
        self.assertEqual(detail, "")

    def test_torch_cpu_fallback_uses_extra_index_url_for_pypi(self):
        """Gemini PR #55 round 4 HIGH: ``--index-url`` alone restricts pip
        to ONLY the PyTorch CPU wheel index, which doesn't host torch's
        runtime deps (filelock, sympy, networkx, jinja2, etc). Must add
        ``--extra-index-url https://pypi.org/simple`` so pip falls back
        to PyPI for those non-PyTorch packages.
        """
        captured_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, _ = dhc.run_torch_cpu_fallback()

        self.assertTrue(ok)
        self.assertEqual(len(captured_cmds), 1)
        cmd = captured_cmds[0]
        # Subagent round 8 MED: don't pin position. Check the URL-flag
        # PAIRS are intact via membership + neighbor lookup so reordering
        # other args (e.g. prepending a corporate mirror) doesn't break
        # the test. Both URL flags MUST appear with their values in
        # the correct neighbor slot.
        self.assertIn("--index-url", cmd)
        iu_value = cmd[cmd.index("--index-url") + 1]
        self.assertEqual(iu_value, dhc._TORCH_CPU_INDEX_URL)
        self.assertIn("--extra-index-url", cmd)
        eiu_value = cmd[cmd.index("--extra-index-url") + 1]
        self.assertEqual(eiu_value, "https://pypi.org/simple")
        # `torch` must appear as a package arg — either the bare name
        # (if version-probe couldn't read metadata) or `torch==X.Y.Z`
        # (Gemini PR #55 round-2 MED #3313903515: pin to currently-
        # installed version to avoid silent upgrade drift). The exact
        # form depends on whether torch metadata is readable in the
        # test process; both are valid.
        torch_pkg_args = [a for a in cmd if a == "torch" or a.startswith("torch==")]
        self.assertEqual(
            len(torch_pkg_args),
            1,
            f"Expected exactly one `torch` or `torch==X.Y.Z` arg, got {cmd!r}",
        )

    def test_torch_cpu_fallback_pins_to_installed_version(self):
        """Gemini PR #55 round-2 MED (#3313903515): pin the CPU fallback
        reinstall to the currently-installed torch version so the CPU
        fallback doesn't silently upgrade torch to whatever the wheel
        index advertises. The version is probed via
        ``importlib.metadata.version`` (NOT ``import torch``) because
        the whole point of the fallback is that ``import torch`` may be
        broken when this code runs.
        """
        captured_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch(
            "dependency_health_check._installed_torch_version",
            return_value="2.16.99",
        ), mock.patch(
            "dependency_health_check.subprocess.run", side_effect=fake_run
        ):
            ok, _ = dhc.run_torch_cpu_fallback()

        self.assertTrue(ok)
        cmd = captured_cmds[0]
        self.assertIn("torch==2.16.99", cmd)
        # And NO bare "torch" — the pinned form replaces it, not adds to.
        self.assertNotIn("torch", cmd)

    def test_torch_cpu_fallback_falls_back_to_bare_torch_when_probe_fails(self):
        """If ``_installed_torch_version`` returns None (metadata
        unreadable — torch's dist-info was deleted, weird half-install,
        etc), the fallback uses the bare ``torch`` arg. Better an
        upgraded install than no install.
        """
        captured_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch(
            "dependency_health_check._installed_torch_version",
            return_value=None,
        ), mock.patch(
            "dependency_health_check.subprocess.run", side_effect=fake_run
        ):
            ok, _ = dhc.run_torch_cpu_fallback()

        self.assertTrue(ok)
        cmd = captured_cmds[0]
        self.assertIn("torch", cmd)
        # And no torch==... pin (the probe failed; bare is the fallback).
        pinned = [a for a in cmd if a.startswith("torch==")]
        self.assertEqual(pinned, [])

    def test_installed_torch_version_strips_local_version_identifier(self):
        """Gemini PR #55 round-2 HIGH + Codex P2: on Windows nvidia,
        the broken CUDA torch reports e.g. ``2.5.1+cu121`` via
        ``importlib.metadata.version``. If we pin the CPU fallback
        reinstall to that exact string, pip fails because the CPU wheel
        index doesn't host ``+cu121`` wheels — defeating the entire
        CPU-fallback purpose in exactly the scenario it's meant to fix.

        ``_installed_torch_version`` must strip the PEP 440 local
        version identifier (anything from ``+`` onwards) so we pin to
        the PUBLIC base version, which IS available on the CPU index.
        """
        # Common Windows nvidia case
        with mock.patch(
            "dependency_health_check.importlib.metadata.version",
            return_value="2.5.1+cu121",
        ):
            self.assertEqual(dhc._installed_torch_version(), "2.5.1")
        # Other CUDA suffixes
        with mock.patch(
            "dependency_health_check.importlib.metadata.version",
            return_value="2.1.2+cu118",
        ):
            self.assertEqual(dhc._installed_torch_version(), "2.1.2")
        # +cpu suffix (already CPU build — strip anyway, public version
        # is what pinned on CPU index)
        with mock.patch(
            "dependency_health_check.importlib.metadata.version",
            return_value="2.5.1+cpu",
        ):
            self.assertEqual(dhc._installed_torch_version(), "2.5.1")
        # Plain public version — unchanged
        with mock.patch(
            "dependency_health_check.importlib.metadata.version",
            return_value="2.12.0",
        ):
            self.assertEqual(dhc._installed_torch_version(), "2.12.0")

    def test_check_mode_exits_zero_on_cuda_only_failure(self):
        """Codex PR #55 round-7 P2 (#PRRT_kwDOSQUnmM6FQIkt): when
        ``check_runtime_dependencies`` returns ONLY ``torch_cuda_failure:*``
        entries, ``main(['--mode', 'check'])`` must exit 0 with WARN
        lines — NOT exit 1. The previous form returned 1 here, which
        made both launchers re-enter ``--mode repair`` on EVERY launch
        for users whose CPU-torch fallback couldn't reach
        download.pytorch.org. The wasteful pip-install churn (~50MB+)
        repeated on every launch with no forward progress.

        Re-detection still works: a CUDA-fix on the next launch returns
        clean ``[dep-health] OK``; a NEW non-CUDA failure correctly
        flips the gate back to exit 1.
        """
        cuda_only = (False, ["torch_cuda_failure:build_runtime_mismatch: ..."])

        with mock.patch(
            "dependency_health_check.check_runtime_dependencies",
            return_value=cuda_only,
        ):
            with mock.patch("builtins.print") as mock_print:
                exit_code = dhc.main(["--mode", "check"])

        self.assertEqual(
            exit_code, 0,
            "CUDA-only failure must exit 0 on --mode check (otherwise "
            "every launch re-triggers wasteful face-stack repair)."
        )
        # WARN lines must be printed — the launcher's log captures them
        # so the persistent broken-CUDA state stays visible to debugging.
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("WARN", printed)
        self.assertIn("torch_cuda_failure", printed)
        # And the bare "FAILED" sentinel must NOT print (that would
        # confuse the launcher's log parsing).
        self.assertNotIn("[dep-health] FAILED", printed)

    def test_check_mode_still_exits_one_on_non_cuda_failure(self):
        """Counterpart sanity check: any non-CUDA failure (e.g. broken
        TF wheel) MUST still exit 1, even if a CUDA failure is also
        present. The partial-success exit-0 is scoped to CUDA-ONLY.
        """
        mixed = (
            False,
            [
                "tensorflow import failed: ModuleNotFoundError",
                "torch_cuda_failure:cudart: DLL load failed",
            ],
        )

        with mock.patch(
            "dependency_health_check.check_runtime_dependencies",
            return_value=mixed,
        ):
            exit_code = dhc.main(["--mode", "check"])

        self.assertEqual(
            exit_code, 1,
            "Mixed failures (CUDA + non-CUDA) must exit 1 — the partial-"
            "success exit-0 is scoped to CUDA-ONLY-failures."
        )

    def test_is_cuda_only_failure_helper(self):
        """Direct unit test of the classification helper."""
        # Empty list — not cuda-only (no failures at all)
        self.assertFalse(dhc._is_cuda_only_failure([]))
        # Single CUDA failure — cuda-only
        self.assertTrue(dhc._is_cuda_only_failure(
            ["torch_cuda_failure:cudart: ImportError"]
        ))
        # Multiple CUDA failures — cuda-only
        self.assertTrue(dhc._is_cuda_only_failure([
            "torch_cuda_failure:build_runtime_mismatch: ...",
            "torch_cuda_failure:cudart: another DLL load",
        ]))
        # Mixed — NOT cuda-only
        self.assertFalse(dhc._is_cuda_only_failure([
            "torch_cuda_failure:cudart: foo",
            "tensorflow import failed: bar",
        ]))
        # Non-CUDA only — NOT cuda-only
        self.assertFalse(dhc._is_cuda_only_failure([
            "tensorflow import failed: bar",
        ]))

    def test_check_runtime_probe_runs_under_torch_cuda_failure(self):
        """Codex PR #55 round-6 P2 (#PRRT_kwDOSQUnmM6FPwqp): the
        RetinaFace runtime probe must run whenever NON-CUDA failures
        are clear — including when a `torch_cuda_failure:*` is present.
        That class of failure is explicitly tolerated by main()'s
        partial-success exit-0 path (GUI launchable on CPU torch), so
        it must NOT suppress the runtime probe that catches
        TensorFlow/Keras/RetinaFace loader breakage.

        Combined-failure scenario: broken CUDA torch + broken RetinaFace
        loader. The previous gate `if not failures` skipped the
        RetinaFace probe entirely; main()'s partial-success path then
        exited 0 ("face stack healthy") without ever validating the
        face stack actually works. Launcher would then open the GUI
        into the very Face Crop broken state this PR is meant to fix.

        Fixed by filtering `torch_cuda_failure:*` BEFORE the runtime-
        probe gate. The RetinaFace probe now runs under CUDA-only
        failure, and its failures get reported as expected.
        """
        modules = _healthy_module_set()
        # CUDA-built torch wheel with broken runtime — classified as
        # torch_cuda_failure:build_runtime_mismatch
        modules["torch"] = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            version=types.SimpleNamespace(cuda="12.1"),
        )

        def fake_importer(name: str):
            if name in modules:
                return modules[name]
            raise ModuleNotFoundError(name)

        # RetinaFace runtime probe that FAILS (simulating broken loader).
        # The bug was that this fake would never be CALLED under the old
        # gating. We track invocation explicitly.
        probe_called = []

        def failing_probe():
            probe_called.append(True)
            return (None, "TF DLL load failure during retinaface init")

        ok, failures = dhc.check_runtime_dependencies(
            importer=fake_importer,
            runtime_probe=failing_probe,
        )

        # The probe MUST have been called even though a CUDA failure
        # was already in the failures list.
        self.assertTrue(
            probe_called,
            "RetinaFace runtime probe was skipped while torch_cuda_failure was "
            "present — this is the round-6 P2 regression. The probe must run "
            "for any non-CUDA failure-free state, including CUDA-only failure."
        )

        # And both failures must appear in the final list.
        combined = "\n".join(failures)
        self.assertFalse(ok, failures)
        self.assertIn("torch_cuda_failure:", combined)
        self.assertIn("retinaface runtime loader failed", combined)

    def test_torch_cpu_fallback_pins_to_stripped_public_version(self):
        """End-to-end: the cmd that goes to pip in the CUDA-broken scenario
        must contain `torch==2.5.1` (PUBLIC base), NOT `torch==2.5.1+cu121`.
        This verifies the strip-and-pin contract reaches `run_torch_cpu_fallback`,
        not just `_installed_torch_version` in isolation.
        """
        captured_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch(
            "dependency_health_check.importlib.metadata.version",
            return_value="2.5.1+cu121",
        ), mock.patch(
            "dependency_health_check.subprocess.run", side_effect=fake_run
        ):
            ok, _ = dhc.run_torch_cpu_fallback()

        self.assertTrue(ok)
        cmd = captured_cmds[0]
        self.assertIn("torch==2.5.1", cmd)
        # And NO local-version-suffixed form — that would fail against
        # the CPU wheel index.
        self.assertNotIn("torch==2.5.1+cu121", cmd)


if __name__ == "__main__":
    unittest.main()


def test_run_repair_succeeds_when_face_stack_ok_despite_mediapipe_step_failure():
    """Code-review HIGH-2: a transient mediapipe/sounddevice install failure must
    NOT flip run_repair to False and block launch when the face stack itself
    repaired fine. Only cuda_ok + face_ok gate the return; the mediapipe steps
    contribute a message. (The authoritative launch gate is main()'s fresh
    verify_in_fresh_process, which independently re-probes FaceLandmarker.)"""
    def fake_run(cmd, *a, **k):
        kind = _classify_repair_call(cmd)
        # face stack succeeds; the mediapipe --no-deps + runtime-deps steps fail.
        rc = 1 if kind in ("mediapipe", "mediapipe_runtime") else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="x")

    with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
        ok, message = dhc.run_repair(failures=["tensorflow import failed: foo"])

    assert ok is True, "face stack OK must yield run_repair True despite mediapipe hiccup"
    assert "non-fatal" in message or "authoritative" in message
