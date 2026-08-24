"""Every research endpoint must actually be rendered somewhere.

This exists because the forward ledger and the segment breakdown were wired
into the API and the static build, and then shipped with no panel rendering
them — published as JSON that nothing on the page ever read. It is the same
failure as a helper that is tested but never called: the unit works, the wiring
does not, and nothing fails loudly enough to notice.
"""
import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "oracle/dashboard/static/app.js"

# Endpoints whose whole purpose is to be read by a person. A data endpoint may
# legitimately exist without a panel; these may not.
MUST_RENDER = ("paper", "segments", "execution", "exit-horizon", "regimes",
               "objective")


def _panel_ids() -> set[str]:
    return set(re.findall(r'id:\s*"([a-z0-9-]+)"', APP_JS.read_text()))


@pytest.mark.parametrize("endpoint", MUST_RENDER)
def test_research_endpoint_has_a_dashboard_panel(endpoint):
    assert endpoint in _panel_ids(), (
        f"/api/{endpoint} is served but no dashboard panel renders it — "
        "the reader would never see it")


def test_every_must_render_endpoint_is_actually_built():
    """A panel pointing at an endpoint the build does not write is just as broken."""
    from oracle import site_build

    for endpoint in MUST_RENDER:
        assert endpoint in site_build.ENDPOINTS, endpoint


def test_research_reports_are_rendered_as_text_not_html():
    """Report text is injected with textContent, never innerHTML.

    The reports are assembled from database values; routing them through
    innerHTML would make any stray markup in a sector name executable.
    """
    src = APP_JS.read_text()
    fn = src[src.index("function researchPanel"):]
    fn = fn[:fn.index("\n}")]
    assert "textContent" in fn
    assert "innerHTML = text" not in fn
