"""Regression coverage for findings from the PR #231 Codex review.

These tests stay deliberately narrow so each reviewed failure mode has a
stable reproducer independent of the broader model-platform suite.
"""

from __future__ import annotations

import json
import subprocess
import os
import pathlib
import shutil
import sqlite3
import time

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


# ── Third Codex review (commit c04888f) ─────────────────────────────


class TestSingleModelResidency:
    """P2: reachable is not resident.

    A single-model backend's health probe deliberately does not load the
    model, so treating "configured and reachable" as "loaded" hides the
    cold start the next request will pay. This was a regression
    introduced by the previous round's fix.
    """

    class _Provider:
        name = "llamacpp"
        selects_model_by_name = False

        def __init__(self, resident):
            self._resident = resident

        def backend_model_id(self):
            return "nova-coder-14b.Q4_K_M.gguf"

        def is_model_resident(self):
            return self._resident

    def _role(self, monkeypatch, resident):
        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "llamacpp", "detail": "",
                "models": ["nova-coder-14b.Q4_K_M.gguf"],
            },
        )
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: self._Provider(resident),
        )
        return mh.get_model_health()["roles"][0]

    def test_configured_but_never_generated_is_not_called_loaded(
        self, monkeypatch
    ):
        role = self._role(monkeypatch, False)
        assert role["loaded"] is False
        assert role["state"] == mh.STATE_INSTALLED
        assert "cold start" in role["hint"]

    def test_actually_resident_is_reported_loaded(self, monkeypatch):
        role = self._role(monkeypatch, True)
        assert role["loaded"] is True
        assert role["state"] == mh.STATE_LOADED

    def test_provider_that_cannot_answer_stays_unknown(self, monkeypatch):
        role = self._role(monkeypatch, None)
        assert role["loaded"] is None
        assert "could not be determined" in role["hint"]

    def test_llamacpp_reports_residency_from_its_handle(self):
        from core.model_providers import LlamaCppProvider

        provider = LlamaCppProvider(model_path="")
        # health() never loads, so before any generation the handle is
        # unset and residency is a definite False, not None.
        assert provider.is_model_resident() is False

    def test_base_contract_defaults_to_unknown(self):
        from core.model_providers.base import ModelProvider

        assert ModelProvider.is_model_resident(object()) is None


class TestTrackedSetIsNotSilentlyTruncated:
    """P2: the membership set decides what may be excerpted at all."""

    def test_cap_is_high_enough_for_real_repositories(self):
        # The Linux kernel is ~80k tracked files; the bound exists only
        # to keep memory finite, not to limit ordinary repositories.
        assert dw.MAX_TRACKED_FILES >= 100_000

    def test_truncation_is_reported_rather_than_silent(self, monkeypatch):
        """A partial index listing must not masquerade as complete."""
        fake = tuple(f"src/file{i}.py" for i in range(dw.MAX_TRACKED_FILES))
        monkeypatch.setattr(cc.dev_workspace, "git_tracked_files", lambda p: fake)

        status = dw.RepoStatus(
            state=dw.STATE_READY, repo_path="/repo", branch="main", clean=True,
        )
        monkeypatch.setattr(
            cc.dev_workspace, "read_status", lambda p, roots=None: status
        )
        ctx = cc.build_code_context("/repo", "hello")
        assert ctx.tracked_truncated is True
        assert "truncated" in ctx.as_prompt_block()

    def test_untruncated_listing_reports_an_exact_count(self, monkeypatch):
        fake = ("a.py", "b.py")
        monkeypatch.setattr(cc.dev_workspace, "git_tracked_files", lambda p: fake)
        status = dw.RepoStatus(
            state=dw.STATE_READY, repo_path="/repo", branch="main", clean=True,
        )
        monkeypatch.setattr(
            cc.dev_workspace, "read_status", lambda p, roots=None: status
        )
        ctx = cc.build_code_context("/repo", "hello")
        assert ctx.tracked_truncated is False
        assert ctx.tracked_count == 2
        assert "truncated" not in ctx.as_prompt_block()


# ── Fourth Codex review (commit e8452b9) ────────────────────────────


