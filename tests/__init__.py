"""Tests never touch the main database: without AIMO_DATA they get a throwaway folder."""
import os
import tempfile

os.environ.setdefault("AIMO_DATA", tempfile.mkdtemp(prefix="aimo-test-"))
