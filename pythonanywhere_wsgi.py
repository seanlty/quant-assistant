"""PythonAnywhere WSGI entrypoint for the stock futures dashboard.

In PythonAnywhere's Web tab, set the WSGI file to import `application`
from this module, or paste the equivalent path/bootstrap code there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.web_dashboard import application  # noqa: E402


os.environ.setdefault("DASHBOARD_CACHE_SECONDS", "900")
