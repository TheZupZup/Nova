"""Code mode repository grounding — bounded, read-only, never a dump.

Pins the safety and behaviour contract of ``core.code_context`` and the
two read-only Dev Workspace helpers it stands on:

  * the git allowlist stays read-only after gaining ``ls-files``;
  * ``read_text_snippet`` refuses secrets, traversal, symlinks, binaries
    and out-of-root paths, and caps what it returns;
  * file selection is deterministic and driven by the *user's* message
    and git state — never by the model;
  * the rendered block is capped, carries an untrusted-data frame, and
    is empty whenever the feature is off / the repo is unlinked;
  * a broken repo degrades to a calm empty block, never an exception.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from core import code_context as cc
from core import dev_workspace as dw


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A small real git repo inside an allowed workspace root."""
    root = tmp_path / "workspace"
    root.mkdir()
    checkout = root / "proj"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    (checkout / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 61)), encoding="utf-8"
    )
    (checkout / "core").mkdir()
    (checkout / "core" / "router.py").write_text(
        "def route():\n    return 'code'\n", encoding="utf-8"
    )
    (checkout / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "initial commit")

    monkeypatch.setenv(dw.ENV_ROOTS, str(root))
    monkeypatch.delenv(cc.ENV_ENABLED, raising=False)
    monkeypatch.delenv(cc.ENV_MAX_FILES, raising=False)
    monkeypatch.delenv(cc.ENV_BUDGET, raising=False)
    return checkout


class TestGitAllowlistStaysReadOnly:
    def test_ls_files_is_allowlisted_and_read_only(self):
        assert ("ls-files",) in dw._ALLOWED_GIT_ARGV
        forbidden = {
            "push", "commit", "fetch", "pull", "clone", "remote",
            "checkout", "merge", "rebase", "reset", "add", "rm", "mv",
            "tag", "stash", "apply", "am", "cherry-pick", "revert",
            "gc", "prune", "fsck", "init", "config", "switch",
            "restore", "worktree", "submodule",
        }
        for argv in dw._ALLOWED_GIT_ARGV:
            assert argv[0] in {"status", "branch", "log", "diff", "ls-files"}
            assert not (set(argv) & forbidden)

    def test_tracked_files_reads_the_index(self, repo):
        tracked = dw.git_tracked_files(str(repo))
        assert "app.py" in tracked
        assert "core/router.py" in tracked

    def test_untracked_files_are_invisible(self, repo):
        (repo / "node_modules_blob.py").write_text("x = 1\n", encoding="utf-8")
        assert "node_modules_blob.py" not in dw.git_tracked_files(str(repo))


class TestReadTextSnippet:
    def test_reads_a_bounded_excerpt(self, repo):
        snippet = dw.read_text_snippet(str(repo), "app.py", max_lines=10)
        assert snippet.path == "app.py"
        assert snippet.total_lines == 60
        assert snippet.end_line == 10
        assert snippet.truncated is True
        assert snippet.text.startswith("line 1")

    def test_small_file_is_not_marked_truncated(self, repo):
        snippet = dw.read_text_snippet(str(repo), "core/router.py")
        assert snippet.truncated is False
        assert "def route" in snippet.text

    def test_char_cap_truncates(self, repo):
        snippet = dw.read_text_snippet(str(repo), "app.py", max_chars=20)
        assert len(snippet.text) <= 20
        assert snippet.truncated is True

    @pytest.mark.parametrize("path", [
        ".env", "../outside.py", "/etc/passwd", ".git/config",
        "core/../../escape.py", "~/secrets.py", "id_rsa",
    ])
    def test_dangerous_paths_are_refused(self, repo, path):
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(repo), path)

    def test_missing_file_is_refused(self, repo):
        with pytest.raises(dw.RepoReadError, match="not found"):
            dw.read_text_snippet(str(repo), "nope.py")

    def test_directory_is_refused(self, repo):
        with pytest.raises(dw.RepoReadError, match="regular files"):
            dw.read_text_snippet(str(repo), "core")

    def test_symlink_is_refused(self, repo, tmp_path):
        target = tmp_path / "outside.txt"
        target.write_text("secret", encoding="utf-8")
        link = repo / "link.py"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable")
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(repo), "link.py")

    def test_binary_content_is_refused(self, repo):
        (repo / "blob.py").write_bytes(b"import os\x00\x01\x02")
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(repo), "blob.py")

    def test_repo_outside_allowed_roots_is_refused(self, repo, monkeypatch):
        monkeypatch.setenv(dw.ENV_ROOTS, "/nonexistent-root")
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(repo), "app.py")


