/* Muse Mosaic shared config — zero DOM, zero network.
 *
 * Pure constants and pure label helpers shared by app.js and drawers.js.
 * Loaded before both. Nothing here reads API responses; display strings
 * for policy/taxonomy versions always come from /health at runtime.
 */
(function () {
  "use strict";

  const API_BASE = (window.MOSAIC_API_BASE || "").replace(/\/+$/, "");

  // Human labels for synthetic source tags. Unknown tags fall back to a
  // mechanical prettification (never a claim about a real system).
  const SRC_PLAIN = {
    "salesforce-like": "Sales system",
    "sap-like": "Payment system",
    "ciam-like": "Login system",
    "ticket": "Support ticket",
    "pim-like": "Catalogue system",
    "erp-like": "Ledger system",
    "marketplace-like": "Marketplace listing",
    "ticket-verified": "Ticket system (verified)",
  };

  function plainSource(source) {
    if (SRC_PLAIN[source]) return SRC_PLAIN[source];
    const base = String(source || "unknown").replace(/-like$/, "").replace(/[_-]+/g, " ").trim() || "unknown";
    return base.charAt(0).toUpperCase() + base.slice(1) + " system";
  }

  const ATTR_PLAIN = {
    display_name: "Name",
    name: "Name",
    phone: "Phone",
    email: "Email",
    address: "Address",
    category: "Category",
    order_id: "Order",
  };

  function plainAttr(attribute) {
    if (ATTR_PLAIN[attribute]) return ATTR_PLAIN[attribute];
    const base = String(attribute || "?").replace(/[_-]+/g, " ").trim() || "?";
    return base.charAt(0).toUpperCase() + base.slice(1);
  }

  // HTML-escape for every interpolated string. Untrusted fragment values,
  // steward notes and engine output all pass through this.
  function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const SRC_COLOR = {
    "salesforce-like": "var(--src-salesforce)",
    "sap-like": "var(--src-sap)",
    "ciam-like": "var(--src-ciam)",
    "pim-like": "var(--src-pim)",
    "erp-like": "var(--src-erp)",
    "marketplace-like": "var(--src-market)",
  };

  // Answer-card states: the only three answers the system may give.
  const VERDICTS = {
    published: { label: "Trusted picture", cls: "ok", mark: "✓", color: "#15803D" },
    escalated: { label: "Needs a human", cls: "need", mark: "⚠", color: "#B45309" },
    denied: { label: "No trusted value — we stop, we do not guess", cls: "stop", mark: "■", color: "#B91C1C" },
  };

  const LOOP_PILL = {
    ready: "● Ready",
    working: "○ Working…",
    engineDown: "■ Engine down — nothing publishes",
    needsYou: (k) => `⚠ Needs you (${k})`,
  };

  window.MosaicConfig = {
    API_BASE,
    SRC_PLAIN,
    ATTR_PLAIN,
    SRC_COLOR,
    VERDICTS,
    LOOP_PILL,
    plainSource,
    plainAttr,
    esc,
  };
})();
