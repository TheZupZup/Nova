"""Local model evaluation harness + the opt-in nova-coder dataset export.

Pins the two rules that make this safe to ship:

  * evaluation **scores text**; it never executes model output, never
    touches a repository, and never downloads a model; and
  * a training-dataset export is **explicit, per-example, and
    operator-approved** — no chat conversation, memory, or system/safety
    prompt can reach it, and nothing is exported automatically.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core import coder_dataset as cd
from core import model_eval as me
from core.model_providers import ERROR_MODEL_MISSING, ModelProviderError


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "eval.db")
    me.migrate(path)
    return path


@pytest.fixture
def case():
    return me.parse_case({
        "id": "demo-case",
        "title": "Demo",
        "prompt": "Write a function.",
        "constraints": [
            {"kind": "must_include_code_block"},
            {"kind": "must_not_contain", "value": "sudo"},
        ],
    })


class _StubProvider:
    def __init__(self, content="", error=None):
        self.content = content
        self.error = error
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error

        class _R:
            content = self.content
            model = request.model

        return _R()


class TestCaseLoading:
    def test_shipped_cases_all_parse(self):
        cases, problems = me.load_cases()
        assert problems == []
        assert len(cases) >= 3
        assert all(c.constraints for c in cases)

    def test_shipped_cases_have_unique_ids(self):
        cases, _ = me.load_cases()
        ids = [c.id for c in cases]
        assert len(ids) == len(set(ids))

    def test_operator_dir_overrides_a_shipped_case(self, tmp_path, monkeypatch):
        shipped = me.load_cases()[0][0]
        override = tmp_path / "x.json"
        override.write_text(json.dumps({
            "id": shipped.id, "prompt": "replaced", "title": "Mine",
        }), encoding="utf-8")
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        found = {c.id: c for c in me.load_cases()[0]}
        assert found[shipped.id].prompt == "replaced"

    def test_bad_case_becomes_a_problem_not_an_exception(self, tmp_path, monkeypatch):
        (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
        (tmp_path / "nolabel.json").write_text(
            json.dumps({"id": "x"}), encoding="utf-8"
        )
        monkeypatch.setenv(me.ENV_CASES_DIR, str(tmp_path))
        _, problems = me.load_cases()
        assert any("broken.json" in p for p in problems)
        assert any("nolabel.json" in p for p in problems)

    @pytest.mark.parametrize("payload,message", [
        ({"prompt": "x"}, "id"),
        ({"id": "a b", "prompt": "x"}, "id"),
        ({"id": "ok", "prompt": ""}, "prompt"),
        ({"id": "ok", "prompt": "x", "constraints": {}}, "constraints"),
        ({"id": "ok", "prompt": "x",
          "constraints": [{"kind": "run_shell"}]}, "kind"),
        ({"id": "ok", "prompt": "x",
          "constraints": [{"kind": "must_match", "value": "("}]}, "regex"),
        ({"id": "ok", "prompt": "x",
          "constraints": [{"kind": "max_chars", "value": "big"}]}, "integer"),
    ])
    def test_invalid_cases_are_refused(self, payload, message):
        with pytest.raises(me.EvalError, match=message):
            me.parse_case(payload)

    def test_no_constraint_kind_can_execute_anything(self):
        for kind in me.CONSTRAINT_KINDS:
            assert not any(
                word in kind
                for word in ("run", "exec", "shell", "apply", "write")
            )


class TestConstraints:
    def test_all_passing_marks_the_case_passed(self, case):
        scored = me.evaluate_output(case, "here:\n```py\nx=1\n```")
        assert scored["all_passed"] is True
        assert scored["constraints_passed"] == 2

    def test_violation_is_reported_per_constraint(self, case):
        scored = me.evaluate_output(case, "just run sudo rm -rf /")
        assert scored["all_passed"] is False
        kinds = {c["kind"]: c["passed"] for c in scored["constraints"]}
        assert kinds["must_include_code_block"] is False
        assert kinds["must_not_contain"] is False

    @pytest.mark.parametrize("kind,value,output,expected", [
        ("must_contain", "timeout", "set a Timeout", True),
        ("must_contain", "timeout", "nothing", False),
        ("must_match", r"def\s+f", "def  f():", True),
        ("must_match", r"def\s+f", "class F:", False),
        ("max_chars", "5", "abc", True),
        ("max_chars", "5", "abcdefgh", False),
        ("min_chars", "5", "abcdefgh", True),
        ("min_chars", "5", "abc", False),
        ("must_mention_file", "core/x.py", "edit core/X.py", True),
    ])
    def test_individual_checks(self, kind, value, output, expected):
        assert me.Constraint(kind, value).check(output)[0] is expected

    def test_output_is_never_executed(self, case):
        # A payload that would be catastrophic if evaluated is just text.
        payload = "__import__('os').system('touch /tmp/nova-eval-pwned')"
        scored = me.evaluate_output(case, payload)
        assert scored["all_passed"] is False
        import os

        assert not os.path.exists("/tmp/nova-eval-pwned")


class TestRunning:
    def test_run_case_records_timing_and_score(self, case, monkeypatch):
        stub = _StubProvider("```py\nx=1\n```")
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        result = me.run_case(case, "qwen2.5-coder:7b")
        assert result["success"] is True
        assert result["model"] == "qwen2.5-coder:7b"
        assert result["context_size"] > 0
        assert result["elapsed_ms"] >= 0
        assert result["error"] == ""

    def test_missing_model_is_a_recorded_failure_not_a_download(
        self, case, monkeypatch
    ):
        stub = _StubProvider(error=ModelProviderError(
            "model not found", kind=ERROR_MODEL_MISSING, model="ghost:1b"
        ))
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        result = me.run_case(case, "ghost:1b")
        assert result["success"] is False
        assert ERROR_MODEL_MISSING in result["error"]

    def test_eval_prompt_carries_no_nova_system_prompt(self, case, monkeypatch):
        stub = _StubProvider("ok")
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        me.run_case(case, "m:1b")
        system = stub.calls[0].messages[0]["content"]
        assert system == me.EVAL_SYSTEM_PROMPT
        assert "Nova" not in system
        assert "IDENTIT" not in system.upper()

    def test_run_persists_results_and_a_summary(self, db, monkeypatch):
        stub = _StubProvider("```py\nx=1\n```")
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        run = me.start_run(
            ["a:1b", "b:7b"], ["readonly-git-helper"], "compare",
            db_path=db, background=False,
        )
        stored = me.get_run(run["id"], db_path=db)
        assert stored["status"] == me.STATUS_DONE
        assert stored["total"] == 2
        assert stored["completed"] == 2
        results = me.list_results(run["id"], db_path=db)
        assert {r["model"] for r in results} == {"a:1b", "b:7b"}
        summary = me.summarize_run(run["id"], db_path=db)
        assert len(summary["models"]) == 2
        assert all("pass_rate" in m for m in summary["models"])

    @pytest.mark.parametrize("models,cases,message", [
        ([], None, "at least one model"),
        ([f"m{i}:1b" for i in range(9)], None, "at most"),
        (["a:1b"], ["nope"], "unknown evaluation case"),
        ([""], None, "non-empty name"),
    ])
    def test_invalid_run_requests_write_nothing(
        self, db, models, cases, message
    ):
        with pytest.raises(me.EvalError, match=message):
            me.start_run(models, cases, db_path=db, background=False)
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT COUNT(*) FROM model_eval_runs"
        ).fetchone()[0] == 0

    def test_migrate_is_idempotent(self, db):
        me.migrate(db)
        me.migrate(db)


class TestHumanReview:
    @pytest.fixture
    def result_id(self, db, monkeypatch):
        stub = _StubProvider("```py\nshell=False\ntimeout=5\n```" + "x" * 200)
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        run = me.start_run(
            ["a:1b"], ["readonly-git-helper"], db_path=db, background=False
        )
        return me.list_results(run["id"], db_path=db)[0]["id"]

    def test_rating_is_recorded_and_clearable(self, db, result_id):
        rated = me.rate_result(result_id, 4, "solid", db_path=db)
        assert rated["human_rating"] == 4
        assert rated["human_note"] == "solid"
        cleared = me.rate_result(result_id, None, db_path=db)
        assert cleared["human_rating"] is None

    @pytest.mark.parametrize("rating", [0, 6, -1, "5", True])
    def test_invalid_ratings_are_refused(self, db, result_id, rating):
        with pytest.raises(me.EvalError):
            me.rate_result(result_id, rating, db_path=db)

    def test_rating_a_missing_result_is_refused(self, db):
        with pytest.raises(me.EvalError, match="not found"):
            me.rate_result(9999, 3, db_path=db)

    def test_results_start_unapproved(self, db, result_id):
        assert me.get_result(result_id, db_path=db)["approved"] is False

    def test_approval_is_explicit_and_reversible(self, db, result_id):
        approved = me.set_result_approval(result_id, True, db_path=db)
        assert approved["approved"] is True
        assert approved["approved_at"]
        withdrawn = me.set_result_approval(result_id, False, db_path=db)
        assert withdrawn["approved"] is False

    def test_a_passing_result_is_not_auto_approved(self, db, result_id):
        # Success is a constraint outcome; approval is a human act.
        assert me.get_result(result_id, db_path=db)["approved"] is False


class TestDatasetExport:
    @pytest.fixture
    def result_id(self, db, monkeypatch):
        stub = _StubProvider(
            "```py\ndef current_branch(p):\n    ...  # shell=False, timeout\n```"
            + " " + "explanation " * 30
        )
        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: stub
        )
        run = me.start_run(
            ["a:1b"], ["readonly-git-helper"], db_path=db, background=False
        )
        return me.list_results(run["id"], db_path=db)[0]["id"]

    def test_unapproved_results_cannot_be_exported(self, db, result_id):
        with pytest.raises(cd.DatasetExportError, match="not been approved"):
            cd.build_examples([result_id], db_path=db)

    def test_approved_result_builds_a_bare_user_assistant_pair(
        self, db, result_id
    ):
        me.set_result_approval(result_id, True, db_path=db)
        examples = cd.build_examples([result_id], db_path=db)
        assert len(examples) == 1
        record = examples[0].as_jsonl_record()
        assert [m["role"] for m in record["messages"]] == ["user", "assistant"]
        assert record["metadata"]["model"] == "a:1b"

    def test_system_prompt_is_never_exported(self, db, result_id):
        me.set_result_approval(result_id, True, db_path=db)
        blob = json.dumps([
            e.as_jsonl_record() for e in cd.build_examples([result_id], db_path=db)
        ])
        assert "system" not in [
            m["role"]
            for e in cd.build_examples([result_id], db_path=db)
            for m in e.as_jsonl_record()["messages"]
        ]
        assert me.EVAL_SYSTEM_PROMPT not in blob

    def test_empty_selection_is_refused(self, db):
        with pytest.raises(cd.DatasetExportError, match="at least one"):
            cd.build_examples([], db_path=db)

    def test_unknown_result_is_refused(self, db):
        with pytest.raises(cd.DatasetExportError, match="does not exist"):
            cd.build_examples([424242], db_path=db)

    def test_non_integer_ids_are_refused(self, db):
        with pytest.raises(cd.DatasetExportError, match="integers"):
            cd.build_examples(["1"], db_path=db)

    @pytest.mark.parametrize("secret", [
        "password=hunter2seven",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN RSA PRIVATE KEY-----",
        "contact me at someone@example.com",
        "eyJhbGciOi.eyJzdWIiOjF9.SflKxwRJSM",
    ])
    def test_secret_shaped_content_refuses_the_whole_export(
        self, db, result_id, secret
    ):
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                "UPDATE model_eval_results SET output = ? WHERE id = ?",
                (f"answer\n{secret}\n", result_id),
            )
        me.set_result_approval(result_id, True, db_path=db)
        with pytest.raises(cd.DatasetExportError, match="credential"):
            cd.build_examples([result_id], db_path=db)

    def test_backend_error_results_are_refused(self, db, result_id):
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                "UPDATE model_eval_results SET error = ? WHERE id = ?",
                ("unreachable", result_id),
            )
        me.set_result_approval(result_id, True, db_path=db)
        with pytest.raises(cd.DatasetExportError, match="backend error"):
            cd.build_examples([result_id], db_path=db)

    def test_export_writes_local_jsonl_and_never_overwrites(
        self, db, result_id, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path / "out")
        me.set_result_approval(result_id, True, db_path=db)
        info = cd.export_jsonl([result_id], "corpus", db_path=db)
        assert info["filename"] == "corpus.jsonl"
        assert info["example_count"] == 1
        lines = (tmp_path / "out" / "corpus.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(lines) == 1
        json.loads(lines[0])
        with pytest.raises(cd.DatasetExportError, match="already exists"):
            cd.export_jsonl([result_id], "corpus", db_path=db)

    @pytest.mark.parametrize("name", [
        "../escape", "a/b", "x" * 200, "na$me", "sub/dir/file.jsonl",
    ])
    def test_bad_filenames_are_refused(
        self, db, result_id, tmp_path, monkeypatch, name
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path / "out")
        me.set_result_approval(result_id, True, db_path=db)
        with pytest.raises(cd.DatasetExportError):
            cd.export_jsonl([result_id], name, db_path=db)

    def test_readiness_reports_counts_never_content(self, db, result_id):
        before = cd.describe_export_readiness(db_path=db)
        assert before["approved_count"] == 0
        assert before["automatic_export"] is False
        me.set_result_approval(result_id, True, db_path=db)
        after = cd.describe_export_readiness(db_path=db)
        assert after["approved_count"] == 1
        assert after["approved_result_ids"] == [result_id]
        assert "output" not in json.dumps(after)

    def test_blank_filename_falls_back_to_a_generated_name(
        self, db, result_id, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cd, "export_dir", lambda: tmp_path / "out")
        me.set_result_approval(result_id, True, db_path=db)
        info = cd.export_jsonl([result_id], "  ", db_path=db)
        assert info["filename"].startswith("nova-coder-")
        assert info["filename"].endswith(".jsonl")

    def test_export_module_cannot_reach_any_other_table(self):
        """No direct DB access: it can only read what model_eval exposes."""
        import core.coder_dataset as module

        assert not hasattr(module, "sqlite3")
        with open(cd.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assert "import sqlite3" not in source
        assert "SELECT" not in source
