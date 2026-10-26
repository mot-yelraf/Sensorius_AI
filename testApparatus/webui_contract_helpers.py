"""Read rendered markup and its extracted assets for static contract assertions.

Browser tests validate actual loading and execution. These helpers let existing
presentation assertions inspect the implementation now referenced by the markup.
"""
from pathlib import Path
import re
import json
from sensorius import saiHtml

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ('js/dashboard_runtime.js', 'js/graph_workspace.js', 'css/dashboard_cards.css',
          'css/dashboard_notifications.css', 'css/graph_workspace.css')


def dashboard_sources():
    """Return renderer and extracted source text for static contract checks."""
    return (ROOT / 'sensorius/saiHtml.py').read_text() + '\n' + '\n'.join(
        (ROOT / 'ui_static' / asset).read_text() for asset in ASSETS
    )


def with_local_assets(chunks):
    """Include only extracted assets actually referenced by this rendered page."""
    markup = ''.join(chunks)
    assets = [asset for asset in ASSETS if '/ui_static/' + asset in markup]
    return [markup, '\n', '\n'.join((ROOT / 'ui_static' / asset).read_text() for asset in assets)]


def render_dashboard(*args, **kwargs):
    """Inspect dashboard markup together with its referenced asset contracts."""
    return with_local_assets(saiHtml.render_dashboard(*args, **kwargs))


def render_graph_modal(*args, **kwargs):
    """Inspect graph markup together with its referenced asset contracts."""
    return with_local_assets(saiHtml.render_graph_modal(*args, **kwargs))


def read_config(markup, element_id="dashboard-config"):
    """Read the inert configuration emitted by the actual renderer."""
    match = re.search(r'<script type="application/json" id="' + re.escape(element_id) + r'">(.*?)</script>', markup, re.S)
    assert match is not None
    return json.loads(match.group(1))
