"""Pytest conftest: stub heavy / optional deps that the env may not ship.

Several existing test files (e.g. ``test_silentguard_summary_endpoint``)
already stub these modules at import time. Centralising the workaround
here lets suites that touch ``web.py`` collect cleanly even when one of
the optional wheels is missing on the host. The stub is **only**
installed when the real module fails to import — on a normal CI worker
where the package exists, the real module is used and the conftest is
a no-op.
"""

import importlib
import sys
from unittest.mock import MagicMock

# Heavy / optional deps that ``web.py`` and its transitive imports
# pull in. Each is replaced by a ``MagicMock`` only when the genuine
# import fails, so a CI environment with the real package installed
# behaves identically to before this conftest existed.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()
