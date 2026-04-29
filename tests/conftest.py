"""
Shared test configuration and stubs.

feedparser and ddgs require sgmllib3k which can fail to build in certain
environments (missing system build tools). Since tests always mock the
functions that call these libraries, we never execute them at runtime — we
only need the modules to be importable. These stubs satisfy that requirement
so the full test suite can run locally without a complete build environment.

CI (GitHub Actions / ubuntu-latest) installs the full requirements.txt
successfully and does not need these stubs.
"""

import sys
import types


def _stub_feedparser() -> None:
    """Insert a minimal feedparser stub into sys.modules if the real one is broken."""
    try:
        import feedparser  # noqa: F401
        return
    except (ImportError, ModuleNotFoundError):
        pass

    stub = types.ModuleType("feedparser")
    stub.parse = lambda *a, **kw: types.SimpleNamespace(entries=[])
    sys.modules["feedparser"] = stub


def _stub_ddgs() -> None:
    """Insert a minimal ddgs stub when the package cannot be installed."""
    try:
        import ddgs  # noqa: F401
        return
    except (ImportError, ModuleNotFoundError):
        pass

    stub = types.ModuleType("ddgs")

    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def text(self, *a, **kw):
            return []

    stub.DDGS = _DDGS
    sys.modules["ddgs"] = stub


_stub_feedparser()
_stub_ddgs()
