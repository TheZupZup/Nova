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


# ── docker-compose.ghcr.yml (prebuilt image) ────────────────────────


class TestGhcrComposeStack:
    """The prebuilt-image stack must mirror docker-compose.yml exactly,
    except it *pulls* the published GHCR image instead of building.

    This is the user-friendly deployment path: deploy Nova without cloning
    or building the repo, while keeping data + models on the same named
    volumes as the build-from-source stack.
    """

    PATH = "docker-compose.ghcr.yml"

    def test_compose_file_exists(self):
        assert (_REPO_ROOT / self.PATH).is_file()

    def test_pulls_prebuilt_ghcr_image(self):
        # Pulls the published image; the tag is overridable via
        # NOVA_IMAGE_TAG and defaults to latest (tracks main).
        body = _read(self.PATH)
        assert "image: ghcr.io/thezupzup/nova:${NOVA_IMAGE_TAG:-latest}" in body

    def test_does_not_build_from_source(self):
        # The whole point of this file is to skip building. No build
        # instruction should appear in the actual configuration (comments
        # may still mention the word "build").
        joined = "\n".join(_uncommented_lines(_read(self.PATH)))
        assert "build:" not in joined
        assert "context:" not in joined

    def test_defines_both_services(self):
        body = _read(self.PATH)
        assert "\n  nova:" in body
        assert "\n  ollama:" in body
        assert "ollama/ollama" in body

    def test_published_on_port_8000(self):
        assert "${NOVA_HOST_PORT:-8000}:${NOVA_PORT:-8000}" in _read(self.PATH)

    def test_data_dir_pinned_to_container_data(self):
        assert "NOVA_DATA_DIR: /data" in _read(self.PATH)

    def test_defaults_to_bundled_ollama(self):
        assert (
            "OLLAMA_HOST: ${OLLAMA_HOST:-http://ollama:11434}"
            in _read(self.PATH)
        )

    def test_shares_persistent_volumes_with_build_stack(self):
        # Same volume names as docker-compose.yml so switching between the
        # build and prebuilt stacks keeps the database and models.
        body = _read(self.PATH)
        assert "nova-data:/data" in body
        assert "ollama-models:/root/.ollama" in body
        assert "\nvolumes:" in body
        assert "\n  nova-data:" in body
        assert "\n  ollama-models:" in body

    def test_passes_env_file(self):
        body = _read(self.PATH)
        assert "env_file:" in body
        assert "- .env" in body

    def test_does_not_mount_docker_socket(self):
        assert "docker.sock" not in _read(self.PATH)

    def test_does_not_run_privileged(self):
        body = _read(self.PATH)
        assert "privileged: true" not in body
        assert "cap_add" not in body

    def test_does_not_force_enable_admin_or_alpha(self):
        joined = "\n".join(_uncommented_lines(_read(self.PATH)))
        for forbidden in (
            'NOVA_ADMIN_UI: "true"',
            "NOVA_ADMIN_UI: true",
            "NOVA_CHANNEL: alpha",
            'NOVA_MAINTENANCE_ENABLED: "true"',
            "NOVA_MAINTENANCE_ENABLED: true",
        ):
            assert forbidden not in joined, (
                f"ghcr compose must not force-enable admin/alpha: {forbidden!r}"
            )


# ── .github/workflows/docker-publish.yml ────────────────────────────


class TestDockerPublishWorkflow:
    """The publish workflow encodes the publishing contract: build on PRs
    for validation, publish to GHCR only on main/tags, authenticate with
    the built-in GITHUB_TOKEN, and never publish from a pull request.
    """

    PATH = ".github/workflows/docker-publish.yml"

    def test_workflow_exists(self):
        assert (_REPO_ROOT / self.PATH).is_file()

    def test_triggers_on_main_tags_and_prs(self):
        body = _read(self.PATH)
        assert 'branches: [ "main" ]' in body
        assert 'tags: [ "v*" ]' in body
        assert "pull_request:" in body

    def test_publishes_to_ghcr(self):
        body = _read(self.PATH)
        assert "REGISTRY: ghcr.io" in body
        assert "IMAGE_NAME: thezupzup/nova" in body

    def test_authenticates_with_github_token(self):
        # GHCR auth uses the automatically provided token — no stored
        # registry password is required.
        assert "password: ${{ secrets.GITHUB_TOKEN }}" in _read(self.PATH)

    def test_requests_packages_write_permission(self):
        assert "packages: write" in _read(self.PATH)

    def test_does_not_publish_from_pull_requests(self):
        # Push is gated on the event not being a pull request, and the
        # registry login is skipped on PRs too — so untrusted forks can
        # only validate the build, never publish.
        body = _read(self.PATH)
        assert "push: ${{ github.event_name != 'pull_request' }}" in body
        assert "if: github.event_name != 'pull_request'" in body

    def test_generates_required_tags(self):
        body = _read(self.PATH)
        # latest tracks main; git SHA always; semver on v* tags.
        assert "type=raw,value=latest,enable={{is_default_branch}}" in body
        assert "type=sha" in body
        assert "type=semver,pattern={{version}}" in body

    def test_no_docker_hub_publishing(self):
        # Requirement: do not introduce Docker Hub publishing.
        body = _read(self.PATH)
        for forbidden in ("docker.io", "DOCKERHUB", "DOCKER_PASSWORD",
                          "registry-1.docker.io"):
            assert forbidden not in body, (
                f"workflow must not add Docker Hub publishing: {forbidden!r}"
            )
