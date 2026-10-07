"""Import pypylon before anything can pull in numpy — the same best-effort
first import cli.py does in production (see CLAUDE.md: numpy loaded before
pypylon makes a later pypylon call on a worker thread segfault at thread
teardown). Without it the suite only survives by file-collection order: any
test file that imports numpy at module level, collected before the first
octacam.cli import, crashes a later doctor/benchmark test."""

try:
    import pypylon.pylon  # noqa: F401
except Exception:  # pragma: no cover - pypylon is optional at test time
    pass
