"""Contract tests for the top-level Docker Compose stack.

These tests pin the behaviour requested for the full Docker deployment:
a self-contained stack (Nova + bundled Ollama) reachable on port 8000,
with every kind of runtime data on a persistent volume and admin/alpha
features left off by default.

The compose / Dockerfile / entrypoint files are read as **plain text** so
the suite does not depend on PyYAML being installed — matching the style
of ``tests/test_paths.py::TestPortableDockerComposeExample`` and
``tests/test_systemd_unit.py``. Each assertion encodes a requirement from
the Docker-based deployment, not an incidental formatting choice.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _uncommented_lines(body: str) -> list[str]:
    """Return stripped, non-comment, non-blank lines.

    Used for negative assertions so a rule that *mentions* something in a
    comment (e.g. "do not expose admin") never trips a check that the
    thing is not actually configured.
    """
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ── docker-compose.yml ──────────────────────────────────────────────


class TestComposeStack:
    def test_compose_file_exists(self):
        assert (_REPO_ROOT / "docker-compose.yml").is_file()

    def test_defines_both_services(self):
        # The stack must run Nova *and* a bundled Ollama, so a user needs
        # neither Python nor Ollama on the host.
        body = _read("docker-compose.yml")
        assert "\n  nova:" in body
        assert "\n  ollama:" in body
        assert "ollama/ollama" in body

    def test_nova_built_from_repo(self):
        # Default path builds the image from this checkout so
        # `docker compose up -d` works without a published image.
        body = _read("docker-compose.yml")
        assert "build:" in body
        assert "context: ." in body

    def test_published_on_port_8000(self):
        # Host-reachable at :8000 (localhost and LAN). Host side is
        # overridable via NOVA_HOST_PORT; container side defaults to 8000.
        body = _read("docker-compose.yml")
        assert "${NOVA_HOST_PORT:-8000}:${NOVA_PORT:-8000}" in body

    def test_data_dir_pinned_to_container_data(self):
        # core/paths.py persists nova.db + subdirs under NOVA_DATA_DIR.
        assert "NOVA_DATA_DIR: /data" in _read("docker-compose.yml")

    def test_defaults_to_bundled_ollama(self):
        # Nova reaches the Ollama container over the compose network, and
        # the value is overridable from .env for an external Ollama.
        body = _read("docker-compose.yml")
        assert "OLLAMA_HOST: ${OLLAMA_HOST:-http://ollama:11434}" in body

    def test_persistent_volumes_for_data_and_models(self):
        # Nova data (db/memory/settings/logs/exports) and Ollama models
        # each live on a named volume — never only in the container layer.
        body = _read("docker-compose.yml")
        assert "nova-data:/data" in body
        assert "ollama-models:/root/.ollama" in body
        # Both named volumes are declared in the top-level volumes block.
        assert "\nvolumes:" in body
        assert "\n  nova-data:" in body
        assert "\n  ollama-models:" in body

    def test_passes_env_file(self):
        # All required environment variables flow in from .env.
        body = _read("docker-compose.yml")
        assert "env_file:" in body
        assert "- .env" in body

    def test_does_not_mount_docker_socket(self):
        assert "docker.sock" not in _read("docker-compose.yml")

    def test_does_not_run_privileged(self):
        body = _read("docker-compose.yml")
        assert "privileged: true" not in body
        assert "cap_add" not in body

    def test_does_not_force_enable_admin_or_alpha(self):
        # Requirement: never expose admin/alpha-only functionality by
        # default. The compose file must not hardcode any of these on.
        joined = "\n".join(_uncommented_lines(_read("docker-compose.yml")))
        for forbidden in (
            'NOVA_ADMIN_UI: "true"',
            "NOVA_ADMIN_UI: true",
            "NOVA_ADMIN_UI=true",
            "NOVA_CHANNEL: alpha",
            "NOVA_CHANNEL=alpha",
            'NOVA_MAINTENANCE_ENABLED: "true"',
            "NOVA_MAINTENANCE_ENABLED: true",
        ):
            assert forbidden not in joined, (
                f"compose must not force-enable admin/alpha: {forbidden!r}"
            )


# ── Dockerfile ──────────────────────────────────────────────────────


class TestDockerfile:
    def test_exposes_8000(self):
        assert "EXPOSE 8000" in _read("Dockerfile")

    def test_persists_under_data_by_default(self):
        assert "NOVA_DATA_DIR=/data" in _read("Dockerfile")

    def test_runs_as_non_root(self):
        assert "USER nova" in _read("Dockerfile")

    def test_no_secrets_or_db_baked(self):
        # The image should not COPY an .env or a database in (comments may
        # mention nova.db, so check only the real instruction lines).
        body = _read("Dockerfile")
        assert "COPY .env" not in body
        assert "nova.db" not in "\n".join(_uncommented_lines(body))


# ── docker/entrypoint.sh ────────────────────────────────────────────


class TestEntrypoint:
    def test_generates_and_persists_secret_key_when_unset(self):
        # No known default key is shipped; one is generated on first run
        # and kept on the data volume so logins survive restarts.
        body = _read("docker/entrypoint.sh")
        assert 'if [ -z "${NOVA_SECRET_KEY:-}" ]' in body
        assert "secret_key" in body
        assert "token_hex" in body
        assert "export NOVA_SECRET_KEY" in body

    def test_ensures_data_dir(self):
        body = _read("docker/entrypoint.sh")
        assert 'DATA_DIR="${NOVA_DATA_DIR:-/data}"' in body
        assert 'mkdir -p "$DATA_DIR"' in body

    def test_execs_final_command(self):
        # tini supervises the real process; signals must propagate.
        assert 'exec "$@"' in _read("docker/entrypoint.sh")
