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

  // One canonical field per question. The pinned taxonomy alias table
  // (/taxonomy/{entity_type}) maps raw dataset headers ("dien_thoai",
  // "reviewer", "full_name") to the field the engine derives. Without it the
  // UI showed the same field under two names and produced empty decision
  // cards that could never be answered.
  const ATTR_CANON = {};
  let aliasesLoaded = false;

  function aliasesReady() {
    return aliasesLoaded;
  }

  function setAttributeAliases(entityType, aliases) {
    aliasesLoaded = true;
    Object.entries(aliases || {}).forEach(([canonical, raws]) => {
      ATTR_CANON[String(canonical).toLowerCase()] = canonical;
      (raws || []).forEach((raw) => {
        ATTR_CANON[String(raw).toLowerCase()] = canonical;
      });
    });
  }

  function canonAttr(attribute) {
    return ATTR_CANON[String(attribute || "").toLowerCase()] || attribute;
  }

  // Raw header -> canonical field, once aliases are loaded. Returns the
  // mapped fragments plus the attributes that have no canonical mapping, so
  // a caller can block honestly rather than post something the policy will
  // reject. Raw names are never dropped silently — they are reported.
  function toCanonicalFragments(list) {
    const allowed = new Set(Object.keys(ATTR_CANON));
    const mapped = [];
    const unmapped = new Set();
    const renamed = [];
    (list || []).forEach((f) => {
      const raw = String(f.attribute || "");
      const canonical = canonAttr(raw);
      if (!allowed.has(String(canonical).toLowerCase())) {
        unmapped.add(raw || "(blank)");
        return;
      }
      if (raw !== canonical) renamed.push(`${raw} → ${canonical}`);
      mapped.push(Object.assign({}, f, { attribute: canonical }));
    });
    return { mapped, unmapped: [...unmapped], renamed };
  }

  function plainAttr(attribute) {
    const canonical = canonAttr(attribute);
    if (ATTR_PLAIN[canonical]) return ATTR_PLAIN[canonical];
    const base = String(canonical || "?").replace(/[_-]+/g, " ").trim() || "?";
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

  // Plain-language rule meanings, transcribed from the runnable policy
  // (trust-engine/policies/*/[version].mg "Rule summary"). Never invented.
  const RULE_PLAIN = {
    R1: "Authority rank 1 beats rank 2, then verified beats unverified, then latest event time. Arrival time was never used.",
    R2: "Escalate, never guess — critical winner unverified, or mixed tie.",
    R3: "Expired fact treated as missing.",
    R4: "Deny weak provenance / no authority ranks.",
    L1: "Link proof — a sole-proof hop (exact email/phone/order) verifies only if the endpoint facts share that exact value.",
    C1: "Cluster uniqueness — a verified rank-1 exact tie is a conflict, never a merge.",
    C2: "Weakest-link chain — a winner cut off from the anchor by unverified hops is escalated, not silently published.",
    T1: "Taxonomy membership for categorical attributes.",
    I1: "Idempotency — same input derives same output.",
  };

  function rulePlain(rule) {
    return RULE_PLAIN[rule] || "";
  }

  window.MosaicConfig = {
    API_BASE,
    SRC_PLAIN,
    ATTR_PLAIN,
    SRC_COLOR,
    VERDICTS,
    LOOP_PILL,
    RULE_PLAIN,
    plainSource,
    plainAttr,
    canonAttr,
    setAttributeAliases,
    aliasesReady,
    toCanonicalFragments,
    rulePlain,
    esc,
  };
})();
