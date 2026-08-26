"""Regression coverage for findings from the PR #231 Codex review.

These tests stay deliberately narrow so each reviewed failure mode has a
stable reproducer independent of the broader model-platform suite.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core import code_context as cc
from core import coder_dataset as cd
from core import dev_workspace as dw
from core import model_eval as me
from core import model_health as mh


class _Reply:
    def __init__(self, content: str, model: str):
        self.content = content
        self.model = model


class _Provider:
    def __init__(self, name: str = "mock", content: str = "ok"):
        self.name = name
        self.content = content

    def generate(self, request):
        return _Reply(self.content, request.model)


def test_code_context_filters_untracked_dirty_files_before_prompt(monkeypatch):
    status = dw.RepoStatus(
        state=dw.STATE_READY,
        repo_path="/safe/repo",
        branch="main",
        clean=False,
        changed_files=(
            {"status": "??", "path": "generated-secret.py"},
            {"status": "M", "path": "tracked.py"},
        ),
    )
    monkeypatch.setattr(dw, "read_status", lambda *a, **k: status)
    monkeypatch.setattr(dw, "git_tracked_files", lambda *a, **k: ("tracked.py",))

    reads = []

    def _read(_repo, path, **_kwargs):
        reads.append(path)
        return dw.FileSnippet(
            path=path,
            text="print('tracked')",
            start_line=1,
            end_line=1,
            total_lines=1,
            truncated=False,
        )

    monkeypatch.setattr(dw, "read_text_snippet", _read)

    ctx = cc.build_code_context("/safe/repo", "why is this dirty?")
    assert ctx.changed_files == ("tracked.py",)
    assert [s.path for s in ctx.snippets] == ["tracked.py"]
    assert reads == ["tracked.py"]
    assert "generated-secret.py" not in ctx.as_prompt_block()


def test_evaluation_records_runtime_context_not_profile_guess(monkeypatch):
    provider = _Provider(name="ollama", content="ok")
    monkeypatch.setattr("core.model_providers.get_provider", lambda: provider)
    monkeypatch.setattr(
        "core.ollama_client.list_running_models",
        lambda: [{"name": "qwen:test", "context_size": 8192}],
    )
    case = me.parse_case({"id": "ctx", "prompt": "Say ok."})
    result = me.run_case(case, "qwen:test")
    assert result["context_size"] == 8192


def test_evaluation_context_is_none_when_runtime_cannot_report_it(monkeypatch):
    provider = _Provider(name="mock", content="ok")
    monkeypatch.setattr("core.model_providers.get_provider", lambda: provider)
    case = me.parse_case({"id": "ctx-none", "prompt": "Say ok."})
    result = me.run_case(case, "some-model")
    assert result["context_size"] is None


def test_dataset_uses_prompt_snapshot_even_if_case_lookup_changes(tmp_path, monkeypatch):
    db = str(tmp_path / "eval.db")
    me.migrate(db)
    provider = _Provider(name="mock", content="approved answer")
    monkeypatch.setattr("core.model_providers.get_provider", lambda: provider)

    # Use a shipped case so start_run exercises the real persistence path.
    case_id = me.load_cases()[0][0].id
    run = me.start_run(["m:1b"], [case_id], db_path=db, background=False)
    result = me.list_results(run["id"], db_path=db)[0]
    original_prompt = result["prompt_snapshot"]
    assert original_prompt

    me.set_result_approval(result["id"], True, db_path=db)

    # Export must no longer consult the mutable case registry at all.
    monkeypatch.setattr(
        me,
        "get_case",
        lambda _case_id: (_ for _ in ()).throw(
            AssertionError("dataset export must not reconstruct the prompt")
        ),
    )
    example = cd.build_examples([result["id"]], db_path=db)[0]
    assert example.prompt == original_prompt


def test_dataset_refuses_legacy_result_without_prompt_snapshot(tmp_path, monkeypatch):
    db = str(tmp_path / "eval.db")
    me.migrate(db)
    provider = _Provider(name="mock", content="approved answer")
    monkeypatch.setattr("core.model_providers.get_provider", lambda: provider)
    case_id = me.load_cases()[0][0].id
    run = me.start_run(["m:1b"], [case_id], db_path=db, background=False)
    result_id = me.list_results(run["id"], db_path=db)[0]["id"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE model_eval_results SET prompt_snapshot = '' WHERE id = ?",
            (result_id,),
        )
    me.set_result_approval(result_id, True, db_path=db)
    with pytest.raises(cd.DatasetExportError, match="predates prompt snapshots"):
        cd.build_examples([result_id], db_path=db)


def test_migrate_adds_snapshot_columns_to_legacy_table(tmp_path):
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE model_eval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                case_title TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                context_size INTEGER,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                constraints_passed INTEGER NOT NULL DEFAULT 0,
                constraints_total INTEGER NOT NULL DEFAULT 0,
                constraints_json TEXT NOT NULL DEFAULT '[]',
                output TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                human_rating INTEGER,
                human_note TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
    me.migrate(db)
    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(model_eval_results)")}
    assert "prompt_snapshot" in columns
    assert "case_source" in columns


def test_model_health_preserves_unknown_loaded_state_when_ps_fails(monkeypatch):
    monkeypatch.setattr(
        "core.provider_status.probe_provider_health",
        lambda *a, **k: {
            "ok": True,
            "provider": "ollama",
            "detail": "",
            "models": ["gemma4"],
        },
    )

    class _Ollama:
        name = "ollama"

    monkeypatch.setattr("core.model_providers.get_provider", lambda: _Ollama())
    monkeypatch.setattr(
        "core.ollama_client.list_running_models",
        lambda: (_ for _ in ()).throw(RuntimeError("ps unavailable")),
    )
    monkeypatch.setattr(
        "core.model_profiles.configured_role_models",
        lambda: {
            "router": "",
            "chat": "gemma4",
            "code": "",
            "advanced": "",
        },
    )

    health = mh.get_model_health()
    chat = next(r for r in health["roles"] if r["role"] == "chat")
    assert chat["state"] == mh.STATE_INSTALLED
    assert chat["loaded"] is None
    assert "cannot tell" in chat["hint"].lower()
    assert "cold start" in chat["hint"].lower()


# ── Second Codex review (commit b8e6335) ────────────────────────────
#
# Three findings about single-model backends and discovery caps. A
# provider that ignores ``request.model`` (llama.cpp serves one
# configured ``.gguf``) must never have a requested label treated as
# provenance, and must not be described as if role names selected
# anything.


class _SingleModelProvider:
    """Stands in for llama.cpp: one file, ignores the requested name."""

    name = "llamacpp"
    selects_model_by_name = False

    def __init__(self, backend_id="nova-coder-14b.Q4_K_M.gguf"):
        self._backend_id = backend_id
        self.seen = []

    def backend_model_id(self):
        return self._backend_id

    def generate(self, request):
        self.seen.append(request.model)

        class _R:
            content = "```py\nshell=False\ntimeout=5\n```" + "x" * 200
            model = request.model

        return _R()


class TestSingleModelProviderProvenance:
    """P1: a run must not attribute one backend to several 'models'."""

    def test_result_records_the_backend_model_not_the_label(
        self, tmp_path, monkeypatch
    ):
        from core import model_eval as me

        db = str(tmp_path / "eval.db")
        me.migrate(db)
        provider = _SingleModelProvider()
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: provider
        )
        run = me.start_run(
            ["pretend-model:70b"], ["readonly-git-helper"],
            db_path=db, background=False,
        )
        row = me.list_results(run["id"], db_path=db)[0]
        assert row["model"] == "nova-coder-14b.Q4_K_M.gguf"
        assert row["requested_model"] == "pretend-model:70b"

    def test_multi_model_run_is_refused_on_a_single_model_backend(
        self, tmp_path, monkeypatch
    ):
        from core import model_eval as me

        db = str(tmp_path / "eval.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _SingleModelProvider(),
        )
        with pytest.raises(me.EvalError, match="single configured model"):
            me.start_run(
                ["a:1b", "b:7b"], ["readonly-git-helper"],
                db_path=db, background=False,
            )
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT COUNT(*) FROM model_eval_results"
        ).fetchone()[0] == 0

    def test_name_routing_backend_keeps_the_requested_label(
        self, tmp_path, monkeypatch
    ):
        from core import model_eval as me

        class _Routing(_SingleModelProvider):
            name = "ollama"
            selects_model_by_name = True

        db = str(tmp_path / "eval.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: _Routing()
        )
        run = me.start_run(
            ["qwen2.5-coder:7b"], ["readonly-git-helper"],
            db_path=db, background=False,
        )
        row = me.list_results(run["id"], db_path=db)[0]
        assert row["model"] == "qwen2.5-coder:7b"

    def test_exported_provenance_names_the_real_backend(
        self, tmp_path, monkeypatch
    ):
        from core import coder_dataset as cd
        from core import model_eval as me

        db = str(tmp_path / "eval.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _SingleModelProvider(),
        )
        run = me.start_run(
            ["marketing-name:99b"], ["readonly-git-helper"],
            db_path=db, background=False,
        )
        rid = me.list_results(run["id"], db_path=db)[0]["id"]
        me.set_result_approval(rid, True, db_path=db)
        example = cd.build_examples([rid], db_path=db)[0]
        assert example.metadata["model"] == "nova-coder-14b.Q4_K_M.gguf"
        assert "marketing-name:99b" not in json.dumps(
            example.as_jsonl_record()["metadata"]
        )


class TestSingleModelProviderHealth:
    """P2: role names are not selectors on a single-model backend."""

    def _health(self, monkeypatch, *, reachable=True):
        from core import model_health as mh

        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": reachable, "provider": "llamacpp", "detail": "",
                "models": ["nova-coder-14b.Q4_K_M.gguf"] if reachable else [],
            },
        )
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _SingleModelProvider(),
        )
        return mh.get_model_health()

    def test_roles_are_not_reported_as_not_installed(self, monkeypatch):
        health = self._health(monkeypatch)
        assert all(r["state"] != "not_installed" for r in health["roles"])

    def test_no_ollama_pull_advice_on_a_non_ollama_host(self, monkeypatch):
        health = self._health(monkeypatch)
        assert all("ollama pull" not in r["hint"] for r in health["roles"])

    def test_hint_names_the_configured_backend_model(self, monkeypatch):
        health = self._health(monkeypatch)
        assert all(
            "nova-coder-14b.Q4_K_M.gguf" in r["hint"] for r in health["roles"]
        )

    def test_unreachable_single_model_backend_is_unknown(self, monkeypatch):
        health = self._health(monkeypatch, reachable=False)
        assert all(r["state"] == "unknown" for r in health["roles"])
        assert all(r["loaded"] is None for r in health["roles"])


class TestApprovedDiscoveryIsUncapped:
    """P2: readiness must not hide older approved results."""

    def test_approved_rows_beyond_the_browsing_cap_are_listed(self, tmp_path):
        from core import coder_dataset as cd
        from core import model_eval as me

        db = str(tmp_path / "eval.db")
        me.migrate(db)
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(db) as conn:
            # One approved row, then more than the newest-N browsing
            # window's worth of newer unapproved rows on top of it.
            conn.execute(
                "INSERT INTO model_eval_results "
                "(run_id, case_id, model, approved, output, prompt_snapshot, "
                "created_at) VALUES (1,'c','m:1b',1,'answer','task',?)",
                (now,),
            )
            for _ in range(600):
                conn.execute(
                    "INSERT INTO model_eval_results "
                    "(run_id, case_id, model, approved, created_at) "
                    "VALUES (1,'c','m:1b',0,?)",
                    (now,),
                )
        readiness = cd.describe_export_readiness(db_path=db)
        assert readiness["approved_count"] == 1
        assert readiness["approved_result_ids"] == [1]