class TestConfiguredContextIsNotArchitectureMax:
    """P2: the architecture maximum is a capability, not a setting.

    A Modelfile carrying `num_ctx 8192` on a model whose architecture
    supports 131072 runs at 8192. Reporting the larger number as
    `runtime_context_size` overstates the context by 16x.
    """

    def test_num_ctx_wins_over_architecture_length(self):
        payload = {
            "model_info": {"llama.context_length": 131072},
            "parameters": "stop <eos>\nnum_ctx 8192",
        }
        assert mh._configured_context_from_show(payload) == 8192
        assert mh._context_capacity_from_show(payload) == 131072

    def test_architecture_length_alone_is_not_a_configuration(self):
        payload = {"model_info": {"llama.context_length": 131072}}
        assert mh._configured_context_from_show(payload) is None
        assert mh._context_capacity_from_show(payload) == 131072

    def test_num_ctx_alone_is_the_configuration(self):
        payload = {"parameters": "num_ctx 4096"}
        assert mh._configured_context_from_show(payload) == 4096
        assert mh._context_capacity_from_show(payload) is None

    def test_role_reports_configured_and_capacity_separately(self, monkeypatch):
        from core import ollama_client

        show = {
            "model_info": {"llama.context_length": 131072},
            "parameters": "num_ctx 8192",
        }
        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "ollama", "detail": "",
                "models": ["gemma4"],
            },
        )
        monkeypatch.setattr(ollama_client, "list_running_models", lambda: [])
        monkeypatch.setattr(
            ollama_client, "show_model", lambda n, host=None: show
        )
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "", "chat": "gemma4", "code": "", "advanced": ""},
        )
        role = next(
            r for r in mh.get_model_health()["roles"] if r["role"] == "chat"
        )
        assert role["runtime_context_size"] == 8192
        assert role["context_capacity"] == 131072

    def test_loaded_models_own_report_still_wins(self, monkeypatch):
        from core import ollama_client

        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "ollama", "detail": "",
                "models": ["gemma4"],
            },
        )
        monkeypatch.setattr(
            ollama_client, "list_running_models",
            lambda: [{"name": "gemma4", "context_size": 2048}],
        )
        monkeypatch.setattr(
            ollama_client, "show_model",
            lambda n, host=None: {"parameters": "num_ctx 8192"},
        )
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "", "chat": "gemma4", "code": "", "advanced": ""},
        )
        role = next(
            r for r in mh.get_model_health()["roles"] if r["role"] == "chat"
        )
        assert role["runtime_context_size"] == 2048


class TestInterruptedRunRecovery:
    """P2: a restart must not leave a job that can never finish."""

    def _seed(self, db, status, completed=0):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO model_eval_runs "
                "(label, models, case_ids, status, total, completed, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("l", "a:1b", "c", status, 5, completed, "t"),
            )

    def test_active_rows_are_closed_out_at_startup(self, tmp_path):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        self._seed(db, me.STATUS_RUNNING, completed=2)
        self._seed(db, me.STATUS_QUEUED)

        assert me.recover_interrupted_runs(db) == 2
        for run in me.list_runs(db_path=db):
            assert run["status"] == me.STATUS_INTERRUPTED
            assert run["finished_at"]
            assert "restarted" in run["error"]

    def test_partial_results_are_kept(self, tmp_path):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        self._seed(db, me.STATUS_RUNNING, completed=3)
        me.recover_interrupted_runs(db)
        assert me.list_runs(db_path=db)[0]["completed"] == 3

    def test_terminal_runs_are_untouched(self, tmp_path):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        self._seed(db, me.STATUS_DONE, completed=5)
        self._seed(db, me.STATUS_ERROR)
        assert me.recover_interrupted_runs(db) == 0
        statuses = {r["status"] for r in me.list_runs(db_path=db)}
        assert statuses == {me.STATUS_DONE, me.STATUS_ERROR}

    def test_recovery_is_idempotent(self, tmp_path):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        self._seed(db, me.STATUS_RUNNING)
        assert me.recover_interrupted_runs(db) == 1
        assert me.recover_interrupted_runs(db) == 0

    def test_recovery_never_raises_on_a_broken_db(self, tmp_path):
        missing = str(tmp_path / "nope" / "e.db")
        assert me.recover_interrupted_runs(missing) == 0

    def test_startup_wires_the_recovery(self):
        """initialize_db must actually call it, not just define it."""
        import inspect

        from core import memory

        assert "_recover_eval_runs" in inspect.getsource(memory.initialize_db)


# ── Fifth Codex review (commit df2a1b6) ─────────────────────────────


class _EchoProvider:
    name = "ollama"
    selects_model_by_name = True

    def __init__(self, content):
        self._content = content

    def generate(self, request):
        class _R:
            content = self._content
            model = request.model

        return _R()


