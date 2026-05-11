"""
Tests for the hardened systemd unit shipped at ``deploy/systemd/nova.service``.

The unit file is text, not Python, but the safety story relies on a
specific set of directives staying present. A regression here — a
silent edit that drops ``NoNewPrivileges`` or relaxes the syscall
filter — would not show up in any application test, so this file
parses the unit and asserts the hardening contract.

We do not run ``systemd-analyze``; that requires systemd on the host
and would fail on macOS / minimal CI workers. The format the unit
file uses is simple (``Key=Value`` lines, section headers in
brackets, ``#`` comments), so we parse it inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# Two units ship in this directory: the system-level Nova service and
# the optional user-level SilentGuard read-only API. Both are covered.
REPO_ROOT = Path(__file__).resolve().parents[1]
NOVA_UNIT = REPO_ROOT / "deploy" / "systemd" / "nova.service"
SILENTGUARD_UNIT = REPO_ROOT / "deploy" / "systemd" / "silentguard-api.service"


def _parse_unit(path: Path) -> dict[str, list[str]]:
    """Return a ``{key: [values...]}`` map of unit-file directives.

    Many systemd directives are *additive* — declaring the same key
    twice does not overwrite, it appends (e.g. ``SystemCallFilter``
    allow-list + denylist on two separate lines). We therefore preserve
    every occurrence in declaration order so a test can assert that
    both the allow group and the denylist are present.
    """
    out: dict[str, list[str]] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(key.strip(), []).append(value.strip())
    return out


@pytest.fixture(scope="module")
def nova_unit() -> dict[str, list[str]]:
    assert NOVA_UNIT.is_file(), f"missing unit file: {NOVA_UNIT}"
    return _parse_unit(NOVA_UNIT)


@pytest.fixture(scope="module")
def silentguard_unit() -> dict[str, list[str]]:
    assert SILENTGUARD_UNIT.is_file(), f"missing unit file: {SILENTGUARD_UNIT}"
    return _parse_unit(SILENTGUARD_UNIT)


# ── nova.service — system-level hardening ──────────────────────────


class TestNovaServiceHardening:
    """Each test pins one of the hardening directives.

    The directive set mirrors the documented contract in
    ``deploy/systemd/README.md`` and ``docs/secure-deployment.md``.
    Adding a new directive is fine; removing one needs a deliberate
    edit here and a justification in the PR.
    """

    def test_no_new_privileges(self, nova_unit):
        assert nova_unit.get("NoNewPrivileges") == ["true"]

    def test_private_tmp(self, nova_unit):
        assert nova_unit.get("PrivateTmp") == ["true"]

    def test_protect_system_strict(self, nova_unit):
        assert nova_unit.get("ProtectSystem") == ["strict"]

    def test_protect_home_read_only(self, nova_unit):
        # Nova reads its checkout but should not write into anyone's
        # home outside ReadWritePaths=.
        assert nova_unit.get("ProtectHome") == ["read-only"]

    def test_capabilities_dropped(self, nova_unit):
        # Empty strings — both must be present so systemd actively
        # drops capabilities instead of inheriting defaults.
        assert nova_unit.get("CapabilityBoundingSet") == [""]
        assert nova_unit.get("AmbientCapabilities") == [""]

    def test_restrict_address_families_lan_only(self, nova_unit):
        families = nova_unit.get("RestrictAddressFamilies", [])
        assert len(families) == 1
        chosen = families[0].split()
        # Nova needs IPv4, IPv6 (for outbound HTTPS) and AF_UNIX (Python
        # internals). Anything else — raw sockets, netlink, packet —
        # should remain blocked.
        assert set(chosen) == {"AF_INET", "AF_INET6", "AF_UNIX"}

    def test_lock_personality(self, nova_unit):
        assert nova_unit.get("LockPersonality") == ["true"]

    def test_memory_deny_write_execute(self, nova_unit):
        assert nova_unit.get("MemoryDenyWriteExecute") == ["true"]

    def test_system_call_architectures_native(self, nova_unit):
        assert nova_unit.get("SystemCallArchitectures") == ["native"]

    def test_restrict_suid_sgid(self, nova_unit):
        assert nova_unit.get("RestrictSUIDSGID") == ["true"]

    def test_protect_kernel_tunables(self, nova_unit):
        assert nova_unit.get("ProtectKernelTunables") == ["true"]

    def test_protect_kernel_modules(self, nova_unit):
        assert nova_unit.get("ProtectKernelModules") == ["true"]

    def test_protect_kernel_logs(self, nova_unit):
        # Locks dmesg / /dev/kmsg — Nova never reads kernel messages.
        assert nova_unit.get("ProtectKernelLogs") == ["true"]

    def test_protect_control_groups(self, nova_unit):
        assert nova_unit.get("ProtectControlGroups") == ["true"]

    # ── New directives added by the hardening PR ────────────────────

    def test_private_devices(self, nova_unit):
        assert nova_unit.get("PrivateDevices") == ["true"]

    def test_restrict_namespaces(self, nova_unit):
        assert nova_unit.get("RestrictNamespaces") == ["true"]

    def test_remove_ipc(self, nova_unit):
        assert nova_unit.get("RemoveIPC") == ["true"]

    def test_protect_proc_invisible(self, nova_unit):
        assert nova_unit.get("ProtectProc") == ["invisible"]

    def test_proc_subset_pid(self, nova_unit):
        assert nova_unit.get("ProcSubset") == ["pid"]

    def test_protect_clock(self, nova_unit):
        assert nova_unit.get("ProtectClock") == ["true"]

    def test_restrict_realtime(self, nova_unit):
        assert nova_unit.get("RestrictRealtime") == ["true"]

    def test_protect_hostname(self, nova_unit):
        assert nova_unit.get("ProtectHostname") == ["true"]

    def test_umask_owner_only(self, nova_unit):
        # 0077 — group and world bits stripped so nova.db and its
        # backups are owner-readable only.
        assert nova_unit.get("UMask") == ["0077"]

    def test_system_call_filter_allows_baseline_and_denies_risk_groups(
        self, nova_unit,
    ):
        filters = nova_unit.get("SystemCallFilter", [])
        # Expect at least one allow line and one denylist line.
        assert filters, "SystemCallFilter must be configured"
        joined = " ".join(filters)
        # Baseline allow group.
        assert "@system-service" in joined
        # Each denylist marker we care about.
        for forbidden in (
            "@debug",
            "@mount",
            "@swap",
            "@reboot",
            "@raw-io",
            "@cpu-emulation",
            "@obsolete",
        ):
            assert forbidden in joined, (
                f"SystemCallFilter must deny {forbidden!r}; "
                f"current value: {filters!r}"
            )
        # The denylist must use the '~' inversion prefix on at least
        # one of the filter lines — otherwise the entries would expand
        # the allow-list instead of restricting it.
        assert any(entry.startswith("~") for entry in filters), (
            "Expected at least one SystemCallFilter= line to start with "
            "'~' so it acts as a denylist on top of the allow set."
        )

    def test_system_call_error_number_is_eperm(self, nova_unit):
        # Returning EPERM keeps a filtered syscall an application
        # error rather than killing the process — the read-aloud
        # flow stays alive even if a dependency tries something weird.
        assert nova_unit.get("SystemCallErrorNumber") == ["EPERM"]

    def test_runs_as_unprivileged_user(self, nova_unit):
        user = (nova_unit.get("User") or [None])[0]
        group = (nova_unit.get("Group") or [None])[0]
        assert user, "User= must be set"
        assert group, "Group= must be set"
        # The example unit ships with the literal placeholder
        # USERNAME — anything else (e.g. someone copied the unit
        # without editing) is also acceptable, but root is not.
        assert user.lower() != "root"
        assert group.lower() != "root"

    def test_does_not_grant_privilege_escalation(self, nova_unit):
        # A handful of directives that, if ever flipped on, would
        # silently break the safety contract. The test states the
        # contract: each one MUST stay absent or false.
        forbidden_keys = (
            "PermissionsStartOnly",
            "SupplementaryGroups",
        )
        for key in forbidden_keys:
            # Either absent entirely, or explicitly empty.
            values = nova_unit.get(key, [])
            assert not values or all(v == "" for v in values), (
                f"Directive {key} should not be set in the hardened unit"
            )

    def test_no_shell_in_exec_start(self, nova_unit):
        # ExecStart= must be a plain argv (no shell pipeline), so
        # there's no shell to coerce. systemd does not interpret a
        # shell unless `/bin/sh -c` is explicit.
        exec_start = nova_unit.get("ExecStart", [])
        assert exec_start, "ExecStart= must be set"
        for line in exec_start:
            assert "/bin/sh" not in line
            assert "|" not in line
            assert ";" not in line
            assert "$(" not in line


# ── silentguard-api.service — optional user-level unit ─────────────


class TestSilentGuardUserUnit:
    """The companion read-only SilentGuard API unit must stay safe.

    Nova does not own SilentGuard, but it ships the example unit, so a
    minimum safety bar applies here too. None of these directives
    require root; they are all valid for ``systemctl --user``.
    """

    def test_loopback_only_address_families(self, silentguard_unit):
        families = silentguard_unit.get("RestrictAddressFamilies", [])
        assert families and families[0]
        assert set(families[0].split()) == {"AF_INET", "AF_INET6", "AF_UNIX"}

    def test_no_new_privileges(self, silentguard_unit):
        assert silentguard_unit.get("NoNewPrivileges") == ["true"]

    def test_exec_start_binds_loopback(self, silentguard_unit):
        exec_start = silentguard_unit.get("ExecStart") or []
        assert exec_start, "ExecStart= must be set"
        # The example argv must keep ``--host 127.0.0.1`` so a
        # casual copy-paste does not expose the read-only API on a
        # non-loopback interface.
        assert "127.0.0.1" in exec_start[0], (
            "Example SilentGuard unit must bind --host 127.0.0.1; "
            f"got: {exec_start!r}"
        )

    def test_read_only_flag_present(self, silentguard_unit):
        exec_start = silentguard_unit.get("ExecStart") or []
        assert exec_start
        assert "--read-only" in exec_start[0], (
            "Example SilentGuard unit must include --read-only so the "
            "integration contract (read-only only) holds."
        )

    def test_runs_under_default_target(self, silentguard_unit):
        # User units belong under default.target; using
        # multi-user.target would imply a system-level install path
        # that the docs explicitly forbid.
        assert silentguard_unit.get("WantedBy") == ["default.target"]
