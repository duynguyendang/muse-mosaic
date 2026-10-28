/* Muse Mosaic demo UI.
 *
 * Read-only except for explicit buttons. It never calls Muse, never derives,
 * never publishes and holds no API key. All trust comes from the ledger via
 * the mosaic-api; the mock proposal is shown for transparency, not as truth.
 */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // Separate frontend deploy: point at the API with e.g.
  // <script>window.MOSAIC_API_BASE = "https://api.example.com"</script>
  // before app.js. Same-origin default keeps local/demo working.
  const API_BASE = (window.MOSAIC_API_BASE || "").replace(/\/+$/, "");
  const entitySel = $("entity");
  const ticketBox = $("with-ticket");
  const resolveBtn = $("resolve");
  const refreshBtn = $("refresh");
  const statusLine = $("status-line");
  const proposerBody = $("proposer-body");
  const engineBody = $("engine-body");
  const stewardBody = $("steward-body");
  const mosaic = $("mosaic");
  const tileDetail = $("tile-detail");
  const dialog = $("explain-dialog");

  let fragments = [];
  let tickets = [];
  let lastResolve = null;

  const loop = {
    mode: "running",
    autoGoldens: 0,
    autoLinks: 0,
    lastAutoRunAt: null,
    engineDown: false,
    taxonomy: null,
    splink: { status: "unknown", splink_version: null, duckdb_version: null },
  };

  function clockNow() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function feed(text) {
    const list = $("feed-list");
    if (!list) return;
    const li = document.createElement("li");
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = clockNow();
    li.appendChild(ts);
    li.appendChild(document.createTextNode(text));
    list.prepend(li);
  }

  function renderLoop() {
    const state = $("loop-state");
    const btn = $("pause-btn");
    if (loop.engineDown && loop.mode === "running") {
      state.textContent = "● Autonomous Loop: Running, engine down — new runs escalate, nothing publishes.";
      state.className = "loop-state running";
    } else if (loop.mode === "running") {
      state.textContent = "● Autonomous Loop: Running";
      state.className = "loop-state running";
    } else {
      state.textContent = "○ Autonomous Loop: Paused — no auto run until resumed.";
      state.className = "loop-state paused";
    }
    btn.textContent = loop.mode === "running" ? "Pause Autonomous Loop" : "Resume Autonomous Loop";
    $("loop-last").textContent = loop.lastAutoRunAt
      ? `Last auto run: ${loop.lastAutoRunAt}`
      : "Last auto run: none yet — no auto run has completed.";
    $("loop-counts").textContent =
      `Auto-published this session: ${loop.autoGoldens} goldens, ${loop.autoLinks} links — without a human click`;
    const hint = $("loop-hint");
    if (hint) {
      hint.textContent = loop.mode === "running"
        ? "Loop Running: choosing a file auto-runs the approved pipeline, no click needed."
        : "Loop Paused: choosing a file waits — click Upload to run manually.";
    }
  }

  function relTime(iso) {
    if (!iso) return "";
    const ms = Date.parse(iso);
    if (Number.isNaN(ms)) return String(iso);
    const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ms).toISOString().slice(0, 10);
  }

  let pendingAll = [];

  function needCounts() {
    const { entityType, entityId } = currentEntity();
    const runQueue = fState.ran && lastFriendly ? lastFriendly.steward_queue || [] : null;
    const pool = runQueue || pendingAll;
    const mine = pool.filter((s) => s.entity_type === entityType && s.entity_id === entityId).length;
    const total = runQueue ? mine : pendingAll.length;
    return { mine, total };
  }

  function renderDetailLoopLine() {
    const { mine, total } = needCounts();
    const { entityType } = currentEntity();
    const who = entityType === "user" ? "person" : "product";
    let text = `Escalated, needs a human: ${mine} for this ${who}`;
    if (total > mine) text += ` — ${total} across all entities`;
    text += " — see Steward Queue";
    $("loop-escalated").textContent = text;
  }

  function renderEscalatedCount(k) {
    $("steward-count").textContent = String(k);
    renderDetailLoopLine();
    fState.needCount = needCounts().mine;
    renderFStatus();
    renderFLoop();
  }

  /* ---- Friendly View (v2.2 default): same endpoints, same decisions ---- */
  const fState = { ran: false, working: false, downstream: null, needCount: 0, splinkPairs: [], policyUser: "", policyProduct: "" };
  let lastFriendly = null;
  let hasRun = false;
  function markRan() {
    if (hasRun) return;
    hasRun = true;
    ["sec-graph", "sec-engine", "sec-mosaic", "sec-golden", "sec-downstream"].forEach((id) => {
      const el = $(id);
      if (el) el.hidden = false;
    });
    const empty = $("detail-empty");
    if (empty) empty.hidden = true;
  }
  function applyPreRunState() {
    if (hasRun) return;
    ["sec-graph", "sec-engine", "sec-mosaic", "sec-golden", "sec-downstream"].forEach((id) => {
      const el = $(id);
      if (el) el.hidden = true;
    });
    const empty = $("detail-empty");
    if (empty) empty.hidden = false;
  }

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

  function setFriendlyFromResolve(payload) {
    lastFriendly = {
      verdict: payload.verdict,
      golden: payload.golden || null,
      steward_queue: payload.steward_queue || [],
      conflicts: payload.conflicts || [],
      decision_id: payload.decision_id,
      policy_version: payload.policy_version,
      taxonomy_version: payload.taxonomy_version,
    };
    fState.ran = true;
  }

  function setFriendlyFromBuild(payload) {
    const first = (payload.goldens || []).find((g) => g.golden_id) || null;
    lastFriendly = {
      verdict: payload.verdict,
      golden: first,
      steward_queue: payload.steward_queue || [],
      conflicts: payload.conflicts || [],
      decision_id: payload.decision_id,
      policy_version: payload.policy_version_user,
      taxonomy_version: payload.taxonomy,
    };
    fState.splinkPairs = (payload.splink && payload.splink.pairs) || [];
    fState.ran = true;
  }

  function showDetail(open) {
    document.body.classList.toggle("detail", open);
    document.body.classList.toggle("friendly", !open);
    $("friendly").hidden = open;
    $("detail-view").hidden = !open;
    if (open) $("detail-view").scrollIntoView();
    else window.scrollTo(0, 0);
  }

  function toggleLoop() {
    loop.mode = loop.mode === "running" ? "paused" : "running";
    renderLoop();
    renderFLoop();
    renderFStatus();
    feed(loop.mode === "running"
      ? "Autonomous Loop resumed — next auto check: on next upload."
      : "Autonomous Loop paused — no auto run until resumed.");
  }

  function renderFStatus() {
    const pill = $("f-status-pill");
    if (!pill) return;
    const { mine } = needCounts();
    fState.needCount = mine;
    pill.className = "pill f-only";
    if (loop.engineDown) {
      pill.textContent = "■ Engine down — nothing publishes";
      pill.classList.add("f-status-down");
    } else if (fState.working) {
      pill.textContent = "○ Working…";
      pill.classList.add("f-status-working");
    } else if (fState.needCount > 0) {
      pill.textContent = `⚠ Needs you (${fState.needCount})`;
      pill.classList.add("f-status-need");
    } else {
      pill.textContent = "● Ready";
      pill.classList.add("f-status-ready");
    }
    const fp = $("f-pause");
    if (fp) fp.textContent = loop.mode === "running" ? "Pause the loop" : "Resume the loop";
  }

  function renderFSteps() {
    const s1 = $("f-step-1"), s2 = $("f-step-2"), s3 = $("f-step-3");
    s1.querySelector(".lbl").textContent = fState.ran ? "Messy pieces" : "Start with messy pieces";
    const setState = (el, mode) => {
      el.classList.toggle("done", mode === "done");
      el.classList.toggle("now", mode === "now");
      el.classList.toggle("active", mode !== "future");
    };
    setState(s1, fState.ran ? "done" : "now");
    setState(s2, !fState.ran ? "future" : (fState.downstream ? "done" : "now"));
    const stopped = !!(fState.ran && lastFriendly && lastFriendly.verdict === "denied");
    setState(s3, fState.downstream || stopped ? "done" : "future");
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

  function renderFPieces() {
    const box = $("f-pieces");
    const list = activeFragments();
    if (!list.length) {
      box.innerHTML = `<div class="f-piece">No pieces loaded yet.</div>`;
      return;
    }
    const valuesByAttr = {};
    list.forEach((f) => {
      (valuesByAttr[f.attribute] = valuesByAttr[f.attribute] || new Set()).add(f.value);
    });
    const bySource = {};
    list.forEach((f) => { (bySource[f.source] = bySource[f.source] || []).push(f); });
    box.innerHTML = Object.entries(bySource).map(([source, arr]) => {
      const lines = arr.map((f) => {
        const conflict = (valuesByAttr[f.attribute] || new Set()).size > 1
          ? ` <span class="f-conflict">Different values — this is what we must decide</span>` : "";
        // Friendly small text is one of two exact forms only, with plain
        // attribute names: verified rank-1 authority is "trusted for [name]",
        // unverified is "not proven yet". Verified non-rank-1 facts carry
        // no tag — rank numbers stay in Detail View.
        const tag = f.verified
          ? (f.authority_rank === 1 ? ` <span class="muted">(trusted for ${esc(plainAttr(f.attribute).toLowerCase())})</span>` : "")
          : ` <span class="muted">(not proven yet)</span>`;
        return `<div>${esc(plainAttr(f.attribute))} ${esc(f.value)}${tag}${conflict}</div>`;
      }).join("");
      return `<div class="f-piece"><span class="src"><span class="f-src-dot" style="background:${SRC_COLOR[source] || "#475569"}"></span>${esc(plainSource(source))} says:</span>${lines}</div>`;
    }).join("");
  }

  function fragmentForAttribute(attribute) {
    const list = activeFragments().filter((f) => f.attribute === attribute);
    return list.find((f) => !f.verified) || list[0] || null;
  }

  function fragmentForFactId(factId) {
    const { entityId } = currentEntity();
    return (
      activeFragments().find((f) => `${entityId}-${f.source_id}` === factId) || null
    );
  }

  function friendlyWhy(attribute, value) {
    // Plain-language proof sentence, built only from the golden field's own
    // data (winner fact id -> fragment source/rank/verified/event time).
    // Never leaks rule_fired, winner_fact_id, or policy_version here.
    if (!value || typeof value !== "object") return "Checked by the engine.";
    const attrPlain =
      (attribute === "purchased_products" ? "purchase" : plainAttr(attribute)).toLowerCase();
    const winner = fragmentForFactId(value.winner_fact_id);
    if (!winner) return "Checked by the engine.";
    const src = plainSource(winner.source);
    const sameAttr = activeFragments().filter((f) => f.attribute === attribute);
    const newer = sameAttr.filter(
      (f) => Date.parse(f.event_time) > Date.parse(winner.event_time)
    );
    const newerSrc =
      newer.length > 0 ? plainSource(newer[newer.length - 1].source) : null;
    const evenThough = newerSrc ? ` Even though ${newerSrc} is newer.` : "";
    if (winner.authority_rank === 1) {
      return `${src} wins because it is trusted for ${attrPlain}.${evenThough}`;
    }
    if (winner.verified && !newer.length) {
      return `${src} wins because it is the newest proven value.`;
    }
    return "Checked by the engine.";
  }

  function setFHead(pillText, titleText) {
    $("f-answer-head").innerHTML =
      `<span class="f-state-pill">${esc(pillText)}</span>${esc(titleText)}`;
  }

  function renderFAnswer() {
    const box = $("f-answer");
    const body = $("f-answer-body");
    $("f-answer-cta").hidden = fState.ran;
    box.className = "f-answer";
    if (!fState.ran || !lastFriendly) {
      box.classList.add("idle");
      setFHead("○ Not yet", "No answer yet");
      body.innerHTML = `Run the picture to get one trusted answer — or an honest stop.`;
      return;
    }
    const f = lastFriendly;
    const needs = (f.steward_queue || []).length;
    const { entityId } = currentEntity();
    let downstreamLine = "";
    if (fState.downstream) {
      const d = fState.downstream;
      const who = d.agent === "search" ? "Search" : "OTP";
      downstreamLine = d.answer === "golden"
        ? `<div class="f-field">${esc(who)} can use ${esc(d.field)} ${esc(d.value)} now.</div>`
        : `<div class="f-field">Downstream stopped: no trusted value.</div>`;
    }
    if (needs > 0) {
      box.classList.add("need");
      setFHead(`⚠ Needs a human`, `(${needs}) — we did not guess.`);
      const item = f.steward_queue[0];
      const frag = fragmentForAttribute(item.attribute);
      const what = frag
        ? `"${frag.value}" for ${plainAttr(item.attribute)} came from ${plainSource(frag.source).toLowerCase()} and could not be proven.`
        : `${plainAttr(item.attribute)} could not be proven.`;
      const ownerLines = `<div class="f-steward-meta">Owner: ${esc(item.owner || "steward-001")}</div>`
        + `<div class="f-steward-meta">Decide by: ${esc(item.deadline || "—")}</div>`
        + `<div class="f-steward-meta">If no decision: ${esc(item.fallback || "do not use this value")}</div>`;
      let actions;
      if (item.suggested_action) {
        actions = `<div class="meta">Suggested: <span class="mono">${esc(item.suggested_action)}</span></div>`
          + `<div class="steward-actions">`
          + `<button class="btn-approve" data-approve="${esc(item.steward_id)}">Use this value</button>`
          + `<button class="btn-reject" data-reject="${esc(item.steward_id)}">Do not use it</button>`
          + `<button class="btn-split" data-split="${esc(item.steward_id)}">These are two different people</button>`
          + `<button class="btn-accept" data-f-accept="${esc(item.steward_id)}" data-action="${esc(item.suggested_action)}">Accept this suggestion</button>`
          + `</div>`;
      } else {
        actions = `<div class="steward-actions">`
          + `<button class="btn-approve" data-approve="${esc(item.steward_id)}">Use this value</button>`
          + `<button class="btn-reject" data-reject="${esc(item.steward_id)}">Do not use it</button>`
          + `<button class="btn-split" data-split="${esc(item.steward_id)}">These are two different people</button>`
          + `</div>`;
      }
      body.innerHTML = `<div class="f-steward-q">${esc(what)}</div>`
        + ownerLines + actions
        + (f.decision_id
          ? `<div class="f-field"><button data-f-proof3 type="button">Proof ID: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`
          : "")
        + downstreamLine;
      bindStewardActions(body);
      const pb3 = body.querySelector("[data-f-proof3]");
      if (pb3) pb3.addEventListener("click", () => explain(f.decision_id));
      const fa = body.querySelector("[data-f-accept]");
      if (fa) fa.addEventListener("click", () =>
        stewardAcceptAsk(fa.getAttribute("data-f-accept"), fa.getAttribute("data-action"), fa));
      return;
    }
    const golden = f.golden;
    if (golden && Object.keys(golden.attributes || {}).length > 0) {
      box.classList.add("ok");
      setFHead("✓ Trusted picture", ` — ${entityId}`);
      const fields = Object.entries(golden.attributes || {}).map(([name, value]) => {
        if (name === "purchased_products" && Array.isArray(value)) {
          const cats = value.map((p) => p.category || "?").join(", ");
          return `<div class="f-field"><b>Bought:</b> <span class="f-val">${esc(cats)}</span>`
            + `<div class="f-why">Verified links, checked by the engine</div></div>`;
        }
        const val = (value && typeof value === "object") ? value.value : value;
        const why = friendlyWhy(name, value);
        return `<div class="f-field"><b>${esc(plainAttr(name))}:</b> <span class="f-val">${esc(val)}</span>`
          + `<div class="f-why">${esc(why)}</div>`
          + `<button data-f-explain="${esc(name)}" type="button">Why this value?</button></div>`;
      }).join("");
      body.innerHTML = fields + downstreamLine;
      if (f.decision_id) {
        body.insertAdjacentHTML("beforeend",
          `<div class="f-field"><button data-f-explain type="button">Proof ID: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`);
      }
      body.querySelectorAll("[data-f-explain]").forEach((btn) =>
        btn.addEventListener("click", () => explain(f.decision_id, btn.getAttribute("data-f-explain") || undefined)));
      return;
    }
    box.classList.add("stop");
    setFHead("■ No trusted value", " — we stop, we do not guess.");
    body.innerHTML = `<div>No value passed every check, so no agent may act on this person yet.</div>`
      + `<div class="f-act"><button data-f-detail type="button">See why we stopped</button></div>`
      + (f.decision_id
        ? `<div class="f-field"><button data-f-proof2 type="button">Proof ID: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`
        : "")
      + downstreamLine;
    body.querySelector("[data-f-detail]").addEventListener("click", () => showDetail(true));
    const pb = body.querySelector("[data-f-proof2]");
    if (pb) pb.addEventListener("click", () => explain(f.decision_id));
  }

  function renderFProof() {
    const el = $("f-proof-text");
    if (!el) return;
    const open = `<b class="f-muse">Muse</b> suggested the links. The <b class="f-eng">engine</b> checked them. `;
    if (!fState.ran || !lastFriendly) {
      el.innerHTML = `${open}Nothing derived yet.`;
      return;
    }
    const t = lastFriendly.golden ? Object.keys(lastFriendly.golden.attributes || {}).length : 0;
    const n = (lastFriendly.steward_queue || []).length;
    el.innerHTML = open + (n > 0
      ? `${t} field${t === 1 ? "" : "s"} trusted, ${n} need${n === 1 ? "s" : ""} a human.`
      : `All ${t} fields trusted.`);
  }

  function renderFLoop() {
    const el = $("f-loop-text");
    if (!el) return;
    const { mine, total } = needCounts();
    $("f-loop-review").hidden = mine === 0 && total === 0;
    if (!fState.ran) {
      el.textContent = "Nothing cleaned yet — try the example below.";
      return;
    }
    const clean = `it cleaned ${loop.autoGoldens} by itself`;
    const when = loop.lastAutoRunAt
      ? ` Last cleaned by itself: ${relTime(loop.lastAutoRunAt)}.` : "";
    if (mine > 0) {
      el.innerHTML = `Mosaic is watching: ${clean}, <b>${mine} need${mine === 1 ? "s" : ""} you</b>.${esc(when)}`;
    } else if (total > 0) {
      el.textContent = `Mosaic is watching: ${clean}, nothing needs you right now (${total} across all entities).${when}`;
    } else {
      el.textContent = `Mosaic is watching: ${clean}, nothing needs you right now.${when}`;
    }
  }

  function renderFriendly() {
    renderFStakes();
    renderFPlan();
    renderFPieces();
    renderFAnswer();
    renderFProof();
    renderFLoop();
    renderFSteps();
    renderFStatus();
    // "Show how it was decided" only makes sense once a decision exists.
    $("f-show-detail").hidden = !fState.ran;
    $("f-show-detail-2").hidden = !fState.ran;
  }

  function renderFPlan() {
    const el = $("f-plan-body");
    if (!el) return;
    // Built only from data the frontend already has: entity types from the
    // loaded fragments, match fields from the last build's Splink pairs
    // (blocking_rule), policy/taxonomy versions from /health or the payload.
    // Entity kinds come from the current selection (loaded fragments carry
    // no entity_type of their own: they are per-entity by construction),
    // plus an uploaded dataset of another type when one is staged.
    const { entityType: currentType } = currentEntity();
    const kinds = [currentType];
    if (window.__uploadedEntityType && window.__uploadedEntityType !== currentType) {
      kinds.push(window.__uploadedEntityType);
    }
    const kindNames = kinds.map((t) => (t === "user" ? "people" : t === "product" ? "products" : t));
    const pairs = (fState.splinkPairs || []).map((p) => p.blocking_rule).filter(Boolean);
    const fields = [...new Set(pairs)].join(" + ");
    const pol = fState.policyUser && fState.policyProduct
      ? `Cleaning rules ${fState.policyUser} / ${fState.policyProduct}`
      : "Cleaning rules (versions load with health)";
    const tax = loop.taxonomy || (lastFriendly && lastFriendly.taxonomy_version)
      ? `Names we recognise: ${loop.taxonomy || (lastFriendly && lastFriendly.taxonomy_version)}` : "";
    el.innerHTML = `<div class="f-plan-line">Things we track: ${esc(kindNames.join(", ") || "—")}.</div>`
      + `<div class="f-plan-line">Match by: ${esc(fields || "exact identifiers (shown after the first build)")}</div>`
      + `<div class="f-plan-line">${esc(pol)}${tax ? ` · ${esc(tax)}` : ""}</div>`;
  }

  function renderFStakes() {
    const el = $("f-stakes");
    if (!el) return;
    const { entityType } = currentEntity();
    el.textContent = entityType === "product"
      ? "Someone searched for this product. Which category should search use?"
      : "Someone needs a one-time code. Which phone should we send it to? If we guess wrong, the code goes to the wrong person.";
  }

  function noteAutoRun(payload) {
    if (payload.verdict === "published" && payload.golden) {
      loop.autoGoldens += 1;
      loop.lastAutoRunAt = payload.golden.created_at || payload.golden.updated_at || null;
    }
    loop.autoLinks += (payload.verified_links || []).length;
    renderLoop();
  }

  function openDrawer() {
    $("steward-drawer").classList.add("open");
    $("drawer-scrim").classList.add("open");
    $("drawer-close").focus();
  }

  function closeDrawer() {
    $("steward-drawer").classList.remove("open");
    $("drawer-scrim").classList.remove("open");
  }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function inlinePrompt(title, def) {
    // Replaces the old browser prompt: same question, inline modal, cancel-safe.
    return new Promise((resolve) => {
      const dlg = $("prompt-dialog");
      $("prompt-title").textContent = title;
      const input = $("prompt-input");
      input.value = def == null ? "" : String(def);
      const ok = $("prompt-ok");
      const cancel = $("prompt-cancel");
      const done = (value) => {
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        dlg.close();
        resolve(value);
      };
      const onOk = () => done(input.value);
      const onCancel = () => done(null);
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      dlg.showModal();
      input.focus();
      input.select();
    });
  }

  function setStatus(text) {
    statusLine.textContent = text;
  }

  async function api(path, options) {
    const response = await fetch(API_BASE + path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail || {};
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail.message || `HTTP ${response.status}`
      );
    }
    return data;
  }

  function renderTaxonomyBadge(healthTaxonomy) {
    // The badge must never sit on "checking…" after health loads: show it
    // only once a taxonomy version is actually known (health or first run).
    const known = loop.taxonomy || healthTaxonomy || "";
    const badge = $("taxonomy-badge");
    if (!badge) return;
    if (known) {
      badge.hidden = false;
      badge.textContent = `Taxonomy: ${known}`;
    } else {
      badge.hidden = true;
    }
  }

  async function refreshHealth() {
    try {
      const data = await api("/health");
      const apiOk = data.mosaic_api === "ok";
      $("api-dot").className = "dot " + (apiOk ? "ok" : "down");
      $("api-status").textContent = "mosaic-api: " + (apiOk ? "ok" : "down");
      const engOk = data.trust_engine === "ok";
      loop.engineDown = !engOk;
      $("engine-dot").className = "dot " + (engOk ? "ok" : "down");
      $("engine-status").textContent =
        "trust-engine: " + (engOk ? "Engine OK" : "Engine DOWN — Fail-closed");
      const splinkOk = data.splink === "ok";
      loop.splink = {
        status: data.splink || "unknown",
        splink_version: data.splink_version || null,
        duckdb_version: data.duckdb_version || null,
      };
      $("splink-dot").className = "dot " + (splinkOk ? "ok" : "down");
      if (splinkOk) {
        const ver = [["Splink", data.splink_version], ["DuckDB", data.duckdb_version]]
          .filter(([, v]) => v).map(([n, v]) => `${n} ${v}`).join(" · ");
        $("splink-status").textContent = "Matching Engine: ok" + (ver ? ` (${ver})` : "");
      } else {
        $("splink-status").textContent = "Matching Engine: DOWN — fallback to Muse + fuzzy";
      }
      $("policy-badge").textContent =
        `Policy: ${data.policy_version_user || "?"} / ${data.policy_version_product || "?"}`;
      fState.policyUser = data.policy_version_user || "";
      fState.policyProduct = data.policy_version_product || "";
      renderTaxonomyBadge(data.taxonomy_version);
      $("config-summary").textContent =
        `Config in force — Policy: ${data.policy_version_user || "?"} / ${data.policy_version_product || "?"}` +
        (loop.taxonomy ? ` · Taxonomy: ${loop.taxonomy}` : "") +
        " (from /health; no config proposed yet)";
      const banner = $("engine-banner");
      if (!engOk) {
        banner.style.display = "block";
        banner.textContent =
          "Trust-engine is stopped. No new golden record can be published. Requests will escalate. This is fail-closed, not a crash.";
      } else {
        banner.style.display = "none";
        banner.textContent = "";
      }
      renderLoop();
      renderFStatus();
    } catch (error) {
      $("api-dot").className = "dot down";
      $("api-status").textContent = "mosaic-api: unreachable";
    }
  }

  function currentEntity() {
    const [entityType, entityId] = entitySel.value.split("/");
    return { entityType, entityId };
  }

  async function loadEntities() {
    // Entities are defined by data/mock fixtures (GET /entities). The three
    // hardcoded <option>s stay as fallback if the fetch fails.
    let items = [];
    try {
      const data = await api("/entities");
      items = data.entities || [];
    } catch (error) {
      return;
    }
    if (!items.length) return;
    const previous = entitySel.value;
    entitySel.innerHTML = items.map((e) =>
      `<option value="${esc(e.entity_type)}/${esc(e.entity_id)}">`
      + `${esc(e.entity_type) === "user" ? "User" : "Product"} — ${esc(e.entity_id)}`
      + ` (${e.fragments} fragments)</option>`).join("")
      + `<option value="custom">Define custom entity…</option>`;
    if (items.some((e) => `${e.entity_type}/${e.entity_id}` === previous)) {
      entitySel.value = previous;
    }
  }

  async function defineCustomEntity() {
    const raw = await inlinePrompt("Define entity as type/id (e.g. user/P-001):", "user/");
    if (!raw) { entitySel.selectedIndex = 0; return loadFragments(); }
    const value = raw.trim();
    if (!/^[a-z][a-z0-9_]*\/.+/.test(value)) {
      setStatus("Custom entity must look like type/id (e.g. user/P-001).");
      entitySel.selectedIndex = 0;
      return loadFragments();
    }
    if (![...entitySel.options].some((o) => o.value === value)) {
      const [t, ...rest] = value.split("/");
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = `${t === "user" ? "User" : "Product"} — ${rest.join("/")} (custom)`;
      entitySel.insertBefore(opt, entitySel.lastChild);
    }
    entitySel.value = value;
    return loadFragments();
  }

  function onEntityChange() {
    fState.ran = false;
    fState.downstream = null;
    lastFriendly = null;
    if (entitySel.value === "custom") {
      defineCustomEntity().catch((error) => setStatus(`Cannot load fragments: ${error.message}`));
      return;
    }
    loadFragments().catch((error) => setStatus(`Cannot load fragments: ${error.message}`));
  }

  function friendlyEntityLabel(entityType, entityId) {
    const pool = fragments.length ? fragments : activeFragments();
    const pick = (attr, source) => {
      const rows = pool.filter((f) => f.attribute === attr);
      return (rows.find((f) => f.source === source) || rows[0] || {}).value;
    };
    if (entityType === "user") {
      const name = pick("display_name", "sap-like") || entityId;
      return `Person — ${name} (${entityId})`;
    }
    const name = pick("category", "pim-like") || pick("name", "pim-like") || entityId;
    return `Product — ${name} (${entityId})`;
  }

  async function loadFragments() {
    const { entityType, entityId } = currentEntity();
    $("graph-path").hidden = true;
    const data = await api(`/fragments/${entityType}/${entityId}`);
    fragments = data.fragments || [];
    tickets = data.tickets || [];
    const opt = [...entitySel.options].find((o) => o.value === `${entityType}/${entityId}`);
    if (opt) opt.textContent = friendlyEntityLabel(entityType, entityId);
    renderFragments();
    renderFriendly();
    const shown = activeFragments().length;
    const ticketNote = (ticketBox.checked && tickets.length)
      ? ` (includes ${tickets.length} ticket(s))` : "";
    setStatus(`Loaded ${shown} synthetic fragments${ticketNote}.`);
  }

  function activeFragments() {
    const list = fragments.slice();
    if (ticketBox.checked) {
      for (const ticket of tickets) {
        for (const fragment of ticket.proposed_fragments || []) {
          list.push(fragment);
        }
      }
    }
    return list;
  }

  const SRC_COLOR = {
    "salesforce-like": "var(--src-salesforce)", "sap-like": "var(--src-sap)",
    "ciam-like": "var(--src-ciam)", "pim-like": "var(--src-pim)",
    "erp-like": "var(--src-erp)", "marketplace-like": "var(--src-market)",
  };

  function renderFragments() {
    const list = activeFragments();
    proposerBody.innerHTML = `<div class="card"><span class="muted">Loaded fragments — no builder trace yet. Resolve or build to see Scout / Linker / Cluster / Taxonomist / Critic claims.</span></div>`
      + list.map((f) => `
      <div class="card ${f.verified ? "info" : "warn"}">
        <h3><span class="swatch" style="background:${SRC_COLOR[f.source] || "var(--tomb)"}"></span>
        ${esc(f.attribute)} <span class="muted mono">${esc(f.value)}</span></h3>
        <div class="meta mono">${esc(f.source)} / ${esc(f.source_id)} · rank ${esc(f.authority_rank)} ·
        ${f.verified ? "verified" : "unverified"} · ${esc(f.event_time)}</div>
      </div>`).join("") || `<div class="card"><span class="muted">No fragments.</span></div>`;
  }

  function badge(text) {
    return `<span class="badge ${esc(text)}">${esc(text)}</span>`;
  }

  async function resolve(auto) {
    resolveBtn.disabled = true;
    fState.working = true;
    renderFStatus();
    try {
      const { entityType, entityId } = currentEntity();
      const list = activeFragments();
      const key = `ui-${Date.now()}`;
      const payload = await api("/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          work_id: `ui-${key}`,
          entity_type: entityType,
          entity_id: entityId,
          idempotency_key: key,
          fragments: list,
        }),
      });
      lastResolve = payload;
      if (payload.taxonomy_version) {
        loop.taxonomy = payload.taxonomy_version;
        renderTaxonomyBadge();
      }
      renderProposal(payload.proposal);
      renderEngine(payload);
      renderSteward(payload.steward_queue || []);
      renderMosaic(payload);
      renderEscalatedCount((payload.steward_queue || []).length);
      const verdictCap = payload.verdict.charAt(0).toUpperCase() + payload.verdict.slice(1);
      if (auto) {
        noteAutoRun(payload);
        feed(`Engine derived: ${entityId} — ${verdictCap} — Decision: ${payload.decision_id}`);
        const firstConflict = (payload.conflicts || [])[0];
        if (firstConflict) {
          feed(`${firstConflict.attribute} escalated — ${firstConflict.rule_fired} — steward item created, loop continued on other fields`);
        }
        await askDownstream(entityType === "product" ? "search" : "otp", { auto: true });
      } else {
        feed(`Manual run (click) — Decision: ${payload.decision_id} — Verdict: ${verdictCap}`);
      }
      setStatus(`Decision ${payload.decision_id} — verdict: ${payload.verdict}.`);
      setFriendlyFromResolve(payload);
      markRan();
      renderFriendly();
    } catch (error) {
      setStatus(`Resolve failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto run failed (fail-closed): ${error.message} — nothing published`);
    } finally {
      resolveBtn.disabled = false;
      fState.working = false;
      renderFStatus();
    }
  }

  function renderProposal(proposal) {
    if (!proposal) return;
    const attrs = Object.entries(proposal.attributes || {});
    const trace = (proposal.subagent_trace || []).map((step) =>
      `<div class="meta">▸ <b>${esc(step.agent || "?")}</b>`
      + `${step.real_call ? " <span class='badge'>REAL</span>" : " <span class='badge'>mock</span>"}`
      + ` — ${esc(step.output_summary || "")}</div>`).join("");
    proposerBody.innerHTML = (trace ? `<div class="card plum"><h3>Sub-agent trace</h3>${trace}</div>`
      : `<div class="card"><span class="muted">No builder trace for this run.</span></div>`)
      + attrs.map(([name, attr]) => `
      <div class="card info">
        <h3>${esc(name)} ${badge(attr.confidence)}</h3>
        <div>proposed: <span class="mono">${esc(attr.proposed_value ?? "— abstain —")}</span></div>
        <div class="meta">${esc(attr.explanation)}</div>
        <div class="meta mono">evidence: ${(attr.evidence_fact_ids || []).map(esc).join(", ")}</div>
      </div>`).join("")
      + `<div class="card"><span class="muted">Muse does not publish. Only the engine publishes. Muse holds no downstream token.</span></div>`;
  }

  function renderGolden(payload) {
    const golden = payload.golden;
    if (!golden) {
      $("golden-body").textContent = `No golden published (verdict: ${payload.verdict}).`;
      return;
    }
    const attrs = Object.entries(golden.attributes || {}).map(([name, value]) => {
      if (name === "purchased_products" && Array.isArray(value)) {
        const items = value.map((p, i) =>
          `<div class="gold-field"><div class="fld">PURCHASED[${i}]</div>`
          + `<div class="val">${esc(p.category || "?")}</div>`
          + (p._lineage ? `<div class="prov mono">${esc(JSON.stringify(p._lineage))}</div>` : "") + `</div>`).join("");
        return `<div class="gold-grid">${items}</div>`;
      }
      const line = typeof value === "object" && value !== null
        ? `<div class="val">${esc(value.value)}</div><div class="prov">${esc(value.rule_fired || "")} · ${esc(value.taxonomy_version || "")}</div>`
        : `<div class="val">${esc(value)}</div>`;
      return `<div class="gold-field"><div class="fld">FIELD: ${esc(name).toUpperCase()}</div>${line}</div>`;
    }).join("");
    $("golden-body").innerHTML =
      `<div class="meta">version <span class="mono">${golden.version}</span> · taxonomy <span class="mono">${esc(golden.taxonomy_version || "—")}</span></div>`
      + `<div class="gold-grid">${attrs}</div>`;
  }

  function renderEngine(payload) {
    const golden = payload.golden;
    const attrs = golden ? Object.entries(golden.attributes || {}) : [];
    const goldenHtml = attrs.map(([name, value]) => {
      if (name === "purchased_products") return "";
      return `
      <div class="card ok">
        <h3>${esc(name)} <span class="mono">${esc(value.value)}</span> ${badge("published")}</h3>
        <div class="meta mono">winner ${esc(value.winner_fact_id)} · rule ${esc(value.rule_fired)} ·
        policy ${esc(value.policy_version)} · taxonomy ${esc(value.taxonomy_version || "—")}</div>
        <div class="meta mono">losers: ${(value.loser_fact_ids || []).map(esc).join(", ") || "—"}</div>
        <div class="meta mono">${esc(value.lineage)}</div>
      </div>`;
    }).join("");

    const conflictHtml = (payload.conflicts || []).map((c) => `
      <div class="card warn">
        <h3>${esc(c.attribute)} ${badge("escalated")}</h3>
        <div class="meta mono">${esc(c.reason_code)} · rule ${esc(c.rule_fired)} · policy ${esc(c.policy_version)} · taxonomy ${esc(c.taxonomy_version || "—")}</div>
        <div class="meta mono">facts: ${(c.fact_ids || []).map(esc).join(", ")}</div>
      </div>`).join("");

    const vlinkHtml = ((payload.verified_links && payload.verified_links.length) ? `
      <div class="card plum"><h3>Verified links (L1)</h3>
      <div class="meta mono">${payload.verified_links.map((l) =>
        `${esc(l.from_fact_id)} —${esc(l.proof)}→ ${esc(l.to_fact_id)}`).join("<br/>")}</div></div>` : "");

    const verdictClass = payload.verdict === "published" ? "ok" : payload.verdict === "denied" ? "stop" : "warn";
    engineBody.innerHTML = `
      <div class="card ${verdictClass}">
        <h3>Decision <span class="mono">${esc(payload.decision_id)}</span> ${badge(payload.verdict)}</h3>
        <div class="meta mono">policy ${esc(payload.policy_version)} · taxonomy ${esc(payload.taxonomy_version || "—")}</div>
        <div class="steward-actions">
          <button data-explain="${esc(payload.decision_id)}">Explain / replay</button>
        </div>
      </div>` + (goldenHtml || "") + (conflictHtml || "") + vlinkHtml;
    engineBody.querySelectorAll("[data-explain]").forEach((btn) => {
      btn.addEventListener("click", () => explain(btn.getAttribute("data-explain")));
    });
    engineBody.insertAdjacentHTML("beforeend",
      `<div class="card"><span class="muted">Same facts + same policy version always derive the same golden record. A rule change is a new policy version, and old golden records are re-derived, not hand-edited.</span></div>`);
    renderGolden(payload);
  }

  let lastStewardItems = [];

  function stewardItemById(id) {
    return lastStewardItems.find((s) => s.steward_id === id)
      || ((lastFriendly && lastFriendly.steward_queue) || []).find((s) => s.steward_id === id)
      || null;
  }

  function valuesForAttribute(attribute) {
    const seen = new Map();
    activeFragments()
      .filter((f) => f.attribute === attribute)
      .forEach((f) => {
        if (!seen.has(f.value)) seen.set(f.value, f.source);
      });
    return [...seen.entries()].map(([value, source]) => ({ value, source }));
  }

  function ownerLinesHtml(item) {
    return `<div class="f-steward-meta">Owner: ${esc(item.owner || "steward-001")}</div>`
      + `<div class="f-steward-meta">Decide by: ${esc(item.deadline || "—")}</div>`
      + `<div class="f-steward-meta">If no decision: ${esc(item.fallback || "do not use this value")}</div>`;
  }

  function stewardQuestionHtml(item) {
    const frag = fragmentForAttribute(item.attribute);
    const what = frag
      ? `"${frag.value}" for ${plainAttr(item.attribute)} came from ${plainSource(frag.source).toLowerCase()} and could not be proven.`
      : `${plainAttr(item.attribute)} could not be proven.`;
    return `<div class="f-steward-q">${esc(what)}</div>` + ownerLinesHtml(item);
  }

  const confirmStash = new Map();

  function afterApprove(payload, id) {
    lastResolve = payload;
    setFriendlyFromResolve(payload);
    renderFriendly();
    renderEngine(payload);
    renderSteward(payload.steward_queue || []);
    renderMosaic(payload);
    renderEscalatedCount((payload.steward_queue || []).length);
    setStatus(`Steward approved ${id}; re-derived as a new fact.`);
    feed(`Steward approved ${id} as a new fact — re-derived, Decision: ${payload.decision_id}`);
  }

  async function doApprove(id, value, errEl) {
    try {
      const payload = await api(`/steward/${id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      afterApprove(payload, id);
    } catch (error) {
      errEl.hidden = false;
      errEl.textContent = `Approve failed (fail-closed): ${error.message}`;
    }
  }

  function stewardApproveAsk(id, btn) {
    const item = stewardItemById(id);
    if (!item) {
      setStatus(`Steward item ${id} not found.`);
      return;
    }
    const vals = valuesForAttribute(item.attribute);
    const options = vals.length
      ? vals.map((v, i) =>
          `<label class="check"><input type="radio" name="cf-${esc(id)}" value="${esc(v.value)}"${i === 0 ? " checked" : ""} /> ${esc(v.value)} — ${esc(plainSource(v.source))}</label>`).join("")
      : `<label>Value to approve (no existing value found)<input data-cf-value style="width:100%" value="" /></label>`;
    const card = `<div class="f-steward-q">Use this value for ${esc(plainAttr(item.attribute))}?</div>`
      + ownerLinesHtml(item)
      + `<div class="f-confirm-vals">${options}</div>`
      + `<div class="steward-actions"><button data-cf-yes class="btn-approve" type="button">Use this value</button>`
      + `<button data-cf-no type="button">Cancel</button></div>`
      + `<div class="meta" data-cf-err hidden></div>`;
    const friendly = btn && btn.closest("#f-answer");
    if (friendly) {
      const body = $("f-answer-body");
      body.innerHTML = card;
      wireApproveCard(body, id, vals.length > 0, true);
      return;
    }
    const holder = btn ? btn.closest("[data-steward]") : null;
    if (!holder) return;
    if (!confirmStash.has(holder)) confirmStash.set(holder, holder.innerHTML);
    holder.innerHTML = card;
    wireApproveCard(holder, id, vals.length > 0, false);
  }

  function wireApproveCard(root, id, hasValues, friendly) {
    const errEl = root.querySelector("[data-cf-err]");
    root.querySelector("[data-cf-no]").addEventListener("click", () => {
      if (friendly) {
        renderFriendly();
      } else {
        const h = confirmStash.get(root);
        confirmStash.delete(root);
        if (h != null) {
          root.innerHTML = h;
          bindStewardActions(root);
        }
      }
    });
    root.querySelector("[data-cf-yes]").addEventListener("click", () => {
      let value;
      if (hasValues) {
        const checked = root.querySelector(`input[name="cf-${CSS.escape(id)}"]:checked`);
        value = checked ? checked.value : "";
      } else {
        value = root.querySelector("[data-cf-value]").value.trim();
      }
      if (!value) {
        errEl.hidden = false;
        errEl.textContent = "Pick a value first — nothing was sent.";
        return;
      }
      doApprove(id, value, errEl);
    });
  }

  function nextTaxonomyVersion(item) {
    const bump = (v) => {
      const m = /-v(\d+)$/.exec(v || "");
      return m ? String(v).replace(/-v\d+$/, `-v${parseInt(m[1], 10) + 1}`) : null;
    };
    // Only reuse the known version when it belongs to the same entity family;
    // a user taxonomy must never seed a product version (or vice versa).
    const known = loop.taxonomy || (lastFriendly && lastFriendly.taxonomy_version) || "";
    const sameFamily = known.indexOf(`tax-${item.entity_type}-`) === 0 ? known : "";
    return bump(sameFamily)
      || `tax-${item.entity_type}-${new Date().toISOString().slice(0, 10)}-v2`;
  }

  function taxonomyInForce(item) {
    const known = loop.taxonomy || (lastFriendly && lastFriendly.taxonomy_version) || "";
    return known.indexOf(`tax-${item.entity_type}-`) === 0 ? known : "unknown";
  }

  function stewardAcceptAsk(id, suggested, btn) {
    const item = stewardItemById(id);
    if (!item) {
      setStatus(`Steward item ${id} not found.`);
      return;
    }
    const prefill = nextTaxonomyVersion(item);
    const inForce = taxonomyInForce(item);
    const card = `<div class="f-steward-q">Accept this suggestion?</div>`
      + `<div class="meta mono">Suggested: ${esc(suggested)}</div>`
      + ownerLinesHtml(item)
      + `<div class="meta">Taxonomy in force: <span class="mono">${esc(inForce)}</span></div>`
      + `<label>New taxonomy version (letters, digits, <span class="mono">._-</span>)`
      + `<input data-cf-version style="width:100%" value="${esc(prefill)}" /></label>`
      + `<div class="steward-actions"><button data-cf-yes class="btn-accept" type="button">Accept this suggestion</button>`
      + `<button data-cf-no type="button">Cancel</button></div>`
      + `<div class="meta" data-cf-err hidden></div>`;
    const friendly = btn && btn.closest("#f-answer");
    const host = friendly ? $("f-answer-body") : (btn ? btn.closest("[data-steward]") : null);
    if (!host) return;
    if (!friendly && !confirmStash.has(host)) confirmStash.set(host, host.innerHTML);
    host.innerHTML = card;
    const errEl = host.querySelector("[data-cf-err]");
    host.querySelector("[data-cf-no]").addEventListener("click", () => {
      if (friendly) {
        renderFriendly();
      } else {
        const h = confirmStash.get(host);
        confirmStash.delete(host);
        if (h != null) {
          host.innerHTML = h;
          bindStewardActions(host);
        }
      }
    });
    host.querySelector("[data-cf-yes]").addEventListener("click", () => {
      const version = host.querySelector("[data-cf-version]").value.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(version)) {
        errEl.hidden = false;
        errEl.textContent = "Version must look like tax-user-2026-10-28-v2: letters, digits, . _ - (must not start with . _ -).";
        return;
      }
      doAccept(id, suggested, version, errEl);
    });
  }

  async function doAccept(id, action, version, errEl) {
    try {
      const payload = await api(`/steward/${id}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, taxonomy_version: version }),
      });
      setStatus(`Taxonomy ${version} created; re-derived. Golden: ${payload.golden ? "published" : "withheld"}.`);
      feed(`Steward accepted taxonomy suggestion — new version ${version}; re-derived, no hand edit`);
      dropFriendlySteward(id);
      loadPending();
    } catch (error) {
      errEl.hidden = false;
      errEl.textContent = `Accept failed (fail-closed): ${error.message}`;
    }
  }

  function dropFriendlySteward(id) {
    if (lastFriendly && lastFriendly.steward_queue) {
      lastStewardItems = lastStewardItems.filter((s) => s.steward_id !== id);
      lastFriendly.steward_queue = lastFriendly.steward_queue.filter((s) => s.steward_id !== id);
      renderSteward(lastStewardItems.length ? lastStewardItems : lastFriendly.steward_queue);
      renderEscalatedCount(lastFriendly.steward_queue.length);
      renderFriendly();
    } else {
      loadPending();
    }
  }

  async function stewardSplit(id) {
    const payload = await api(`/steward/${id}/split`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restore_version: 1 }),
    });
    setStatus(`Split restored v1 as v${payload.golden_version} (decision ${payload.decision_id}).`);
    feed(`Steward split identity — restored v1 as v${payload.golden_version}, Decision: ${payload.decision_id}`);
    dropFriendlySteward(id);
  }

  function stewardCardHtml(item) {
    return `
      <div class="card warn" data-steward="${esc(item.steward_id)}">
        <h3>${esc(item.attribute)} <span class="muted mono">${esc(item.status)}</span></h3>
        <div class="meta">owner ${esc(item.owner)} · deputy ${esc(item.deputy || "—")}</div>
        <div class="meta">deadline <span class="mono">${esc(item.deadline)}</span></div>
        <div class="meta">fallback: ${esc(item.fallback)}</div>
        ${item.suggested_action ? `<div class="meta">suggested: <span class="mono">${esc(item.suggested_action)}</span></div>` : ""}
        <div class="steward-actions">
          <button class="btn-approve" data-approve="${esc(item.steward_id)}">Approve w/ value…</button>
          <button class="btn-reject" data-reject="${esc(item.steward_id)}">Reject</button>
          <button class="btn-split" data-split="${esc(item.steward_id)}">Split to v1</button>
          ${item.suggested_action ? `<button class="btn-accept" data-accept="${esc(item.steward_id)}" data-action="${esc(item.suggested_action)}">Accept suggested</button>` : ""}
        </div>
      </div>`;
  }

  function bindStewardActions(root) {
    root.querySelectorAll("[data-accept]").forEach((btn) => {
      btn.addEventListener("click", () => stewardAcceptAsk(btn.getAttribute("data-accept"), btn.getAttribute("data-action"), btn));
    });
    root.querySelectorAll("[data-reject]").forEach((btn) => {
      btn.addEventListener("click", () => stewardReject(btn.getAttribute("data-reject")));
    });
    root.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.addEventListener("click", () => stewardApproveAsk(btn.getAttribute("data-approve"), btn));
    });
    root.querySelectorAll("[data-split]").forEach((btn) => {
      btn.addEventListener("click", () => stewardSplit(btn.getAttribute("data-split")));
    });
  }

  function renderSteward(items) {
    pendingAll = items;
    lastStewardItems = items;
    renderEscalatedCount(items.length);
    if (!items.length) {
      stewardBody.innerHTML = `<div class="card"><span class="muted">Queue empty — nothing escalated.</span></div>`;
      $("steward-drawer-list").innerHTML = `<div class="card"><span class="muted">No pending steward items. All escalated attributes have been decided.</span></div>`;
      return;
    }
    const first = items[0];
    const rule = first.rule_fired || first.reason_code || "escalated";
    stewardBody.innerHTML = `
      <div class="exception-card" role="alert">
        <div>⚠ ${items.length} escalation${items.length > 1 ? "s" : ""} need${items.length > 1 ? "" : "s"} a human — ${esc(first.attribute)} — ${esc(rule)} — Steward Queue (${items.length})</div>
        <div class="steward-actions" style="margin-top:0">
          <button id="exception-review" type="button">Review in Steward Queue</button>
        </div>
      </div>`;
    $("exception-review").addEventListener("click", openDrawer);
    const drawerList = $("steward-drawer-list");
    drawerList.innerHTML = items.map(stewardCardHtml).join("");
    bindStewardActions(drawerList);
  }

  async function loadPending() {
    try {
      const data = await api("/steward/pending");
      if (!lastResolve) renderSteward(data.pending || []);
    } catch (error) {
      setStatus(`Steward queue unreadable: ${error.message}`);
    }
  }

  function renderGraph(payload, highlight) {
    const svg = $("graph-canvas");
    const nodes = payload.nodes || [];
    const edges = payload.edges || [];
    const W = Math.max(svg.clientWidth || 900, 400);
    const H = 300;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const byType = {};
    nodes.forEach((n) => { (byType[n.node_type] = byType[n.node_type] || []).push(n); });
    const types = Object.keys(byType);
    const pos = {};
    types.forEach((t, ti) => {
      byType[t].forEach((n, i) => {
        pos[n.node_id] = {
          x: 60 + (ti * (W - 120)) / Math.max(types.length - 1, 1),
          y: 40 + (i * (H - 80)) / Math.max(byType[t].length - 1, 1),
        };
      });
    });
    const color = { Fragment: "#38bdf8", Entity: "#a78bfa", Golden: "#34d399", Taxonomy: "#fbbf24", Steward: "#f472b6", Document: "#475569" };
    const edgeColor = { has_attribute: "#334155", proposed_link: "#a78bfa", verified_link: "#34d399", purchased: "#fbbf24", same_person: "#7dd3fc", link_miss: "#f87171", cluster_member: "#a78bfa" };
    const edgeDashed = { proposed_link: "5,4", cluster_member: "5,4" };
    let html = "";
    edges.forEach((e) => {
      const a = pos[e.from_node_id];
      const b = pos[e.to_node_id];
      if (!a || !b) return;
      const hl = highlight && (highlight.includes(e.from_node_id) || highlight.includes(e.to_node_id));
      html += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${edgeColor[e.edge_type] || "#475569"}" stroke-width="${hl ? 3 : 1.2}" stroke-dasharray="${edgeDashed[e.edge_type] || "none"}" opacity="${hl ? 1 : 0.7}"><title>${esc(e.edge_type)}${e.proof ? ": " + esc(e.proof) : ""} (${esc(e.proposer || "?")})</title></line>`;
    });
    nodes.forEach((n) => {
      const p = pos[n.node_id];
      if (!p) return;
      const label = (n.node_id.split("-").slice(-2).join("/") || n.node_type).slice(-18);
      html += `<g data-node="${esc(n.node_id)}" style="cursor:pointer">`
        + `<circle cx="${p.x}" cy="${p.y}" r="11" fill="${color[n.node_type] || "#64748b"}" stroke="#ffffff" stroke-width="2.5"><title>${esc(n.node_id)} [${esc(n.node_type)}]${n.source ? " · " + esc(n.source) : ""}</title></circle>`
        + `<text x="${p.x + 15}" y="${p.y + 4}" font-size="11" font-weight="600" fill="#1E293B" stroke="#ffffff" stroke-width="3" paint-order="stroke">${esc(label)}</text></g>`;
    });
    svg.innerHTML = html;
    $("graph-info").textContent = `${nodes.length} nodes, ${edges.length} edges. Solid = verified, dashed = proposed.`;
    svg.querySelectorAll("[data-node]").forEach((g) => {
      g.addEventListener("click", () => {
        const id = g.getAttribute("data-node");
        const node = nodes.find((n) => n.node_id === id);
        $("graph-detail").textContent = JSON.stringify(node, null, 1);
      });
    });
    return { nodes, edges };
  }

  let lastGraph = { nodes: [], edges: [] };

  async function loadGraph() {
    const { entityType, entityId } = currentEntity();
    try {
      const data = await api(`/graph/${entityType}/${entityId}`);
      lastGraph = { nodes: data.nodes || [], edges: data.edges || [] };
      renderGraph(data, null);
      // Pathfinder F1–F5 only applies to the Hidden Connection entity.
      const ids = (data.nodes || []).map((n) => n.node_id || "");
      const hasChain = ["F1", "F2", "F3", "F4", "F5"].every((f) =>
        ids.some((id) => id.includes(`-${f}-`) || id.endsWith(`-${f}`) || id.includes(`(${f})`)));
      $("graph-path").hidden = !hasChain;
      setStatus(`Graph loaded: ${lastGraph.nodes.length} nodes, ${lastGraph.edges.length} edges.`);
    } catch (error) {
      setStatus(`Graph failed: ${error.message}`);
    }
  }

  async function graphPath() {
    const { entityType, entityId } = currentEntity();
    try {
      const from = await inlinePrompt("Pathfinder from node id:", "nd-fragment-user-P-001-sf-001-phone");
      const to = await inlinePrompt("...to node id:", "nd-fragment-user-P-001-tck-001-phone");
      if (!from || !to) return;
      const data = await api(`/graph/${entityType}/${entityId}?path_from=${encodeURIComponent(from)}&path_to=${encodeURIComponent(to)}`);
      lastGraph = { nodes: data.nodes || [], edges: data.edges || [] };
      renderGraph(data, data.path || []);
      $("graph-detail").textContent = "Path:\n" + (data.path || []).join("\n") || "No path (disconnected).";
    } catch (error) {
      setStatus(`Pathfinder failed: ${error.message}`);
    }
  }

  async function graphImpact() {
    const { entityType, entityId } = currentEntity();
    try {
      const field = await inlinePrompt("Blast radius for golden field:", "phone");
      if (!field) return;
      const data = await api(`/graph/${entityType}/${entityId}?impact=${encodeURIComponent(field)}`);
      lastGraph = { nodes: data.nodes || [], edges: data.edges || [] };
      renderGraph(data, (data.impact || {}).downstream_nodes || []);
      $("graph-detail").textContent = "Blast radius of " + field + ":\n" + JSON.stringify(data.impact || {}, null, 1);
    } catch (error) {
      setStatus(`Impact failed: ${error.message}`);
    }
  }

  function renderMatchingEngine(splink) {
    const info = splink || {};
    const status = info.status || "unavailable";
    const ok = status === "ok";
    const ver = [["Splink", loop.splink.splink_version || info.splink_version],
                 ["DuckDB", loop.splink.duckdb_version || info.duckdb_version]]
      .filter(([, v]) => v).map(([n, v]) => `${n} ${v}`).join(" · ");
    const clusters = info.clusters || [];
    const pairs = info.pairs || [];
    const clusterHtml = clusters.map((c) => `
      <div class="meta mono">${esc(c.cluster_id)} — ${(c.member_ids || []).length} fragments: ${(c.member_ids || []).map(esc).join(", ")}</div>`).join("");
    const pairHtml = pairs.map((p) => `
      <div class="meta mono">${esc(p.left)} ↔ ${esc(p.right)} · weight ${esc(p.match_weight ?? "?")} · prob ${esc(p.match_probability ?? "?")} · via ${esc(p.blocking_rule || "?")} (${esc(p.proposer || "Splink")})</div>`).join("");
    const card = document.createElement("div");
    card.className = ok ? "card plum" : "card warn";
    card.innerHTML = `
      <h3>Matching Engine ${ok
        ? badge("published")
        : `<span class="badge escalated">DOWN</span>`}
        <span class="muted mono">${esc(ver || "versions from /health")}</span></h3>
      ${ok
        ? `<div class="meta">${clusters.length} clusters, ${pairs.length} pairs — candidates only, never a golden.</div>${clusterHtml}${pairHtml}`
        : `<div class="meta">SPLINK_UNAVAILABLE — fallback to Muse + fuzzy. No publish on fallback alone.</div>`}
      ${info.detail ? `<div class="meta">${esc(info.detail)}</div>` : ""}`;
    proposerBody.prepend(card);
  }

  async function buildMosaic(auto) {
    fState.working = true;
    renderFStatus();
    const { entityType } = currentEntity();    const docsBySource = {};
    for (const f of activeFragments()) {
      (docsBySource[f.source] = docsBySource[f.source] || { source: f.source, fields: {} });
    }
    const documents = Object.values(docsBySource).map((d, i) => ({
      document_id: `DOC-${i + 1}`,
      entity_type: entityType,
      source: d.source,
      synthetic: true,
      fields: Object.fromEntries(
        activeFragments().filter((f) => f.source === d.source).map((f) => [f.attribute, {
          value: f.value, authority_rank: f.authority_rank, event_time: f.event_time,
          verified: f.verified, ttl_seconds: f.ttl_seconds,
        }])
      ),
    }));
    try {
      const payload = await api("/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          work_id: `ui-build-${Date.now()}`,
          idempotency_key: `ui-build-${Date.now()}`,
          dataset: { documents },
        }),
      });
      lastResolve = payload;
      setStatus(`Build ${payload.decision_id} — verdict: ${payload.verdict} (${payload.splink_status}).`);
      setFriendlyFromBuild(payload);
      markRan();
      renderFriendly();
      await loadGraph();
      const first = (payload.goldens || []).find((g) => g.golden_id) || null;
      if (first) {
        renderGolden(first);
        renderEngine({ ...payload, golden: first, policy_version: payload.policy_version_user });
        renderMosaic({ ...payload, golden: first, proposal: { attributes: {} } });
      }
      renderSteward(payload.steward_queue || []);
      renderEscalatedCount((payload.steward_queue || []).length);
      renderMatchingEngine(payload.splink);
      if (payload.splink && payload.splink.status === "ok") {
        const nPairs = (payload.splink.pairs || []).length;
        const nClusters = (payload.splink.clusters || []).length;
        feed(`Matching engine matched: ${nClusters} clusters, ${nPairs} pairs — candidates only, engine still verifies`);
      } else {
        feed(`Matching engine unavailable — fallback to Muse + fuzzy (SPLINK_UNAVAILABLE), nothing published on fallback alone`);
      }
      const verdictCap = String(payload.verdict || "").charAt(0).toUpperCase() + String(payload.verdict || "").slice(1);
      if (auto) {
        noteAutoRun({ ...payload, golden: first, verified_links: payload.verified_links || [] });
        feed(`Engine derived: cluster — ${verdictCap} — Decision: ${payload.decision_id}`);
      } else {
        feed(`Manual Build Mosaic (click) — Decision: ${payload.decision_id} — Verdict: ${verdictCap}`);
      }
    } catch (error) {
      setStatus(`Build failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto build failed (fail-closed): ${error.message} — nothing published`);
      fState.working = false;
      renderFStatus();
      return;
    }
    fState.working = false;
    renderFStatus();
  }

  async function uploadFile(auto, fileInput) {
    const input = fileInput || $("upload-file");
    if (!input.files || !input.files.length) {
      setStatus("Choose a JSON file first (or use a template).");
      return null;
    }
    const fileName = input.files[0].name;
    if (auto) {
      feed(`Upload received — ${fileName} — auto-run started, no click needed`);
    }
    const form = new FormData();
    form.append("file", input.files[0]);
    try {
      const payload = await api("/upload", { method: "POST", body: form });
      setStatus(`Upload parsed: ${payload.fragments_parsed} fragments, type ${payload.detected_entity_type} (${payload.detected_confidence}). `
        + `${payload.link_proposals.length} link proposals. Use "Resolve upload" to derive.`);
      feed(`Scout detected: ${payload.detected_entity_type} — ${payload.fragments_parsed} fragments, attribute map proposed`);
      feed(`Linker proposed: ${(payload.link_proposals || []).length} link(s) — approved config in force, no new approval needed`);
      window.__uploadedFragments = payload.fragments || [];
      window.__uploadedEntityType = payload.detected_entity_type === "product" ? "product" : "user";
      return payload;
    } catch (error) {
      setStatus(`Upload rejected: ${error.message}`);
      feed(`Upload rejected: ${error.message} — nothing proposed`);
      return null;
    }
  }

  async function resolveUpload(auto) {
    const list = window.__uploadedFragments || [];
    if (!list.length) {
      setStatus("Upload a file first.");
      return;
    }
    // The answer below is about the uploaded pieces, so the Friendly View
    // must describe those — not the fixture fragments for this entity.
    fragments = list.slice();
    tickets = [];
    fState.working = true;
    renderFStakes();
    renderFPieces();
    renderFStatus();
    try {
      const { entityId } = currentEntity();
      const key = `ui-upload-${Date.now()}`;
      const payload = await api("/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          work_id: `ui-${key}`,
          entity_type: window.__uploadedEntityType || "user",
          entity_id: entityId,
          idempotency_key: key,
          fragments: list,
        }),
      });
      lastResolve = payload;
      if (payload.taxonomy_version) {
        loop.taxonomy = payload.taxonomy_version;
        renderTaxonomyBadge();
      }
      renderProposal(payload.proposal);
      renderEngine(payload);
      renderSteward(payload.steward_queue || []);
      renderMosaic(payload);
      renderEscalatedCount((payload.steward_queue || []).length);
      const verdictCap = payload.verdict.charAt(0).toUpperCase() + payload.verdict.slice(1);
      if (auto) {
        noteAutoRun(payload);
        feed(`Engine derived: ${entityId} — ${verdictCap} — Decision: ${payload.decision_id}`);
        const firstConflict = (payload.conflicts || [])[0];
        if (firstConflict) {
          feed(`${firstConflict.attribute} escalated — ${firstConflict.rule_fired} — steward item created, loop continued on other fields`);
        }
        await askDownstream((window.__uploadedEntityType || "user") === "product" ? "search" : "otp", { auto: true });
      } else {
        feed(`Manual upload resolve (click) — Decision: ${payload.decision_id} — Verdict: ${verdictCap}`);
      }
      setStatus(`Upload resolved — decision ${payload.decision_id}, verdict ${payload.verdict}.`);
      setFriendlyFromResolve(payload);
      markRan();
      renderFriendly();
    } catch (error) {
      setStatus(`Upload resolve failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto run failed (fail-closed): ${error.message} — nothing published`);
    } finally {
      fState.working = false;
      renderFStatus();
    }
  }

  async function askDownstream(agent, opts) {
    const auto = !!(opts && opts.auto);
    const { entityType, entityId } = currentEntity();
    const field = agent === "search" ? "category" : "phone";
    try {
      const payload = await api(`/downstream/${agent}?entity_type=${entityType}&entity_id=${entityId}&field=${field}`);
      fState.downstream = {
        agent, answer: payload.answer, field: payload.field,
        value: payload.value, reason: payload.reason,
      };
      renderFriendly();
      $("downstream-answer").textContent = JSON.stringify(payload, null, 1);
      const label = agent === "search" ? `Search uses ${payload.value}` : `OTP uses ${payload.value}`;
      if (payload.answer === "golden") {
        $("downstream-pill").textContent = `Downstream: ${label}`;
        $("downstream-log").textContent =
          `Downstream agent called GET /golden/${entityType}/${entityId} — Decision: ${payload.decision_id} — then acted (${label})`;
        if (auto) {
          feed(`Downstream agent called GET /golden/${entityType}/${entityId} — Decision: ${payload.decision_id} — then acted (${label})`);
        }
        setStatus(payload.answer === "golden"
          ? `Downstream ${agent}: MAY ACT on ${payload.field} (${payload.value}).`
          : `Downstream ${agent}: STOP — ${payload.reason}. ${payload.fallback || ""}`);
      } else {
        $("downstream-pill").textContent = "Downstream: No trusted value";
        $("downstream-log").textContent =
          `Downstream agent called GET /golden/${entityType}/${entityId} — No trusted value — downstream stopped.`;
        if (auto) {
          feed(`Downstream agent called GET /golden/${entityType}/${entityId} — No trusted value — downstream stopped.`);
        }
        setStatus(payload.answer === "golden"
          ? `Downstream ${agent}: MAY ACT on ${payload.field} (${payload.value}).`
          : `Downstream ${agent}: STOP — ${payload.reason}. ${payload.fallback || ""}`);
      }
      return payload;
    } catch (error) {
      setStatus(`Downstream ${agent} failed: ${error.message}`);
      return null;
    }
  }

  async function explain(decisionId, field) {
    const data = await api(`/explain/${decisionId}`);
    $("explain-id").textContent = decisionId;
    const tree = (data.derivation_tree || []).join("\n");
    const trace = (data.subagent_trace || []).map((s) =>
      `▸ ${s.agent || "?"}${s.real_call ? " [REAL]" : " [mock]"} — ${s.output_summary || ""}`).join("\n");
    const wonder = data.wonder_note ? `Wonder: ${JSON.stringify(data.wonder_note)}\n\n` : "";
    const head = field ? `Proof for: ${field}\n\n` : "";
    const text = head
      + `RULE ${data.rule_fired} · POLICY ${data.policy_version} · TAXONOMY ${data.taxonomy_version || "—"}\n\n`
      + `DERIVATION TREE\n${tree || "(empty)"}\n\n`
      + `SUBAGENT TRACE\n${trace || "(none)"}\n\n`
      + wonder
      + JSON.stringify(data, null, 1);
    // Highlight the lineage row(s) for the requested field; the full proof
    // stays visible (highlighting is in addition, not instead).
    const key = field ? `"${field}"` : null;
    $("explain-body").innerHTML = text.split("\n").map((line) =>
      (key && line.includes(key) ? `<mark class="f-hit">${esc(line)}</mark>` : esc(line))
    ).join("\n");
    dialog.showModal();
    const hit = $("explain-body").querySelector("mark");
    if (hit) hit.scrollIntoView({ block: "center" });
  }

  function renderMosaic(payload) {    mosaic.innerHTML = "";
    const tiles = [];
    const golden = payload.golden || { attributes: {} };
    for (const [name, value] of Object.entries(golden.attributes || {})) {
      if (name === "purchased_products" && Array.isArray(value)) {
        value.forEach((item, i) => tiles.push({
          kind: "user", label: `purchased[${i}]=${item.category || "?"}`, wide: false,
          detail: { nested: item },
        }));
        continue;
      }
      tiles.push({ kind: "gold", label: `${name}=${value.value}`, wide: false, detail: value });
    }
    const proposal = payload.proposal || { attributes: {} };
    for (const [name, attr] of Object.entries(proposal.attributes || {})) {
      tiles.push({ kind: "agent", label: `Muse proposed ${name}`, wide: false, detail: attr });
    }
    for (const [name, value] of Object.entries(golden.attributes || {})) {
      if (name === "purchased_products") continue;
      tiles.push({ kind: "rule", label: `${value.rule_fired} fired for ${name}`, wide: false, detail: value });
    }
    for (const link of (payload.splink_links || [])) {
      tiles.push({ kind: "agent", label: `Matching engine candidate: ${link.proof}`, wide: false, detail: link });
    }
    for (const item of payload.steward_queue || []) {
      tiles.push({ kind: "rule", label: `steward: ${item.attribute}`, wide: true, detail: item });
    }
    tiles.forEach((tile, index) => {
      const button = document.createElement("button");
      button.className = `tile ${tile.kind}${tile.wide ? " wide" : ""}`;
      button.title = tile.label;
      button.setAttribute("aria-label", tile.label);
      button.addEventListener("click", () => {
        tileDetail.textContent = JSON.stringify({ tile: tile.label, ...tile.detail }, null, 2);
      });
      mosaic.appendChild(button);
      if (index === 0) tileDetail.textContent = `Mosaic for decision ${payload.decision_id} — click any tile.`;
    });
    if (!tiles.length) {
      tileDetail.textContent = "No tiles: nothing derived yet.";
    }
  }

  entitySel.addEventListener("change", onEntityChange);
  ticketBox.addEventListener("change", renderFragments);
  refreshBtn.addEventListener("click", loadFragments);
  resolveBtn.addEventListener("click", () => resolve(false));
  $("build").addEventListener("click", () => buildMosaic(false));
  $("upload-btn").addEventListener("click", async () => {
    await uploadFile(false);
    if ((window.__uploadedFragments || []).length) {
      await resolveUpload(false);
    }
  });
  $("upload-file").addEventListener("change", async () => {
    if (loop.mode !== "running") {
      setStatus("Loop Paused: file staged — click Upload to run manually.");
      return;
    }
    const uploaded = await uploadFile(true);
    if (uploaded && (window.__uploadedFragments || []).length) {
      await resolveUpload(true);
    }
  });
  $("graph-load").addEventListener("click", loadGraph);
  $("graph-path").addEventListener("click", graphPath);
  $("graph-impact").addEventListener("click", graphImpact);
  $("downstream-otp").addEventListener("click", () => askDownstream("otp"));
  $("downstream-search").addEventListener("click", () => askDownstream("search"));
  $("pause-btn").addEventListener("click", toggleLoop);
  $("f-pause").addEventListener("click", toggleLoop);
  $("f-loop-review").addEventListener("click", openDrawer);
  $("f-try-answer").addEventListener("click", () => resolve(false));
  $("f-upload-scroll").addEventListener("click", () => {
    $("f-upload").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("f-show-detail").addEventListener("click", () => showDetail(true));
  $("f-show-detail-2").addEventListener("click", () => showDetail(true));
  $("f-back").addEventListener("click", () => showDetail(false));
  $("f-back-top").addEventListener("click", () => showDetail(false));
  $("f-loop-more-btn").addEventListener("click", () => {
    const more = $("f-loop-more");
    const items = [...$("feed-list").children].slice(0, 3);
    more.innerHTML = items.map((li) => `<li>${esc(li.textContent)}</li>`).join("")
      || `<li>No autonomous steps yet.</li>`;
    more.hidden = !more.hidden;
    $("f-loop-more-btn").textContent = more.hidden ? "See what it did by itself" : "Hide";
  });
  $("f-try-example").addEventListener("click", () => resolve(false));
  $("f-upload-btn").addEventListener("click", async () => {
    await uploadFile(false, $("f-upload-file"));
    if ((window.__uploadedFragments || []).length) {
      await resolveUpload(false);
    }
  });
  $("f-upload-file").addEventListener("change", async () => {
    if (loop.mode !== "running") {
      setStatus("Loop Paused: file staged — click Upload to run manually.");
      return;
    }
    const uploaded = await uploadFile(true, $("f-upload-file"));
    if (uploaded && (window.__uploadedFragments || []).length) {
      await resolveUpload(true);
    }
  });
  $("steward-open").addEventListener("click", openDrawer);
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-scrim").addEventListener("click", closeDrawer);
  $("feed-toggle-inline").addEventListener("click", () => {
    const list = $("feed-list");
    const hidden = list.style.display === "none";
    list.style.display = hidden ? "" : "none";
    $("feed-toggle-inline").textContent = hidden ? "Activity feed: shown below" : "Activity feed: hidden";
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });

  renderLoop();
  applyPreRunState();
  renderFriendly();
  feed("Autonomous Loop started — Running. Next auto check: on next upload.");
  refreshHealth();
  window.setInterval(refreshHealth, 10000);
  loadEntities()
    .catch(() => {})
    .finally(() => loadFragments().catch((error) => setStatus(`Cannot load fragments: ${error.message}`)));
  loadPending();
})();
