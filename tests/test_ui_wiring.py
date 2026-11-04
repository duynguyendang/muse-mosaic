"""UI static wiring tests for the studio cockpit (index.html + studio.js).

No browser here, so this guards the web/ file split: every $()/getElementById
id referenced in studio.js must exist in index.html (or be minted dynamically
by studio.js), studio.js must be the only local script, and no dead legacy
scripts may reappear at the top level.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def _studio_js_ids() -> set[str]:
    src = (WEB_ROOT / "studio.js").read_text(encoding="utf-8")
    ids = set(re.findall(r"""\$\("([^"]+)"\)""", src))
    ids.update(re.findall(r"""getElementById\(["']([^"']+)["']\)""", src))
    return ids


def _studio_html_ids() -> set[str]:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    src = (WEB_ROOT / "studio.js").read_text(encoding="utf-8")
    created = set(re.findall(r'id="([^"]+)"', src))
    created.update(re.findall(r'\.id = "([^"]+)"', src))
    return set(re.findall(r'id="([^"]+)"', html)) | created


def test_studio_js_ids_exist_in_html():
    missing = sorted(_studio_js_ids() - _studio_html_ids())
    assert not missing, f"studio.js references missing HTML ids: {missing}"


def test_studio_is_the_only_script():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    local = [s for s in re.findall(r'<script src="([^"]+)"', html) if s.startswith(".")]
    assert local == ["./studio.js"], local


def test_studio_has_no_dead_legacy_scripts_at_top_level():
    for name in ("app.js", "chat.js", "drawers.js", "config.js"):
        assert not (WEB_ROOT / name).exists(), f"dead file web/{name}"


def test_studio_gates_otp_and_bcbs_for_unattested_profiles():
    """Analytical Mesh publishes unverified multi-valued output: OTP and the
    BCBS dossier must not read as ready/certified under it."""
    src = (WEB_ROOT / "studio.js").read_text(encoding="utf-8")
    assert "isUnattestedPolicy()" in src
    assert src.count("Gated by Policy (Unattested Mode)") >= 2


def test_studio_has_no_runtime_tailwind_cdn():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert "cdn.tailwindcss.com" not in html
    assert (WEB_ROOT / "studio.css").exists()


def test_studio_kpis_use_disjoint_cluster_buckets():
    """Certified + held must never exceed clusters: a partial golden (some
    attributes published, some held) is counted once, as held."""
    src = (WEB_ROOT / "studio.js").read_text(encoding="utf-8")
    assert "partial_goldens_count" in src
    assert "partial" in src and "readable" in src


def test_header_has_no_duplicate_action_buttons_and_deck_owns_downloads():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    for gone in ("btn-run", "btn-manifest", "btn-export"):
        assert f'id="{gone}"' not in html, gone
    assert 'id="copilot-open"' in html
    js = (WEB_ROOT / "studio.js").read_text(encoding="utf-8")
    assert 'addEventListener("click", downloadManifest)' in js
    assert 'addEventListener("click", exportGoldens)' in js
