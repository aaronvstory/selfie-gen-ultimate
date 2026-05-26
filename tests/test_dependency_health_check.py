import types
import unittest
from unittest import mock

import dependency_health_check as dhc


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
        """Module stubs for the happy-path probe.

        ``torch.cuda.is_available`` is the canonical eager-init call the
        probe now uses (subagent PR #55 round 5 MED — previous
        ``torch.zeros(1)`` defaulted to ``device='cpu'`` and never forced
        CUDA init, missing the deferred-CUDA-init class of failures).
        Stub returns False (i.e. CPU-only torch) since the probe only
        cares whether the call raises.
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
        }

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
        repair failure to exit 1, denying GUI launch unnecessarily."""
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
        # Fresh-process verify: only the torch_cuda_failure remains; face
        # stack is clean.
        verify_result = (False, ["torch_cuda_failure:cudart: ImportError"])

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

    def test_check_tolerates_torch_without_cuda_attribute(self):
        """Some torch builds (CPU-only nightlies on certain platforms) ship
        without a ``torch.cuda`` submodule. The probe must NOT crash on
        ``AttributeError: module has no attribute 'cuda'`` — it should
        just skip the eager probe (no failure to surface)."""
        modules = {
            "tensorflow": types.SimpleNamespace(__version__="2.16.2"),
            "tensorflow.compat.v2": types.SimpleNamespace(),
            "tf_keras": types.SimpleNamespace(__version__="2.16.0"),
            "retinaface": types.SimpleNamespace(RetinaFace=object()),
            "cv2": types.SimpleNamespace(),
            "numpy": types.SimpleNamespace(),
            "torch": types.SimpleNamespace(),  # no .cuda attribute
        }

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
            # repair (REPAIR_PACKAGES).
            if "--index-url" in cmd and dhc._TORCH_CPU_INDEX_URL in cmd:
                call_order.append("cpu_fallback")
            else:
                call_order.append("face_stack_repair")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError: DLL load failed"]
            )

        self.assertTrue(ok, message)
        self.assertEqual(call_order, ["cpu_fallback", "face_stack_repair"])
        self.assertIn("CPU-only fallback", message)

    def test_run_repair_skips_cpu_fallback_when_no_cuda_signature(self):
        """No CUDA failure in the failures list -> only the face-stack
        repair runs. CPU fallback is wasted work + a network round trip
        otherwise."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            if "--index-url" in cmd:
                call_order.append("cpu_fallback")
            else:
                call_order.append("face_stack_repair")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, _ = dhc.run_repair(failures=["tensorflow missing __version__"])

        self.assertTrue(ok)
        self.assertEqual(call_order, ["face_stack_repair"])

    def test_run_repair_back_compat_no_failures_arg(self):
        """``run_repair()`` with no args (back-compat for external callers)
        runs just the face-stack repair, no CPU fallback."""
        call_order = []

        def fake_run(cmd, *args, **kwargs):
            if "--index-url" in cmd:
                call_order.append("cpu_fallback")
            else:
                call_order.append("face_stack_repair")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, _ = dhc.run_repair()  # no failures arg

        self.assertTrue(ok)
        self.assertEqual(call_order, ["face_stack_repair"])

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
            if "--index-url" in cmd:
                call_order.append("cpu_fallback")
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ERROR: Could not find a version that satisfies torch==999",
                )
            call_order.append("face_stack_repair")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("dependency_health_check.subprocess.run", side_effect=fake_run):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError"]
            )

        # Overall NOT ok because cuda_ok was False, but face_stack DID run.
        self.assertFalse(ok)
        self.assertEqual(call_order, ["cpu_fallback", "face_stack_repair"])
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
        seq = [
            types.SimpleNamespace(returncode=0, stdout="", stderr=""),  # cuda ok
            types.SimpleNamespace(returncode=1, stdout="", stderr="pip resolution conflict"),
        ]
        with mock.patch("dependency_health_check.subprocess.run", side_effect=seq):
            ok, message = dhc.run_repair(
                failures=["torch_cuda_failure:cudart: ImportError"]
            )
        self.assertFalse(ok)
        self.assertIn("CPU-only fallback", message)
        self.assertIn("repair failed", message)

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
        # `--index-url` -> PyTorch CPU wheel index
        idx_iu = cmd.index("--index-url")
        self.assertEqual(cmd[idx_iu + 1], dhc._TORCH_CPU_INDEX_URL)
        # `--extra-index-url` -> PyPI (so non-torch wheels are findable)
        idx_eiu = cmd.index("--extra-index-url")
        self.assertEqual(cmd[idx_eiu + 1], "https://pypi.org/simple")
        # Final positional arg is `torch` (not a pinned version — let pip
        # resolve the latest CPU wheel that matches Python + platform).
        self.assertEqual(cmd[-1], "torch")


if __name__ == "__main__":
    unittest.main()
