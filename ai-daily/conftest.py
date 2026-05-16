"""Pytest configuration -- ensures the project root is importable so tests
can `import common` and `from ingest import ...` regardless of invocation cwd.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