class TestFullResponseIsScoredBeforeTruncation:
    """P2: the storage cap must not manufacture a passing result."""

    @pytest.fixture
    def case(self):
        return me.parse_case({
            "id": "cap", "prompt": "p",
            "constraints": [{"kind": "max_chars", "value": str(me.MAX_OUTPUT_CHARS)}],
        })

    def test_over_limit_answer_does_not_pass_via_truncation(
        self, case, monkeypatch
    ):
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _EchoProvider("x" * (me.MAX_OUTPUT_CHARS + 1)),
        )
        result = me.run_case(case, "m:1b")
        assert result["success"] is False
        assert result["output_truncated"] is True
        assert len(result["output"]) == me.MAX_OUTPUT_CHARS

    def test_within_limit_answer_still_passes(self, case, monkeypatch):
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _EchoProvider("x" * 100),
        )
        result = me.run_case(case, "m:1b")
        assert result["success"] is True
        assert result["output_truncated"] is False

    def test_truncation_flag_is_persisted(self, tmp_path, monkeypatch):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _EchoProvider("y" * (me.MAX_OUTPUT_CHARS + 50)),
        )
        run = me.start_run(
            ["a:1b"], ["readonly-git-helper"], db_path=db, background=False
        )
        assert me.list_results(run["id"], db_path=db)[0]["output_truncated"] is True

    def test_truncated_result_cannot_be_exported(self, tmp_path, monkeypatch):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _EchoProvider("y" * (me.MAX_OUTPUT_CHARS + 50)),
        )
        run = me.start_run(
            ["a:1b"], ["readonly-git-helper"], db_path=db, background=False
        )
        rid = me.list_results(run["id"], db_path=db)[0]["id"]
        me.set_result_approval(rid, True, db_path=db)
        with pytest.raises(cd.DatasetExportError, match="truncated"):
            cd.build_examples([rid], db_path=db)


class TestExportedMetadataIsScanned:
    """P2: provenance rides into the corpus and must pass the gate too."""

    @pytest.fixture
    def approved(self, tmp_path, monkeypatch):
        db = str(tmp_path / "e.db")
        me.migrate(db)
        monkeypatch.setattr(
            "core.model_providers.get_provider",
            lambda: _EchoProvider(
                "```py\nshell=False\ntimeout=5\n```" + " explanation" * 30
            ),
        )
        run = me.start_run(
            ["a:1b"], ["readonly-git-helper"], db_path=db, background=False
        )
        rid = me.list_results(run["id"], db_path=db)[0]["id"]
        me.set_result_approval(rid, True, db_path=db)
        return db, rid

    def _set_source(self, db, rid, value):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE model_eval_results SET case_source = ? WHERE id = ?",
                (value, rid),
            )

    @pytest.mark.parametrize("secret", [
        "reported by alice@example.com",
        "token=hunter2seven",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "-----BEGIN RSA PRIVATE KEY-----",
    ])
    def test_secret_in_provenance_refuses_the_export(self, approved, secret):
        db, rid = approved
        self._set_source(db, rid, secret)
        with pytest.raises(cd.DatasetExportError, match="credential"):
            cd.build_examples([rid], db_path=db)

    def test_clean_provenance_still_exports(self, approved):
        db, rid = approved
        self._set_source(db, rid, "linthra#412")
        example = cd.build_examples([rid], db_path=db)[0]
        assert example.metadata["case_source"] == "linthra#412"

    def test_every_string_metadata_field_is_covered(self, approved):
        """Generic, so a field added later is scanned without a code change."""
        db, rid = approved
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE model_eval_results SET model = ? WHERE id = ?",
                ("model-owned-by-bob@example.com", rid),
            )
        with pytest.raises(cd.DatasetExportError, match="credential"):
            cd.build_examples([rid], db_path=db)


# ── Sixth Codex review (commit d4bc141) ─────────────────────────────


class TestShowMetadataSurvivesPsFailure:
    """P2: /api/ps and /api/show fail independently.

    ps says what is *resident*; show says what a model is *configured*
    with. Gating show on ps having worked discarded a good `num_ctx` and
    architecture capacity whenever the loaded-model view was down.
    """

    def _role(self, monkeypatch, *, ps_ok):
        from core import ollama_client

        show = {
            "model_info": {"llama.context_length": 131072},
            "parameters": "num_ctx 8192",
        }
        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "ollama", "detail": "",
                "models": ["gemma4"],
            },
        )
        if ps_ok:
            monkeypatch.setattr(ollama_client, "list_running_models", lambda: [])
        else:
            def _boom():
                raise ollama_client.OllamaUnavailable("ps down")

            monkeypatch.setattr(ollama_client, "list_running_models", _boom)
        monkeypatch.setattr(
            ollama_client, "show_model", lambda n, host=None: show
        )
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "", "chat": "gemma4", "code": "", "advanced": ""},
        )
        return next(
            r for r in mh.get_model_health()["roles"] if r["role"] == "chat"
        )

    def test_context_metadata_survives_a_ps_failure(self, monkeypatch):
        role = self._role(monkeypatch, ps_ok=False)
        assert role["runtime_context_size"] == 8192
        assert role["context_capacity"] == 131072

    def test_residency_still_unknown_when_ps_fails(self, monkeypatch):
        """Recovering metadata must not resurrect a residency claim."""
        role = self._role(monkeypatch, ps_ok=False)
        assert role["loaded"] is None

    def test_working_ps_is_unaffected(self, monkeypatch):
        role = self._role(monkeypatch, ps_ok=True)
        assert role["runtime_context_size"] == 8192
        assert role["context_capacity"] == 131072
        assert role["loaded"] is False


