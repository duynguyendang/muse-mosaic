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
        # Scene 0 entry point is the intake-bar sample pill (the old
        # f-scene0 sidebar button was removed with the duplicate upload box).
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
        "app-header",
        "demo-stop",
        "demo-start",
        "demo-ticket",
        "entity-active",
        "f-side-detail",
        "f-side-steward-count",
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
    # realtime-bar stays in DOM for the contract but never renders.
    assert "#realtime-bar { display: none; }" in html
    # Mock/Real bar is exact copy and lives outside detail view (always visible).
    assert "REAL: Meta API call" in html
    assert html.index('id="realtime-bar"') > html.index("/#detail-view")
    src = (WEB / "app.js").read_text(encoding="utf-8")
    for handler in ("openPolicy", "markTrapNodes", "renderSimulation"):
        assert handler in src, handler


def test_user_first_surface_ids_exist():
    """User-First v1.0 surfaces: how-it-works, Scene 0 modal, export dropdown."""
    html_ids = _html_ids()
    for ref in (
        "f-how-it-works",
        "how-dialog",
        "how-log",
        "scene0-modal",
        "scene0-modal-body",
        "scene0-modal-yes",
        "scene0-modal-change",
        "scene0-modal-no",
        "f-export-dropdown",
        "f-export-btn",
        "f-export-menu",
        "f-mosaic-tag",
        "f-viewing",
        "f-bulk-view",
        "f-agent-modal",
        "config-modal",
    ):
        assert ref in html_ids, ref
    src = (WEB / "app.js").read_text(encoding="utf-8")
    for handler in (
        "downloadVerifiedJson",
        "downloadQuarantineJson",
        "restoreEngineAndRetry",
        "hopChainFromProposals",
        "plainFieldSentence",
    ):
        assert handler in src, handler
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "Autonomous Data Assembly Studio" in html
    assert "No, do not use this plan" in html
    assert "Export Options" in html
    assert "How we'll clean this file" in html
    assert "This approves how we clean, not the final answer" in html


def test_plain_language_regressions_absent():
    """Copy that was replaced by plain language must not come back."""
    for name in ("app.js", "index.html"):
        src = (WEB / name).read_text(encoding="utf-8")
        for banned in (
            "Muse suggests, the engine checks",
            "Would publish the neighbour as",
            "Manglekit did not merge the neighbour",
            "Trust-engine is stopped. No new golden record can be published",
            "Same decision id returned. No double derivation.",
        ):
            assert banned not in src, f"{name} still contains: {banned[:60]}"


def test_drawer_and_ticker_surface_ids_exist():
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
        # App shell: frozen header + two-column workspace (no right dock).
        "app-shell",
        "app-side",
        "app-main",
        "trust-bar",
        "f-intake-upload",
        "f-intake-file",
        "f-add-shard",
        "f-add-shard-file",
        "f-intake-samples",
        "f-enrich",
        "f-side-steward",
        "f-side-steward-count",
        "f-side-boundary",
        "f-side-detail",
        "f-status-pill",
        "f-steps",
        "f-step-1",
        "f-step-2",
        "f-step-3",
        "f-agents-grid",
        "f-telemetry",
        "f-reasoning",
        "f-mosaic",
        "f-pieces",
        "config-modal",
    ):
        assert f'id="{ref}"' in html, ref
    assert 'id="f-simulate"' not in html, "header Simulate box must be gone"
    assert 'id="f-sim-ticket"' not in html, "ticket trigger lives in the sidebar now"
    assert 'id="f-sim-engine"' not in html, "engine toggle lives on the status pill now"
    assert 'id="f-scenario-bar"' not in html, "standalone Scenarios strip must be gone"
    assert 'id="f-upload"' not in html, "duplicate sidebar upload box must be gone"
    assert 'id="f-answer-cta"' not in html, "duplicate Step 3 CTA buttons must be gone"
    assert 'id="f-intake-bar"' not in html, "old intake bar replaced by the sidebar"
    assert 'id="app-dock"' not in html, "2.5 has no right dock; the ticker lives in Step 2"
    assert "f-flow-arrow" not in html, "spacer banners must be gone"
    assert "btn-view-mosaic" not in html, "full-width View Mosaic buttons must be gone"
    assert "f-step-sub" not in html, "redundant step paragraphs must be gone"
    for cls in ("left-sidebar", "center-workspace", "app-shell-body"):
        assert f'class="{cls}"' in html or f' {cls}"' in html or f"{cls} " in html, cls
    # Entity badge is always visible (no hidden attr); realtime bar stays
    # in DOM for the contract but never renders.
    badge_line = next(line for line in html.splitlines() if 'id="entity-active"' in line)
    assert "hidden" not in badge_line, "entity badge must always render"
    assert "#realtime-bar { display: none; }" in html
    # All five sample dataset presets live in the sidebar.
    for act in ("scene0", "act1", "act2", "act3", "act4"):
        assert f'data-act="{act}"' in html, act
    for enrich in ("ticket", "order", "email"):
        assert f'data-enrich="{enrich}"' in html, enrich
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert '$("f-status-pill").addEventListener("click"' in src
    # Dataset isolation: runs pin their entity keys; steps filter by them.
    assert "activeKeys" in src
    assert "scopedNeeds" in src
