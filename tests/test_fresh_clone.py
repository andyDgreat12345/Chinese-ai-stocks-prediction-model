"""Every research module must survive a database that does not exist yet.

The research phase can run before any ingestion has, and a fresh clone has no
database file at all. These modules previously raised "no such table:
china_close" in that state. The workflow runs them with `|| true`, so the job
would still have gone green while copying a Python traceback into the published
report — the failure would have been invisible in exactly the way this codebase
treats as the primary hazard.
"""
import subprocess
import sys

import pytest

MODULES = [
    "oracle.research.exit_horizon",
    "oracle.research.execution",
    "oracle.research.regimes",
    "oracle.learning.objective",
    "oracle.paper",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_degrades_on_a_nonexistent_database(module, tmp_path):
    db = tmp_path / "definitely-not-created-yet.db"
    assert not db.exists()
    r = subprocess.run([sys.executable, "-m", module],
                       capture_output=True, text=True, timeout=300,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                            "ORACLE_DB": str(db)})
    assert r.returncode == 0, f"{module} failed on a fresh DB:\n{r.stderr[-1500:]}"
    assert "Traceback" not in r.stderr, f"{module} raised:\n{r.stderr[-1500:]}"
    assert r.stdout.strip(), f"{module} produced no report"


def test_settlement_warning_survives_the_fresh_clone_path(tmp_path):
    """The caveat that matters most must print when there is nothing to price."""
    db = tmp_path / "fresh.db"
    r = subprocess.run([sys.executable, "-m", "oracle.research.execution"],
                       capture_output=True, text=True, timeout=300,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                            "ORACLE_DB": str(db)})
    assert r.returncode == 0
    assert "VERIFY THIS WITH THE BROKER" in r.stdout