class TestEvaluationDocsMatchTheCode:
    """P2: the docs described a behaviour the code no longer has.

    Documentation drift has now been a finding twice in this PR, so the
    claim that matters most is pinned against the implementation rather
    than left to review to catch a third time.
    """

    @pytest.fixture
    def doc(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / "docs" / "model-evaluation.md").read_text(encoding="utf-8")

    def test_docs_do_not_claim_the_profile_size_is_recorded(self, doc):
        lowered = doc.lower()
        assert "context size** nova assumed" not in lowered
        assert "observed, not assumed" in lowered

    def test_docs_explain_a_null_context(self, doc):
        assert "the runtime did not say" in doc

    def test_run_case_records_a_runtime_value_not_a_profile_value(self):
        """The code half of the claim the docs now make."""
        import inspect

        source = inspect.getsource(me.run_case)
        assert "_runtime_context_size(" in source
        # The profile's own context must not be what gets recorded.
        assert "profile.context_size" not in source


# ── Seventh Codex review (commit 5856e86) ───────────────────────────


class TestSnippetOpenIsRaceSafe:
    """P2: path validation expires the instant it returns.

    ``validate_proposed_path`` proves containment against the filesystem
    as it looked at that moment. Anything that happens between then and
    the open is unobserved, and *every* component of the path is
    swappable in that window — not just the final one. A no-follow open
    of the leaf alone still lets a swapped parent directory redirect the
    read outside the repository.
    """

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        root = tmp_path / "ws"
        root.mkdir()
        checkout = root / "proj"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"],
            cwd=checkout, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=checkout, check=True
        )
        (checkout / "app.py").write_text("real = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "i"], cwd=checkout, check=True
        )
        monkeypatch.setenv(dw.ENV_ROOTS, str(root))
        return checkout, tmp_path

    def test_symlink_present_before_validation_is_refused(self, repo):
        """Baseline: path validation catches a symlink that is already there."""
        checkout, tmp_path = repo
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("OUT_OF_ROOT_SECRET\n", encoding="utf-8")
        (checkout / "app.py").unlink()
        try:
            os.symlink(secret, checkout / "app.py")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable")
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(checkout), "app.py")

    def test_symlink_swapped_inside_the_race_window_is_refused(
        self, repo, monkeypatch
    ):
        """The actual TOCTOU: the swap happens *after* validation.

        ``validate_proposed_path`` proves containment with ``resolve()``,
        which is a statement about the filesystem at that instant. Here
        the file is replaced by a symlink to an out-of-root secret the
        moment validation returns — before a single byte is opened. Only
        no-follow semantics on the open itself can stop this.
        """
        checkout, tmp_path = repo
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("OUT_OF_ROOT_SECRET\n", encoding="utf-8")
        target = checkout / "app.py"

        try:
            os.symlink(secret, tmp_path / "_probe")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable")

        # Validation returning is exactly the start of the race window:
        # everything it checked is now a statement about the past.
        real_validate = dw.validate_proposed_path

        def _swap_after_validation(repo_root, raw):
            rel = real_validate(repo_root, raw)
            if not os.path.islink(str(target)):
                os.unlink(str(target))
                os.symlink(str(secret), str(target))
            return rel

        monkeypatch.setattr(dw, "validate_proposed_path", _swap_after_validation)

        # The refusal is the whole point: no ``FileSnippet`` is returned,
        # so the out-of-root secret never becomes prompt content.
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(checkout), "app.py")

    def test_parent_directory_swapped_in_the_window_cannot_escape(
        self, repo, monkeypatch
    ):
        """A no-follow leaf is not enough — the *parent* is swappable too.

        ``src/app.py`` validates cleanly and its final component stays an
        ordinary file the whole time. The attack replaces ``src`` with a
        symlink to a directory outside the repository, so the same
        relative path now names a different file. Checking only the leaf
        misses this entirely: the open succeeds and returns the outside
        secret.
        """
        checkout, tmp_path = repo
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "app.py").write_text(
            "OUT_OF_ROOT_SECRET = 1\n", encoding="utf-8"
        )

        src = checkout / "src"
        src.mkdir()
        (src / "app.py").write_text("real = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "src"], cwd=checkout, check=True
        )

        try:
            os.symlink(outside, tmp_path / "_probe_dir")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks unavailable")

        real_validate = dw.validate_proposed_path

        def _swap_parent_after_validation(repo_root, raw):
            rel = real_validate(repo_root, raw)
            if not os.path.islink(str(src)):
                shutil.rmtree(str(src))
                os.symlink(str(outside), str(src))
            return rel

        monkeypatch.setattr(
            dw, "validate_proposed_path", _swap_parent_after_validation
        )

        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(checkout), "src/app.py")

    def test_reads_refuse_to_run_without_safe_open_primitives(
        self, repo, monkeypatch
    ):
        """No safe primitive means no read — never a path-based fallback."""
        checkout, _ = repo
        monkeypatch.setattr(dw, "_SAFE_OPEN_SUPPORTED", False)
        with pytest.raises(dw.RepoReadError):
            dw.read_text_snippet(str(checkout), "app.py")

    def test_ordinary_reads_still_work(self, repo):
        checkout, _ = repo
        snippet = dw.read_text_snippet(str(checkout), "app.py")
        assert "real = 1" in snippet.text


