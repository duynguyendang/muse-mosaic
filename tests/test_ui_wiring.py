"""UI static wiring tests: ids referenced by JS must exist in index.html.

No browser here, so this guards the web/ file split (config/drawers/app):
every getElementById/$ id referenced in JS must exist in the HTML, script
tags must load in dependency order, and no reference to the removed
mosaic-api/static/ layout may remain.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def _created_ids() -> set[str]:
    """Ids minted dynamically inside JS templates (present at runtime)."""
    ids: set[str] = set()
    for name in ("config.js", "drawers.js", "chat.js", "app.js"):
        src = (WEB / name).read_text(encoding="utf-8")
        ids.update(re.findall(r'id="([^"]+)"', src))
        ids.update(re.findall(r'\.id = "([^"]+)"', src))
    return ids


def _html_ids() -> set[str]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return set(re.findall(r'id="([^"]+)"', html)) | _created_ids()


def _js_ids(name: str) -> set[str]:
    src = (WEB / name).read_text(encoding="utf-8")
    ids = set(re.findall(r"""\$\("([^"]+)"\)""", src))
    ids.update(re.findall(r"""getElementById\('([^']+)'\)""", src))
    ids.update(re.findall(r"""getElementById\("([^"]+)"\)""", src))
    # data-f-* drawer holders are queried by attribute, but the f-answer
    # body is a hard id dependency of the drawer flow.
    return ids


def test_all_js_id_references_exist_in_html():
    html_ids = _html_ids()
    missing: dict[str, list[str]] = {}
    for name in ("config.js", "drawers.js", "chat.js", "app.js"):
        for ref in sorted(_js_ids(name)):
            if ref not in html_ids:
                missing.setdefault(name, []).append(ref)
    assert not missing, f"JS references missing HTML ids: {missing}"


def test_script_tags_load_in_dependency_order():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r'<script src="([^"]+)"></script>', html)
    assert tags == ["./config.js", "./drawers.js", "./chat.js", "./app.js"], tags


def test_no_reference_to_removed_static_layout():
    for name in ("index.html", "config.js", "drawers.js", "app.js"):
        src = (WEB / name).read_text(encoding="utf-8")
        assert "mosaic-api/static" not in src, name


def test_drawer_namespace_contract():
    """drawers.js exposes exactly the drawer API app.js consumes."""
    src = (WEB / "drawers.js").read_text(encoding="utf-8")
    for name in (
        "renderSteward",
        "bindStewardActions",
        "loadPending",
        "explain",
        "openDrawer",
        "closeDrawer",
        "openTaxonomy",
        "openSimilar",
        "stewardAcceptAsk",
        "stewardApproveAsk",
        "stewardRejectAsk",
        "doReject",
    ):
        assert re.search(rf"\b{name}\b", src), name
    assert "window.MosaicDrawers" in src
    assert "window.MosaicChat" in (WEB / "chat.js").read_text(encoding="utf-8")
    assert "window.MosaicConfig" in (WEB / "config.js").read_text(encoding="utf-8")


def test_config_holds_no_dom_or_network():
    src = (WEB / "config.js").read_text(encoding="utf-8")
    assert "document." not in src
    assert "fetch(" not in src
    assert "window.MosaicConfig" in src


def test_scene0_panel_ids_exist():
    html_ids = _html_ids()
    for ref in (
        "f-scene0",
        "f-config",
        "f-config-title",
        "f-config-body",
        "f-config-note",
        "f-config-summary",
        "f-config-approve",
        "f-config-edit",
        "f-config-reject",
        "f-config-build",
        # created dynamically by approveScene0, wired immediately:
        "f-config-build2",
    ):
        assert ref in html_ids, ref
    assert "f-config-cancel" not in (WEB / "index.html").read_text(encoding="utf-8")


def test_demo_controls_and_badges_exist():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "Demo controls: DEMO ONLY" in html
    html_ids = _html_ids()
    for ref in (
        "demo-stop",
        "demo-start",
        "demo-ticket",
        "entity-active",
        "graph-candidates",
        "realtime-bar",
        "upload-collapse",
        "upload-body",
        "upload-collapsed",
        "upload-expand",
        "explain-decision",
        "steward-open-2",
        "steward-count-2",
        "similar-open",
    ):
        assert ref in html_ids, ref
    # Mock/Real bar is exact copy and lives outside detail view (always visible).
    assert "REAL: Meta API call" in html
    assert html.index('id="realtime-bar"') > html.index("/#detail-view")
    src = (WEB / "app.js").read_text(encoding="utf-8")
    for handler in ("openPolicy", "markTrapNodes", "renderSimulation"):
        assert handler in src, handler


def test_drawer_and_chat_surface_ids_exist():
    html_ids = _html_ids()
    for ref in (
        "taxonomy-drawer",
        "taxonomy-drawer-body",
        "taxonomy-drawer-close",
        "similar-drawer",
        "similar-drawer-body",
        "similar-drawer-close",
        "f-taxonomy",
        "f-similar",
        # created dynamically by renderSimulation, scrolled into view:
        "f-simulation",
    ):
        assert ref in html_ids, ref
    src = (WEB / "app.js").read_text(encoding="utf-8")
    for handler in (
        "openTaxonomy",
        "openSimilar",
        "announceRunBubbles",
        "loadGraphForBuild",
        "renderSimulation",
    ):
        assert handler in src, handler


def test_bidirectional_highlight_hooks_exist():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert "data-walk=" in src
    assert "highlightOnCanvas" in src
    assert "highlightGoldenForNode" in src
    assert "animateChain" in src
    assert "verifyingChecklist" in src.lower() or "Verifying checklist" in src
    assert "Verify, not Approve" in (WEB / "index.html").read_text(encoding="utf-8")


def test_protocol_chat_surface_exists():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for ref in (
        "f-scenario-bar",
        "f-chat-stream",
        "f-chat-input-bar",
        "f-chat-input",
    ):
        assert f'id="{ref}"' in html, ref
    for act in ("scene0", "act1", "act2", "act3", "act4", "act5"):
        assert f'data-act="{act}"' in html, act
    for question in ("phone", "F6", "datalog", "muse-only"):
        assert f'data-q="{question}"' in html, question
