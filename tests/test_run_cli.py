"""Arg-handling tests for the on-demand job runner (no jobs executed)."""
from oracle.run import main


def test_help_returns_zero():
    assert main(["--help"]) == 0
    assert main([]) == 0


def test_unknown_job_returns_one_without_running():
    # Validated before any DB/network work, so this is safe to assert offline.
    assert main(["not_a_job"]) == 1
