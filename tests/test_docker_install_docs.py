"""Contract tests for the Docker-first install experience.

These pin the polish layer added on top of the zero-setup Docker stack:
a root ``INSTALL.md`` (Docker as the recommended user install path, with
from-source kept for contributors), a Docker Desktop guide, a strictly
read-only ``scripts/docker-doctor.sh`` diagnostic, and a hardened
``.dockerignore`` that keeps secrets/junk out of the build context
without dropping anything the image needs.

Files are read as **plain text** so the suite has no extra dependencies,
matching ``tests/test_docker_stack.py`` and ``tests/test_systemd_unit.py``.
Each assertion encodes a requirement of the install experience, not an
incidental formatting choice.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _uncommented_lines(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ── INSTALL.md ──────────────────────────────────────────────────────


class TestInstallGuide:
    PATH = "INSTALL.md"

    def test_exists_at_repo_root(self):
        assert (_REPO_ROOT / self.PATH).is_file()

    def test_recommends_docker_and_prebuilt_image(self):
        # Docker is the official user install path, and the prebuilt GHCR
        # image is the recommended flavour of it.
        body = _read(self.PATH)
        assert "recommended" in body.lower()
        assert "docker compose -f docker-compose.ghcr.yml up -d" in body

    def test_keeps_the_from_source_contributor_path(self):
        # Nova must not become Docker-only: the guide keeps the
        # build-from-source stack and points contributors at the local
        # Python setup in the README.
        body = _read(self.PATH)
        assert "git clone" in body
        assert "docker compose up -d" in body
        assert "README.md#running-locally" in body

    def test_documents_both_persistent_volumes(self):
        # Users must be able to tell what would be lost if the volumes
        # were removed: nova-data holds the database (memory,
        # conversations, settings) + logs/exports; ollama-models holds
        # the downloaded models.
        body = _read(self.PATH)
        assert "`nova-data`" in body
        assert "`ollama-models`" in body
        for word in ("memory", "conversations", "settings", "logs", "exports"):
            assert word in body.lower()

    def test_warns_that_down_v_deletes_data(self):
        body = _read(self.PATH)
        assert "docker compose down -v" in body
        assert "DELETES" in body

    def test_gives_update_commands_for_both_paths(self):
        body = _read(self.PATH)
        # Prebuilt: pull the newer image, then recreate.
        assert "docker compose -f docker-compose.ghcr.yml pull" in body
        # From source: update the checkout, then rebuild.
        assert "git pull" in body
        assert "docker compose up -d --build" in body

    def test_names_default_login_and_tells_users_to_change_it(self):
        body = _read(self.PATH)
        assert "admin" in body
        assert "changeme" in body
        assert "localhost" in body  # "...before exposing beyond localhost"
        assert "NOVA_PASSWORD" in body

    def test_credential_seeding_is_first_start_only(self):
        # NOVA_USERNAME/NOVA_PASSWORD are consumed only when the account
        # is created in the empty database (core/users.py migrate seeds
        # once). The guide must not claim that editing .env later changes
        # the login; the post-install path is the admin Users panel.
        body = _read(self.PATH)
        assert "first start" in body.lower()
        assert "reset password" in body.lower()

    def test_volume_sharing_caveat_names_the_project_directory(self):
        # Compose prefixes volume names with the project (directory)
        # name, so "switch without losing data" only holds from the same
        # directory — the guide must say so.
        assert "same directory" in _read(self.PATH)


# ── docs/docker-desktop.md ──────────────────────────────────────────


class TestDockerDesktopGuide:
    PATH = "docs/docker-desktop.md"

    def test_exists(self):
        assert (_REPO_ROOT / self.PATH).is_file()

    def test_states_the_compose_creates_desktop_manages_rule(self):
        body = _read(self.PATH)
        assert "Use Docker Compose to create the stack" in body
        assert "monitor" in body.lower()

    def test_explains_why_run_on_the_image_is_not_supported(self):
        # Clicking "Run" on ghcr.io/thezupzup/nova skips the bundled
        # Ollama, the volumes, the ports, and the stack wiring — all four
        # reasons must be spelled out.
        body = _read(self.PATH)
        assert "ghcr.io/thezupzup/nova" in body
        assert "Ollama service" in body
        assert "volumes" in body
        assert "ports" in body
        assert "Compose stack" in body

    def test_shows_expected_containers_images_and_volumes(self):
        body = _read(self.PATH)
        for expected in (
            "`nova`",
            "`nova-ollama`",
            "ghcr.io/thezupzup/nova",
            "`nova:local`",
            "ollama/ollama",
            "nova-data",
            "ollama-models",
        ):
            assert expected in body, f"Desktop guide must mention {expected!r}"

    def test_shows_the_healthy_uvicorn_log_line(self):
        assert "Uvicorn running on http://0.0.0.0:8000" in _read(self.PATH)

    def test_documents_the_linux_sign_in_fallback(self):
        body = _read(self.PATH)
        assert "docker login" in body
        assert "docker login ghcr.io" in body

    def test_documents_safe_vs_destructive_reset(self):
        body = _read(self.PATH)
        # Safe: down (keep volumes) then up.
        assert "docker compose down" in body
        assert "docker compose up -d" in body
        # Destructive: down -v, clearly flagged as deleting data.
        assert "docker compose down -v" in body
        assert "delete" in body.lower()


# ── scripts/docker-doctor.sh ────────────────────────────────────────


class TestDockerDoctor:
    PATH = "scripts/docker-doctor.sh"

    def test_exists_and_is_executable(self):
        path = _REPO_ROOT / self.PATH
        assert path.is_file()
        assert os.access(path, os.X_OK), "docker-doctor.sh must be executable"

    def test_is_posix_shell(self):
        # POSIX sh, not bash — the helper must run on minimal systems
        # (NAS boxes, alpine, dash-as-sh distros).
        body = _read(self.PATH)
        assert body.startswith("#!/bin/sh\n")
        assert "set -u" in body

    def test_path_resolution_is_cdpath_safe(self):
        # An exported CDPATH makes POSIX `cd` echo the resolved path,
        # corrupting `$(cd ... && pwd)` substitutions and breaking the
        # run-from-anywhere fallback. The cd calls must neutralise it.
        assert "CDPATH= cd" in _read(self.PATH)

    def test_supports_compose_file_selection_via_env_var(self):
        # NOVA_COMPOSE_FILES=a.yml:b.yml selects the stack to inspect,
        # defaulting to the build-from-source stack.
        body = _read(self.PATH)
        assert "NOVA_COMPOSE_FILES" in body
        assert "docker-compose.yml" in body

    def test_runs_the_documented_checks(self):
        body = _read(self.PATH)
        # docker CLI present, daemon reachable, compose plugin present,
        # config renders, containers visible, volumes present, port hint.
        assert "command -v docker" in body
        assert "docker info" in body
        assert "docker compose version" in body
        assert "config" in body
        assert "docker ps" in body
        assert "docker volume ls" in body
        assert "docker port" in body
        assert "nova-data" in body
        assert "ollama-models" in body
        assert "8000" in body

    def test_volumes_missing_is_a_warning_not_a_failure(self):
        # Fresh installs have no volumes yet; the doctor must say they
        # are created on first start instead of failing.
        assert "created automatically on the first" in _read(self.PATH)

    def test_warns_that_down_v_deletes_volumes(self):
        body = _read(self.PATH)
        assert "down -v" in body
        assert "DELETES" in body

    def test_is_strictly_read_only(self):
        # The doctor diagnoses; it never mutates. Mutating docker/compose
        # verbs may only appear inside comments or printed hint strings
        # (info/warn/fail lines), never as commands.
        body = _read(self.PATH)
        for forbidden in (
            "docker volume rm",
            "docker system prune",
            "docker rm",
            "docker rmi",
            "docker run",
            "docker build",
            "docker pull",
        ):
            assert forbidden not in body, (
                f"docker-doctor.sh must never invoke {forbidden!r}"
            )
        for verb in ("compose down", "up -d", "compose restart"):
            for line in body.splitlines():
                if verb in line:
                    stripped = line.strip()
                    assert stripped.startswith(("#", "info", "warn", "fail")), (
                        f"{verb!r} may only appear in comments or printed "
                        f"hints, found: {line!r}"
                    )


# ── .dockerignore ───────────────────────────────────────────────────


class TestDockerignore:
    PATH = ".dockerignore"

    def test_keeps_secrets_and_databases_out_of_the_image(self):
        # `**/` prefixes matter: unlike .gitignore, a dockerignore glob
        # without one matches only at the context root, and .gitignore's
        # `*.env` catch-all (prod.env, local.env, ...) must be mirrored
        # so no untracked secret file is baked in by `COPY . .`.
        lines = _uncommented_lines(_read(self.PATH))
        for required in (
            "**/.env",
            "**/.env.*",
            "**/*.env",
            "**/*.db",
            "**/*.sqlite",
            "**/*.sqlite3",
        ):
            assert required in lines, f".dockerignore must exclude {required!r}"
        # .env.example is a documented sample and stays available.
        assert "!.env.example" in lines

    def test_excludes_bulky_generated_junk(self):
        lines = _uncommented_lines(_read(self.PATH))
        for required in (
            "node_modules/",
            "build/",
            "dist/",
            "**/*.tar.gz",
            "**/*.gguf",
            "exports/",
            "backups/",
            "memory-packs/",
        ):
            assert required in lines, f".dockerignore must exclude {required!r}"

    def test_excludes_compose_metadata(self):
        # The compose files drive the stack from the host; the image
        # itself never reads them.
        assert "docker-compose*.yml" in _uncommented_lines(_read(self.PATH))

    def test_does_not_exclude_files_the_image_needs(self):
        # Dockerfile does `COPY . .` and then relies on these paths; a
        # careless pattern here would silently break the image.
        # CHANGELOG.md is on the list because core/data_export.py and
        # core/memory_pack.py read it at runtime to stamp exports with
        # the Nova version.
        lines = _uncommented_lines(_read(self.PATH))
        for needed in (
            "core/",
            "core",
            "memory/",
            "memory",
            "static/",
            "static",
            "docker/",
            "docker",
            "docker*",
            "web.py",
            "main.py",
            "config.py",
            "*.py",
            "**/*.py",
            "requirements.txt",
            "CHANGELOG.md",
            "*.md",
            "**/*.md",
        ):
            assert needed not in lines, (
                f".dockerignore must not exclude {needed!r} — the image "
                f"needs it (see Dockerfile / runtime readers)"
            )