class TestSingleModelBackendSkipsOllamaMetadata:
    """P2: a regression introduced by the round-6 fix.

    Removing the ps gate made the /api/show lookup unconditional, so a
    llama.cpp host with an Ollama daemon also running would publish an
    unrelated Ollama model's numbers for every llama.cpp role.
    """

    class _Provider:
        name = "llamacpp"
        selects_model_by_name = False

        def backend_model_id(self):
            return "nova-coder.gguf"

        def is_model_resident(self):
            return False

    def test_ollama_show_is_not_called_for_single_model_roles(
        self, monkeypatch
    ):
        from core import ollama_client

        calls = []

        def _show(name, host=None):
            calls.append(name)
            return {"parameters": "num_ctx 8192"}

        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "llamacpp", "detail": "",
                "models": ["nova-coder.gguf"],
            },
        )
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: self._Provider()
        )
        monkeypatch.setattr(ollama_client, "show_model", _show)

        health = mh.get_model_health()
        assert calls == []
        for role in health["roles"]:
            assert role["runtime_context_size"] is None
            assert role["context_capacity"] is None

    def test_ollama_roles_still_get_their_metadata(self, monkeypatch):
        """The round-6 fix must survive: a real Ollama host still reports."""
        from core import ollama_client

        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": True, "provider": "ollama", "detail": "",
                "models": ["gemma4"],
            },
        )
        monkeypatch.setattr(ollama_client, "list_running_models", lambda: [])
        monkeypatch.setattr(
            ollama_client, "show_model",
            lambda n, host=None: {"parameters": "num_ctx 8192"},
        )
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "", "chat": "gemma4", "code": "", "advanced": ""},
        )
        role = next(
            r for r in mh.get_model_health()["roles"] if r["role"] == "chat"
        )
        assert role["runtime_context_size"] == 8192


class TestOverridesMergeBeforeTheCap:
    """P2: the cap decided which definition won, not just how many."""

    def test_a_late_override_still_replaces_a_shipped_case(
        self, tmp_path, monkeypatch
    ):
        for i in range(me.MAX_CASES - 3):
            (tmp_path / f"a{i:04d}.json").write_text(
                json.dumps({"id": f"aaa{i:04d}", "prompt": "p"}),
                encoding="utf-8",
            )
        (tmp_path / "zz.json").write_text(
            json.dumps({"id": "readonly-git-helper", "prompt": "OVERRIDDEN"}),
            encoding="utf-8",
        )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        cases = {c.id: c for c in me.load_cases()[0]}
        assert cases["readonly-git-helper"].prompt == "OVERRIDDEN"

    def test_the_total_is_still_capped(self, tmp_path, monkeypatch):
        for i in range(me.MAX_CASES + 20):
            (tmp_path / f"c{i:04d}.json").write_text(
                json.dumps({"id": f"ccc{i:04d}", "prompt": "p"}),
                encoding="utf-8",
            )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        cases, problems = me.load_cases()
        assert len(cases) == me.MAX_CASES
        assert any("only the first" in p for p in problems)


# ── Round 9 ─────────────────────────────────────────────────────────
#
# Four findings the owner asked to be closed as *classes* rather than as
# the literal examples that exposed them.