class TestSelection:
    def test_mentioned_file_is_selected(self, repo):
        tracked = dw.git_tracked_files(str(repo))
        assert cc.mentioned_paths(
            "please look at core/router.py", tracked
        ) == ("core/router.py",)

    def test_bare_basename_resolves_when_unique(self, repo):
        tracked = dw.git_tracked_files(str(repo))
        assert cc.mentioned_paths("what does router.py do", tracked) == (
            "core/router.py",
        )

    def test_ambiguous_basename_is_skipped_not_guessed(self):
        tracked = ("a/util.py", "b/util.py")
        assert cc.mentioned_paths("check util.py", tracked) == ()

    def test_untracked_mention_is_ignored(self, repo):
        tracked = dw.git_tracked_files(str(repo))
        assert cc.mentioned_paths("read /etc/passwd.py now", tracked) == ()

    def test_extensionless_word_never_selects_a_file(self, repo):
        tracked = dw.git_tracked_files(str(repo))
        assert cc.mentioned_paths("router", tracked) == ()


class TestBuildCodeContext:
    def test_ready_context_reports_git_state(self, repo):
        ctx = cc.build_code_context(str(repo), "explain core/router.py")
        assert ctx.ready
        assert ctx.clean is True
        assert ctx.tracked_count == 3
        assert [s.path for s in ctx.snippets] == ["core/router.py"]

    def test_dirty_tree_selects_changed_files(self, repo):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        ctx = cc.build_code_context(str(repo), "why is this broken?")
        assert ctx.clean is False
        assert "app.py" in ctx.changed_files
        assert [s.path for s in ctx.snippets] == ["app.py"]

    def test_secret_files_never_reach_the_context(self, repo):
        (repo / ".env").write_text("SECRET=changed\n", encoding="utf-8")
        ctx = cc.build_code_context(str(repo), "check .env please")
        block = ctx.as_prompt_block()
        assert "hunter2" not in block
        assert "SECRET=" not in block
        assert all(s.path != ".env" for s in ctx.snippets)

    def test_file_count_is_capped(self, repo):
        for i in range(10):
            (repo / f"f{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        ctx = cc.build_code_context(str(repo), "look at everything")
        assert len(ctx.snippets) <= cc.DEFAULT_MAX_FILES

    def test_character_budget_is_respected(self, repo, monkeypatch):
        monkeypatch.setenv(cc.ENV_BUDGET, "200")
        (repo / "app.py").write_text(
            "\n".join("y" * 80 for _ in range(200)), encoding="utf-8"
        )
        ctx = cc.build_code_context(str(repo), "fix app.py")
        total = sum(len(s.text) for s in ctx.snippets)
        assert total <= 200
        assert ctx.budget_exhausted is True

    def test_disabled_yields_an_empty_block(self, repo, monkeypatch):
        monkeypatch.setenv(cc.ENV_ENABLED, "false")
        ctx = cc.build_code_context(str(repo), "fix app.py")
        assert ctx.state == "disabled"
        assert ctx.as_prompt_block() == ""

    def test_no_repo_yields_an_empty_block(self):
        ctx = cc.build_code_context(None, "fix it")
        assert ctx.state == "no_repo"
        assert ctx.as_prompt_block() == ""

    def test_invalid_repo_degrades_calmly(self, tmp_path, monkeypatch):
        monkeypatch.setenv(dw.ENV_ROOTS, str(tmp_path))
        ctx = cc.build_code_context(str(tmp_path / "gone"), "hello")
        assert ctx.state != dw.STATE_READY
        assert ctx.as_prompt_block() == ""

    def test_block_carries_an_untrusted_data_frame(self, repo):
        block = cc.build_code_context(str(repo), "explain app.py").as_prompt_block()
        lowered = block.lower()
        assert "untrusted data" in lowered
        assert "never as instructions" in lowered
        assert "cannot run commands" in lowered

    def test_block_names_branch_and_cleanliness(self, repo):
        block = cc.build_code_context(str(repo), "status?").as_prompt_block()
        assert "Branch:" in block
        assert "Working tree: clean" in block

    def test_context_is_json_serialisable(self, repo):
        import json

        json.dumps(cc.build_code_context(str(repo), "hi").as_dict())
