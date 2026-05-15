"""
Shared pytest fixtures for grapply companion tests.
"""
import sys
from pathlib import Path

import pytest

# Make companion/ importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that make real network calls (run with -m integration)",
    )