class TestMustMatchCannotStallTheWorker:
    """P1: ``re.search`` on an operator pattern had no bound of any kind.

    Python's engine backtracks and cannot be interrupted — the match is
    one C call holding the GIL, so a runaway cannot be joined, cancelled
    or signalled. ``(a+)+$`` against input that almost matches takes
    exponential time, and an evaluation worker that entered one was gone
    for the life of the process.
    """

    CATASTROPHIC = (
        r"(a+)+$",
        r"(a*)*b",
        r"(a|aa)+$",
        r"(x+x+)+y",
        r"([a-zA-Z]+)*$",
        r"(\w+\s?)*$",
    )

    #: Ordinary constraint patterns, including every shape the shipped
    #: cases use. A screen that refused these would be useless.
    SAFE = (
        r"def\s+fetch_status",
        r"^\s*class\s+\w+",
        r"\d+\.\d+",
        r"[a-z]+_[a-z]+",
        r"foo|bar",
        r".*error.*",
        r"\bTODO\b",
        r"a{2,5}",
        r"\w+@\w+\.\w+",
    )

    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_catastrophic_patterns_are_refused_at_case_load(
        self, pattern, tmp_path, monkeypatch
    ):
        (tmp_path / "c.json").write_text(
            json.dumps({
                "id": "regex-bomb",
                "prompt": "p",
                "constraints": [{"kind": "must_match", "value": pattern}],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        cases, problems = me.load_cases()
        # The shipped cases still load; the bomb does not.
        assert "regex-bomb" not in {c.id for c in cases}
        assert any("unsafe regex" in p for p in problems)

    @pytest.mark.parametrize("pattern", SAFE)
    def test_ordinary_patterns_still_load_and_run(self, pattern):
        assert me.unsafe_regex_reason(pattern) is None
        constraint = me.Constraint(kind=me.CONSTRAINT_MUST_MATCH, value=pattern)
        passed, detail = constraint.check("nothing here")
        assert "refused" not in detail

    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_a_direct_constraint_fails_closed_without_raising(self, pattern):
        """The dangerous path is closed, not merely the documented one.

        ``Constraint`` can be built without going through case loading,
        and ``check`` promises never to raise — so it re-screens and
        refuses rather than trusting that a caller validated first.
        """
        constraint = me.Constraint(kind=me.CONSTRAINT_MUST_MATCH, value=pattern)
        started = time.monotonic()
        passed, detail = constraint.check("a" * 64 + "!")
        assert passed is False
        assert "refused" in detail
        # The refusal is structural, so it is instant. The unguarded
        # search on this input does not finish at all.
        assert time.monotonic() - started < 1.0

    def test_backreferences_are_refused(self):
        assert me.unsafe_regex_reason(r"(.*)\1") is not None

    def test_an_invalid_regex_is_still_refused_as_a_regex(self, tmp_path,
                                                          monkeypatch):
        (tmp_path / "c.json").write_text(
            json.dumps({
                "id": "bad-regex",
                "prompt": "p",
                "constraints": [{"kind": "must_match", "value": "("}],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        cases, problems = me.load_cases()
        assert "bad-regex" not in {c.id for c in cases}
        assert any("regex" in p for p in problems)

    def test_disjoint_adjacent_repeats_are_not_confused_for_overlap(self):
        """``\\s+\\w+`` cannot backtrack — the classes share no character.

        A blunt "two repetitions in a row" rule rejected this, which is
        the single most common shape in a real constraint.
        """
        assert me.unsafe_regex_reason(r"\s+\w+") is None
        assert me.unsafe_regex_reason(r"\d+[a-z]+") is None
        # ...but overlapping ones still are.
        assert me.unsafe_regex_reason(r"\w+\w*") is not None
        assert me.unsafe_regex_reason(r".*.*x") is not None


class TestFencedCodeBlockNeedsBothFences:
    """P2: a lone ``` scored as "the model produced code".

    That is what a truncated or malformed answer looks like, so the
    constraint was passing exactly the outputs it existed to catch.
    """

    def _check(self, text):
        return me.Constraint(
            kind=me.CONSTRAINT_MUST_INCLUDE_CODE_BLOCK
        ).check(text)[0]

    def test_a_complete_block_passes(self):
        assert self._check("here:\n```\nx = 1\n```\ndone") is True

    def test_a_language_tag_is_allowed(self):
        assert self._check("```python\nx = 1\n```") is True

    def test_a_lone_opening_fence_is_refused(self):
        assert self._check("here is the fix:\n```python\nx = 1") is False

    def test_a_single_stray_marker_is_refused(self):
        assert self._check("```") is False

    def test_ordinary_prose_is_refused(self):
        assert self._check("Just change the timeout to 30 seconds.") is False


class TestOllamaMetadataStaysWithOllama:
    """P2: ``/api/show`` was gated on "not a single-model backend".

    That is a different question from "is Ollama serving this". A mock,
    a remote OpenAI-compatible server, or any future name-routing
    provider all select by name, so the old gate let them inherit the
    numbers of whatever an unrelated Ollama daemon happened to hold
    under the same label.
    """

    @pytest.fixture
    def health_with_provider(self, monkeypatch):
        from core import ollama_client

        def _apply(provider_name):
            calls = []

            class _Probe:
                def __call__(self, name=None):
                    return {
                        "ok": True,
                        "provider": provider_name,
                        "models": ["gemma3:1b"],
                        "detail": "",
                    }

            class _P:
                name = provider_name

            def _show(name, host=None):
                calls.append(name)
                return {"model_info": {"llama.context_length": 131072},
                        "parameters": "num_ctx 8192"}

            monkeypatch.setattr(
                "core.provider_status.probe_provider_health", _Probe()
            )
            monkeypatch.setattr(
                "core.model_providers.get_provider", lambda: _P()
            )
            monkeypatch.setattr(ollama_client, "show_model", _show)
            monkeypatch.setattr(
                ollama_client, "list_running_models", lambda: []
            )
            monkeypatch.setattr(
                "core.model_profiles.configured_role_models",
                lambda: {"router": "gemma3:1b", "chat": "gemma3:1b",
                         "code": "gemma3:1b", "advanced": "gemma3:1b"},
            )
            return calls
        return _apply

    def test_ollama_metadata_is_read_when_ollama_is_the_provider(
        self, health_with_provider
    ):
        calls = health_with_provider("ollama")
        health = mh.get_model_health()
        assert calls, "/api/show should be consulted for an Ollama backend"
        router = next(r for r in health["roles"] if r["role"] == "router")
        assert router["runtime_context_size"] == 8192
        assert router["context_capacity"] == 131072

    def test_a_non_ollama_provider_never_inherits_ollama_metadata(
        self, health_with_provider
    ):
        """A reachable daemon is not evidence that it is the one answering."""
        calls = health_with_provider("mock")
        health = mh.get_model_health()
        assert calls == [], "/api/show must not be asked about another backend"
        for role in health["roles"]:
            assert role["runtime_context_size"] is None
            assert role["context_capacity"] is None


# ── Round 10 ────────────────────────────────────────────────────────
#
# Three of these are round 9's own fixes not going far enough, which is
# the same shape as rounds 3, 7 and 8: the screen bounded the wrong
# property, the fence counter counted markers instead of roles.


class TestBoundedRepeatsAreScreenedToo:
    r"""P1: the screen counted ``*``/``+`` and let ``a?`` through.

    "Open-ended" was the wrong property to bound. A *bounded* repeat is
    just as ambiguous when its length can vary: every ``a?`` is an
    independent match-or-skip decision, so ``^a?a?…a{30}$`` is 2**30
    assignments to work through before failure can be reported — inside
    the 500-character value cap, and just as unkillable as ``(a+)+$``.
    """

    #: 92 characters, well under ``MAX_CONSTRAINT_VALUE_LEN``.
    BOMB = "^" + "a?" * 30 + "a" * 30 + "$"

    def test_the_bounded_bomb_is_refused(self):
        assert me.unsafe_regex_reason(self.BOMB) is not None

    def test_it_fails_closed_instantly_rather_than_running(self):
        constraint = me.Constraint(
            kind=me.CONSTRAINT_MUST_MATCH, value=self.BOMB
        )
        started = time.monotonic()
        passed, detail = constraint.check("a" * 30 + "!")
        assert passed is False
        assert "refused" in detail
        # Unscreened, this input does not finish at all.
        assert time.monotonic() - started < 1.0

    def test_a_long_chain_of_optionals_is_refused(self):
        """Even when neighbours are disjoint, the chain is the problem."""
        assert me.unsafe_regex_reason("^" + "a?b?" * 20 + "c$") is not None

    def test_a_fixed_repeat_is_not_elastic(self):
        """``a{3}`` cannot give ground, so it is not ambiguous."""
        assert me.unsafe_regex_reason(r"^a{3}b{4}c{5}$") is None

    @pytest.mark.parametrize("pattern", (
        r"def\s+fetch_status",
        r"^\s*class\s+\w+",
        r"^\s*def\s+\w+\s*\(",
        r"\w+@\w+\.\w+",
        r"\d+\.\d+",
        r"a{2,5}",
    ))
    def test_real_constraints_are_unaffected(self, pattern):
        assert me.unsafe_regex_reason(pattern) is None

    def test_the_shipped_cases_still_load(self):
        cases, problems = me.load_cases()
        assert {c.id for c in cases} >= {"bugfix-http-timeout"}
        assert not problems


class TestAClosingFenceCannotCarryALanguage:
    """P2: counting markers passed two *opening* fences.

    ```` ```python ```` followed by ```` ```javascript ```` closes
    nothing — it is what a model that keeps restarting its answer emits.
    Markdown says an opening fence may carry an info string and a
    closing one may not, so the two roles are distinguishable.
    """

    def _check(self, text):
        return me.Constraint(
            kind=me.CONSTRAINT_MUST_INCLUDE_CODE_BLOCK
        ).check(text)[0]

    def test_two_opening_fences_do_not_count_as_a_block(self):
        assert self._check("```python\nx = 1\n```javascript\ny = 2") is False

    def test_a_real_block_still_passes(self):
        assert self._check("```python\nx = 1\n```") is True

    def test_a_bare_opening_fence_and_a_bare_close_pass(self):
        assert self._check("```\nx = 1\n```") is True

    def test_a_second_block_after_a_closed_one_still_passes(self):
        assert self._check(
            "```python\na\n```\nprose\n```js\nb\n```"
        ) is True

    def test_a_lone_opener_is_still_refused(self):
        assert self._check("```python\nx = 1") is False


class TestEmptyConstraintValuesAreRejected:
    """P2: an empty value is a broken constraint, not a lax one.

    ``"" in text`` holds for every response and ``re.search("")`` matches
    at position zero, so a case missing a ``value`` reported
    ``all_passed`` regardless of what the model said — corrupting model
    comparisons, and able to promote a worthless answer into an approved
    training example.
    """

    @pytest.mark.parametrize("kind", sorted(me.VALUE_BEARING_KINDS))
    def test_a_missing_value_is_refused(self, kind):
        with pytest.raises(me.EvalError):
            me._coerce_constraint({"kind": kind})

    @pytest.mark.parametrize("kind", sorted(me.VALUE_BEARING_KINDS))
    def test_a_whitespace_only_value_is_refused(self, kind):
        with pytest.raises(me.EvalError):
            me._coerce_constraint({"kind": kind, "value": "   "})

    def test_a_valueless_kind_is_still_allowed(self):
        """``must_include_code_block`` carries its assertion in the kind."""
        constraint = me._coerce_constraint(
            {"kind": me.CONSTRAINT_MUST_INCLUDE_CODE_BLOCK}
        )
        assert constraint.value == ""

    def test_a_case_with_an_empty_value_does_not_load(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "c.json").write_text(
            json.dumps({
                "id": "vacuous",
                "prompt": "p",
                "constraints": [{"kind": "must_contain", "value": ""}],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        cases, problems = me.load_cases()
        assert "vacuous" not in {c.id for c in cases}
        assert any("non-empty" in p for p in problems)


class TestDatasetExportIsPrivateOnDisk:
    """P2: the export was created 0644 under the usual 022 umask.

    Every local account on a shared host could then read the prompts and
    completions an operator approved. The credential scan does not make
    that safe — it looks for secret-shaped strings, while the export
    exists to carry proprietary task and code content.
    """

    @pytest.fixture
    def one_example(self, monkeypatch):
        """Stand in for the DB read; this test is about the file, not SQL."""
        example = cd.DatasetExample(
            prompt="explain the timeout",
            completion="Use a timeout= argument.",
            metadata={"model": "m", "case_id": "c", "result_id": "1"},
        )
        monkeypatch.setattr(
            cd, "build_examples", lambda ids, db_path=None: [example]
        )
        return example

    def test_the_file_is_created_readable_only_by_its_owner(
        self, tmp_path, monkeypatch, one_example
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path)
        info = cd.export_jsonl([1], "out.jsonl")
        mode = os.stat(info["path"]).st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {mode:o}"

    def test_the_mode_does_not_depend_on_the_operator_umask(
        self, tmp_path, monkeypatch, one_example
    ):
        """A chmod after the fact would leave the file briefly world-readable."""
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path)
        previous = os.umask(0o000)
        try:
            info = cd.export_jsonl([1], "permissive.jsonl")
        finally:
            os.umask(previous)
        assert os.stat(info["path"]).st_mode & 0o777 == 0o600

    def test_the_content_is_still_written(
        self, tmp_path, monkeypatch, one_example
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path)
        info = cd.export_jsonl([1], "content.jsonl")
        lines = pathlib.Path(info["path"]).read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["messages"][0]["content"] == (
            "explain the timeout"
        )

    def test_an_existing_name_is_still_refused(
        self, tmp_path, monkeypatch, one_example
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path)
        (tmp_path / "taken.jsonl").write_text("x", encoding="utf-8")
        with pytest.raises(cd.DatasetExportError):
            cd.export_jsonl([1], "taken.jsonl")
