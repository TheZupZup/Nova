"""Wire-level contract for the new admin model-platform endpoints.

Covers ``/admin/models/profiles``, ``/admin/models/health``, and the
``/admin/eval/*`` surface (cases, runs, human rating, approval, and the
opt-in dataset export).

The rules pinned here:

  * every endpoint is admin-only — non-admin, restricted, and
    unauthenticated callers are refused;
  * a run request is validated before anything is written, and a run
    never blocks on or triggers a download;
  * a dataset export requires explicitly-named, explicitly-approved
    results — there is no bulk/automatic path through the API.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import memory as core_memory, model_eval, users  # noqa: E402
from memory import store as natural_store  # noqa: E402


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER,
               is_restricted=False):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(
            conn, username, password, role=role, is_restricted=is_restricted,
        )


@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter

    _login_limiter._store.clear()

    import web

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(client, username, password="pw"):
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(db_path, web_client):
    _make_user(db_path, "alice", role=users.ROLE_ADMIN)
    return _login(web_client, "alice")


@pytest.fixture
def user_token(db_path, web_client):
    _make_user(db_path, "bob")
    return _login(web_client, "bob")


@pytest.fixture
def restricted_token(db_path, web_client):
    _make_user(db_path, "kid", is_restricted=True)
    return _login(web_client, "kid")


_ENDPOINTS = [
    ("GET", "/admin/models/profiles", None),
    ("GET", "/admin/models/health", None),
    ("GET", "/admin/eval/cases", None),
    ("GET", "/admin/eval/runs", None),
    ("GET", "/admin/eval/runs/1", None),
    ("POST", "/admin/eval/runs", {"models": ["a:1b"]}),
    ("POST", "/admin/eval/results/1/rating", {"rating": 3}),
    ("POST", "/admin/eval/results/1/approval", {"approved": True}),
    ("GET", "/admin/eval/dataset", None),
    ("POST", "/admin/eval/dataset/export", {"result_ids": [1]}),
]


class TestAuthGating:
    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_non_admin_forbidden(self, web_client, user_token, method, path, body):
        resp = web_client.request(method, path, json=body, headers=_h(user_token))
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_restricted_forbidden(
        self, web_client, restricted_token, method, path, body
    ):
        resp = web_client.request(
            method, path, json=body, headers=_h(restricted_token)
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_unauthenticated_refused(self, web_client, method, path, body):
        resp = web_client.request(method, path, json=body)
        assert resp.status_code in (401, 403)


class TestProfilesAndHealth:
    def test_profiles_report_every_role(self, web_client, admin_token):
        payload = web_client.get(
            "/admin/models/profiles", headers=_h(admin_token)
        ).json()
        assert [r["role"] for r in payload["roles"]] == [
            "router", "chat", "code", "advanced"
        ]
        code = next(r for r in payload["roles"] if r["role"] == "code")
        assert code["env_var"] == "NOVA_CODE_MODEL"
        assert "context_size" in code["profile"]

    def test_health_reports_an_unreachable_backend_as_data(
        self, web_client, admin_token, monkeypatch
    ):
        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            lambda name=None: {
                "ok": False, "provider": "ollama",
                "detail": "connection refused", "models": [],
            },
        )
        resp = web_client.get("/admin/models/health", headers=_h(admin_token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["reachable"] is False
        assert body["errors"] == ["backend_unreachable"]


class TestEvalEndpoints:
    def test_cases_list_the_shipped_examples(self, web_client, admin_token):
        payload = web_client.get(
            "/admin/eval/cases", headers=_h(admin_token)
        ).json()
        assert payload["problems"] == []
        assert len(payload["cases"]) >= 3
        assert "must_include_code_block" in payload["constraint_kinds"]

    def test_missing_run_is_404(self, web_client, admin_token):
        resp = web_client.get("/admin/eval/runs/9999", headers=_h(admin_token))
        assert resp.status_code == 404

    @pytest.mark.parametrize("body", [
        {"models": []},
        {"models": ["a:1b"], "case_ids": ["nope"]},
        {"models": ["a:1b"], "unexpected": 1},
        {},
    ])
    def test_invalid_run_requests_are_refused(
        self, web_client, admin_token, db_path, body
    ):
        resp = web_client.post(
            "/admin/eval/runs", json=body, headers=_h(admin_token)
        )
        assert resp.status_code in (400, 422)
        conn = sqlite3.connect(db_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM model_eval_runs"
        ).fetchone()[0] == 0

    def _run_one(self, web_client, admin_token, db_path, monkeypatch):
        """Execute one deterministic run inline; return its run id."""
        class _Stub:
            def generate(self, request):
                class _R:
                    content = "```py\nshell=False\ntimeout=1\n```" + "x" * 250
                    model = request.model

                return _R()

        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: _Stub()
        )
        monkeypatch.setattr(model_eval, "_db_path", lambda p=None: db_path)
        real_start = model_eval.start_run
        monkeypatch.setattr(
            model_eval, "start_run",
            lambda *a, **k: real_start(*a, **{**k, "background": False}),
        )

        resp = web_client.post(
            "/admin/eval/runs",
            json={"models": ["a:1b"], "case_ids": ["readonly-git-helper"],
                  "label": "smoke"},
            headers=_h(admin_token),
        )
        assert resp.status_code == 202
        run_id = resp.json()["id"]

        detail = web_client.get(
            f"/admin/eval/runs/{run_id}", headers=_h(admin_token)
        ).json()
        assert detail["run"]["status"] == "done"
        assert len(detail["results"]) == 1
        assert detail["summary"]["models"][0]["model"] == "a:1b"
        return run_id

    def test_run_is_accepted_and_readable(
        self, web_client, admin_token, db_path, monkeypatch
    ):
        self._run_one(web_client, admin_token, db_path, monkeypatch)

    def test_rating_and_approval_flow(
        self, web_client, admin_token, db_path, monkeypatch
    ):
        run_id = self._run_one(
            web_client, admin_token, db_path, monkeypatch
        )
        result_id = web_client.get(
            f"/admin/eval/runs/{run_id}", headers=_h(admin_token)
        ).json()["results"][0]["id"]

        rated = web_client.post(
            f"/admin/eval/results/{result_id}/rating",
            json={"rating": 5, "note": "good"}, headers=_h(admin_token),
        )
        assert rated.status_code == 200
        assert rated.json()["human_rating"] == 5

        bad = web_client.post(
            f"/admin/eval/results/{result_id}/rating",
            json={"rating": 9}, headers=_h(admin_token),
        )
        assert bad.status_code == 422

        readiness = web_client.get(
            "/admin/eval/dataset", headers=_h(admin_token)
        ).json()
        assert readiness["approved_count"] == 0
        assert readiness["automatic_export"] is False

        approved = web_client.post(
            f"/admin/eval/results/{result_id}/approval",
            json={"approved": True}, headers=_h(admin_token),
        )
        assert approved.status_code == 200
        assert approved.json()["approved"] is True

        after = web_client.get(
            "/admin/eval/dataset", headers=_h(admin_token)
        ).json()
        assert after["approved_result_ids"] == [result_id]

    def test_export_refuses_unapproved_results(
        self, web_client, admin_token, db_path, monkeypatch
    ):
        run_id = self._run_one(
            web_client, admin_token, db_path, monkeypatch
        )
        result_id = web_client.get(
            f"/admin/eval/runs/{run_id}", headers=_h(admin_token)
        ).json()["results"][0]["id"]
        resp = web_client.post(
            "/admin/eval/dataset/export",
            json={"result_ids": [result_id]}, headers=_h(admin_token),
        )
        assert resp.status_code == 400
        assert "not been approved" in resp.json()["detail"]

    def test_export_has_no_bulk_form(self, web_client, admin_token):
        # ``result_ids`` is mandatory: there is no "export everything".
        resp = web_client.post(
            "/admin/eval/dataset/export", json={}, headers=_h(admin_token)
        )
        assert resp.status_code == 422
        resp = web_client.post(
            "/admin/eval/dataset/export",
            json={"result_ids": []}, headers=_h(admin_token),
        )
        assert resp.status_code == 422


class TestSettingsModelsCardWiring:
    """The admin card in Settings → Models is wired and stays read-only.

    The card is the only operator-facing surface for the model-platform
    work, so its wiring is pinned here: the markup exists, the handler
    is defined and called on load, it reads exactly one read-only
    endpoint, and it offers no control that could pull, load, generate,
    or restart anything.
    """

    @pytest.fixture
    def page(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / "static" / "index.html").read_text(encoding="utf-8")

    def test_markup_and_handler_exist(self, page):
        for token in (
            'id="settings-model-roles-row"',
            'id="settings-model-roles-summary"',
            'id="settings-model-roles-refresh-btn"',
            "async function loadModelRolesCard()",
            "function renderModelRolesCard(",
        ):
            assert token in page, token

    def test_card_is_loaded_alongside_the_provider_card(self, page):
        assert "loadModelRolesCard().catch(() => {});" in page

    def test_card_reads_only_the_health_endpoint(self, page):
        start = page.index("async function loadModelRolesCard()")
        end = page.index("async function testModelsProviderConnection()")
        body = page[start:end]
        assert '"/admin/models/health"' in body
        # No write verb anywhere in the card's own code.
        assert "method: \"POST\"" not in body
        assert "method: \"PUT\"" not in body
        assert "method: \"DELETE\"" not in body
        for endpoint in ("/admin/models/pull", "/admin/maintenance",
                         "/admin/provider/default-model"):
            assert endpoint not in body

    def test_card_is_hidden_for_non_admins(self, page):
        start = page.index("async function loadModelRolesCard()")
        body = page[start:start + 1200]
        assert 'currentUser.role === "admin"' in body
        assert 'row.style.display = "none"' in body

    def test_card_escapes_every_backend_supplied_value(self, page):
        """Model names and hints come from the backend — never raw.

        A model name is operator-controlled, but a runtime ``hint`` and a
        model tag both reach this card as backend strings, so every
        interpolation must be escaped rather than concatenated raw.
        """
        import re

        start = page.index("function renderModelRolesCard(")
        end = page.index("async function testModelsProviderConnection()")
        body = page[start:end]

        # Every `${...role.<field>...}` interpolation in the card must
        # have escapeHtml applied inside it.
        interpolations = re.findall(r"\$\{([^}]*\brole\.[^}]*)\}", body)
        assert interpolations, "expected the card to render role fields"
        for expr in interpolations:
            assert "escapeHtml(" in expr, f"unescaped interpolation: {expr}"

    def test_unreachable_backend_renders_a_notice_not_a_missing_claim(self, page):
        start = page.index("function renderModelRolesCard(")
        end = page.index("async function testModelsProviderConnection()")
        body = page[start:end]
        assert "health.reachable" in body
        assert "is a claim that a model is missing" in body
