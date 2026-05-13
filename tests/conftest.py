"""
Shared pytest fixtures for overhired companion tests.
"""
import sys
from pathlib import Path

# Make companion/ importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "companion"))
