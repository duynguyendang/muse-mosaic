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
  // Shared constants and pure label helpers live in config.js.
  const API_BASE = window.MosaicConfig.API_BASE;
  const esc = window.MosaicConfig.esc;
  const plainSource = window.MosaicConfig.plainSource;
  const plainAttr = window.MosaicConfig.plainAttr;
  const SRC_COLOR = window.MosaicConfig.SRC_COLOR;
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
    const c2 = $("steward-count-2");
    if (c2) c2.textContent = String(k);
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

  function setFriendlyFromResolve(payload) {
    const sim = $("f-simulation");
    if (sim) sim.hidden = true;
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
    window.__lastGolden = payload.golden || null;
    window.__lastProposals = payload.proposal ? [payload.proposal] : [];
    window.__lastGoldens = payload.golden ? [payload.golden] : [];
  }

  function setFriendlyFromBuild(payload) {
    const first = (payload.goldens || []).find((g) => g.golden_id) || null;
    const sim = $("f-simulation");
    if (sim) sim.hidden = true;
    if (first && first.entity_type && first.entity_id) {
      fState.activeEntity = { entityType: first.entity_type, entityId: first.entity_id };
    }
    if (payload.taxonomy_version) {
      loop.taxonomy = payload.taxonomy_version;
      renderTaxonomyBadge();
    }
    lastFriendly = {
      verdict: payload.verdict,
      golden: first,
      steward_queue: payload.steward_queue || [],
      conflicts: payload.conflicts || [],
      decision_id: payload.decision_id,
      policy_version: payload.policy_version_user,
      taxonomy_version: payload.taxonomy_version || payload.taxonomy || (first && first.taxonomy_version) || loop.taxonomy,
    };
    fState.splinkPairs = (payload.splink && payload.splink.pairs) || [];
    fState.ran = true;
    // Ledger-backed stores for the deterministic chat (askAbout/fallback):
    // golden for attribute answers, proposals for the F6 trap evidence.
    window.__lastGolden = first;
    window.__lastProposals = payload.proposals || [];
    window.__lastGoldens = payload.goldens || [];
  }

  // After a /build, point the graph canvas at what was just built (first
  // golden's entity) so highlight, Explain and downstream all agree.
  async function loadGraphForBuild(payload) {
    const first = (payload.goldens || []).find((g) => g.golden_id) || null;
    if (first && first.entity_type && first.entity_id) {
      fState.activeEntity = { entityType: first.entity_type, entityId: first.entity_id };
      await loadGraph(first.entity_type, first.entity_id);
    } else {
      await loadGraph();
    }
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
    s1.querySelector(".lbl").textContent = "Messy pieces";
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
        // Live survivorship badges (Invariant D): after a run, every piece
        // shows its live verdict. Matched on exact attribute + value only —
        // raw names that never entered derivation get no badge, never a guess.
        let survive = "";
        if (f._trap) {
          survive = ` <span class="tag">✕ Excluded Trap</span>`;
        } else {
          const golden = lastFriendly && lastFriendly.golden && lastFriendly.golden.attributes;
          const matchFactId = (fid) => fid && f.source_id && (fid === f.source_id || fid.endsWith(`-${f.source_id}`));
          const byFactId = golden && Object.values(golden).find((v) =>
            v && typeof v === "object" && (
              matchFactId(v.winner_fact_id) || (v.loser_fact_ids || []).some(matchFactId)
            )
          );
          const field = byFactId || (golden && golden[f.attribute]);
          if (field && typeof field === "object" && field.value != null) {
            const rule = field.rule_fired || "R1";
            const isWin = byFactId
              ? matchFactId(field.winner_fact_id)
              : String(field.value) === String(f.value);
            survive = isWin
              ? ` <span class="tag">✓ Winner · ${esc(rule)}</span>`
              : ` <span class="tag">Tombstone → Survivor · ${esc(rule)}</span>`;
          } else {
            const item = ((lastFriendly && lastFriendly.steward_queue) || [])
              .find((s) => s.attribute === f.attribute);
            if (item) {
              survive = ` <span class="tag">⚠ Escalated · ${esc(item.rule_fired || item.reason_code || "R2")}</span>`;
            }
          }
        }
        // Friendly small text is one of two exact forms only, with plain
        // attribute names: verified rank-1 authority is "trusted for [name]",
        // unverified is "not proven yet". Verified non-rank-1 facts carry
        // no tag — rank numbers stay in Detail View.
        const tag = f.verified
          ? (f.authority_rank === 1 ? ` <span class="muted">(trusted for ${esc(plainAttr(f.attribute).toLowerCase())})</span>` : "")
          : ` <span class="muted">(not proven yet)</span>`;
        return `<div>${esc(plainAttr(f.attribute))} ${esc(f.value)}${tag}${conflict}${survive}</div>`;
      }).join("");
      return `<div class="f-piece"><span class="src"><span class="f-src-dot" style="background:${SRC_COLOR[source] || "#475569"}"></span>${esc(plainSource(source))} says:</span>${lines}</div>`;
    }).join("");
  }

  function fragmentForAttribute(attribute) {
    const list = activeFragments().filter((f) => f.attribute === attribute);
    return list.find((f) => !f.verified) || list[0] || null;
  }

  function fragmentForFactId(factId, attrName) {
    // Fact ids differ by path: /resolve uses "{entity}-{source_id}", /build
    // uses "{cluster}-{docId}-{rawAttr}". Match exact, entity-prefixed, and
    // suffix forms so the friendly sentence never loses its source. Returns
    // null (caller falls back to a generic line) when nothing matches —
    // never a guessed fragment.
    if (!factId) return null;
    const list = activeFragments();
    const { entityId } = currentEntity();
    return (
      list.find((f) => f.source_id && factId === f.source_id)
      || list.find((f) => f.source_id && factId === `${entityId}-${f.source_id}`)
      || list.find((f) => f.source_id && factId.endsWith(`-${f.source_id}`))
      || list.find((f) => f._docId && attrName && factId.endsWith(`-${f._docId}-${attrName}`))
      || null
    );
  }

  function friendlyWhy(attribute, value) {
    // Plain-language proof sentence, built only from the golden field's own
    // data (winner fact id -> fragment source/rank/verified/event time).
    // Never leaks rule_fired, winner_fact_id, or policy_version here.
    if (!value || typeof value !== "object") return "Checked by the engine.";
    const attrPlain =
      (attribute === "purchased_products" ? "purchase" : plainAttr(attribute)).toLowerCase();
    const winner = fragmentForFactId(value.winner_fact_id, attribute);
    if (!winner) return "Checked by the engine.";
    const src = plainSource(winner.source);
    const loserSet = new Set(value.loser_fact_ids || []);
    const sameAttr = activeFragments().filter((f) =>
      f.attribute === attribute
      || (f.source_id && [...loserSet].some((id) => id === f.source_id || id.endsWith(`-${f.source_id}`)))
    );
    const newer = sameAttr.filter(
      (f) => Date.parse(f.event_time) > Date.parse(winner.event_time)
    );
    const newerSrc =
      newer.length > 0 ? plainSource(newer[newer.length - 1].source) : null;
    const evenThough = newerSrc ? ` Even though ${newerSrc} is newer.` : "";
    if (winner.authority_rank === 1) {
      return `${src} wins because it is trusted for ${attrPlain}.${evenThough}`;
    }
    if (winner.verified) {
      return `${src} wins because it is proven for ${attrPlain}.${evenThough}`;
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
        ? `<div class="f-field">Step 3: Safe to act — ${esc(who)} sent to ${esc(d.value)}.</div>`
        : `<div class="f-field">Step 3: Downstream stopped — Needs a human before sending ${esc(who === "Search" ? "results" : "OTP")}.</div>`;
    }
    // Engine-down card: red stop state with an inline restore action.
    if (loop.engineDown) {
      box.classList.add("stop");
      setFHead("■ Engine down", " — nothing publishes");
      body.innerHTML = `<div>No new golden record can be published. Trust-engine is required, not optional.</div>`
        + `<div class="steward-actions"><button data-f-restore type="button">Restore Trust Engine &amp; Retry Same Key</button></div>`
        + downstreamLine;
      const rs = body.querySelector("[data-f-restore]");
      if (rs) rs.addEventListener("click", async () => {
        await api("/demo/engine/start", { method: "POST" });
        await refreshHealth();
        setStatus("Same decision id returned. No double derivation.");
        feed("Demo: trust-engine started (DEMO ONLY) — retry with the same key to prove the same decision id.");
        renderFAnswer();
      });
      return;
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
          ? `<div class="f-field"><button data-f-proof3 type="button">Proof: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`
          : "")
        + downstreamLine;
      MosaicDrawers.bindStewardActions(drawerCtx, body);
      const pb3 = body.querySelector("[data-f-proof3]");
      if (pb3) pb3.addEventListener("click", () => explain(f.decision_id));
      const fa = body.querySelector("[data-f-accept]");
      if (fa) fa.addEventListener("click", () =>
        MosaicDrawers.stewardAcceptAsk(drawerCtx, fa.getAttribute("data-f-accept"), fa.getAttribute("data-action"), fa));
      return;
    }
    const golden = f.golden;
    if (golden && Object.keys(golden.attributes || {}).length > 0) {
      box.classList.add("ok");
      setFHead("✓ Trusted picture", ` — ${f.golden.entity_id || entityId}${f.decision_id ? ` [Proof: ${String(f.decision_id).slice(0, 8)}…]` : ""}`);
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
          + `<button data-f-explain="${esc(name)}" type="button">Why this value? · Proof ${esc(String(f.decision_id || "").slice(0, 8))}…</button></div>`;
      }).join("");
      body.innerHTML = fields + downstreamLine;
      if (f.decision_id) {
        body.insertAdjacentHTML("beforeend",
          `<div class="f-field"><button data-f-explain type="button">Proof: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`);
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
        ? `<div class="f-field"><button data-f-proof2 type="button">Proof: ${esc(String(f.decision_id).slice(0, 8))}… — see full lineage</button></div>`
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
    // Active-cluster badge: after a build, the UI follows the built entity
    // (e.g. user/cluster-1), even when the fixture selector shows another.
    const activeBadge = $("entity-active");
    if (activeBadge) {
      if (fState.activeEntity) {
        activeBadge.hidden = false;
        activeBadge.textContent = `Active: ${fState.activeEntity.entityType}/${fState.activeEntity.entityId}`;
      } else {
        activeBadge.hidden = true;
        activeBadge.textContent = "";
      }
    }
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
    let response;
    try {
      response = await fetch(API_BASE + path, options);
    } catch (error) {
      // Network failure: the ledger cannot be proven reachable. Fail closed
      // in the UI too — banner + disabled publish until the API is back.
      const banner = $("engine-banner");
      if (banner) {
        banner.style.display = "block";
        banner.textContent = "Ledger unavailable. No publish can be proven. Build Mosaic is disabled until the ledger is back.";
      }
      const buildBtn = $("build");
      if (buildBtn) buildBtn.disabled = true;
      if (resolveBtn) resolveBtn.disabled = true;
      throw new Error(`mosaic-api unreachable: ${error.message}`);
    }
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
      $("engine-status").textContent = engOk ? "● Engine OK" : "● Engine DOWN — Fail-closed";
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
    // After a build the UI follows the built entity (e.g. cluster-1), not the
    // fixture selector, so downstream checks, highlight and Explain all refer
    // to what was actually derived. Changing the selector clears the override.
    if (fState.activeEntity) return fState.activeEntity;
    const [entityType, entityId] = entitySel.value.split("/");
    return { entityType, entityId };
  }

  // Programmatic entity switch (Scenario Chips, demo buttons): the selector
  // alone does not fire onEntityChange, so clear the built-entity override
  // here — otherwise the next run derives under a stale entity id.
  function switchEntity(value) {
    entitySel.value = value;
    fState.activeEntity = null;
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
    fState.activeEntity = null;
    lastFriendly = null;
    const sim = $("f-simulation");
    if (sim) sim.hidden = true;
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
    // Fixtures always load from the selector itself, never from a stale
    // built-entity override (cleared by onEntityChange above).
    const [entityType, entityId] = entitySel.value.split("/");
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

  function renderFragments() {
    const list = activeFragments();
    proposerBody.innerHTML = `<div class="card"><span class="muted">Loaded fragments — no builder trace yet. Resolve or build to see Scout / Linker / Cluster / Taxonomist / Critic claims.</span></div>`
      + list.map((f) => `
      <div class="card ${f.verified ? "info" : "warn"}">
        <h3><span class="swatch" style="background:${SRC_COLOR[f.source] || "var(--tomb)"}"></span>
        ${esc(f.attribute)} <span class="muted mono">${esc(f.value)}</span>
        <span class="tag">${f._uploaded ? "UPLOADED" : "SYNTHETIC"}</span></h3>
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
      MosaicDrawers.renderSteward(drawerCtx, payload.steward_queue || []);
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
      } else {
        feed(`Manual run (click) — Decision: ${payload.decision_id} — Verdict: ${verdictCap}`);
      }
      setStatus(`Decision ${payload.decision_id} — verdict: ${payload.verdict}.`);
      setFriendlyFromResolve(payload);
      announceRunBubbles(payload);
      markRan();
      renderFriendly();
      await askDownstream(entityType === "product" ? "search" : "otp", { auto: true });
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
      + `${step.output_summary && /abstain/i.test(step.output_summary) ? " <span class='badge'>Abstain</span>" : ""}`
      + ` — ${esc(step.output_summary || "")}</div>`).join("");
    proposerBody.innerHTML = (trace ? `<div class="card plum"><h3>Sub-agent trace</h3>${trace}</div>`
      : `<div class="card"><span class="muted">No builder trace for this run.</span></div>`)
      + attrs.map(([name, attr]) => {
        const conf = (typeof attr.confidence === "number")
          ? `<span class="tag">Confidence claim (Muse, not calibrated): ${esc(String(attr.confidence))}</span> <span class="tag">Claim, not a fact</span>`
          : `<span class="tag">Claim, not a fact · ${esc(attr.confidence || "?")}</span>`;
        return `
      <div class="card info">
        <h3>${esc(name)} ${badge(attr.confidence)}</h3>
        <div>proposed: <span class="mono">${esc(attr.proposed_value ?? "— abstain —")}</span></div>
        <div class="meta">${esc(attr.explanation)}</div>
        <div class="meta">evidence: ${(attr.evidence_fact_ids || []).map(esc).join(", ") || "—"} ${conf}</div>
      </div>`; }).join("")
      + `<div class="card"><span class="muted">Muse does not publish. Only the engine publishes. Muse holds no downstream token.</span></div>`;
  }

  function renderGolden(payload) {
    const golden = payload.golden;
    window.__lastGolden = golden || null;
    if (!golden) {
      $("golden-body").textContent = `No golden published (verdict: ${payload.verdict}).`;
      return;
    }
    const attrs = Object.entries(golden.attributes || {}).map(([name, value]) => {
      if (name === "purchased_products" && Array.isArray(value)) {
        const items = value.map((p, i) =>
          `<div class="gold-field" data-g-field="purchased_products" data-winner=""><div class="fld">PURCHASED[${i}]</div>`
          + `<div class="val">${esc(p.category || "?")}</div>`
          + (p._lineage ? `<div class="prov mono">${esc(JSON.stringify(p._lineage))}</div>` : "") + `</div>`).join("");
        return `<div class="gold-grid">${items}</div>`;
      }
      const line = typeof value === "object" && value !== null
        ? `<div class="val">${esc(value.value)}</div><div class="prov">${esc(value.rule_fired || "")} · ${esc(value.taxonomy_version || "")}</div>`
        : `<div class="val">${esc(value)}</div>`;
      const winner = (value && typeof value === "object" && value.winner_fact_id) || "";
      return `<div class="gold-field" data-g-field="${esc(name)}" data-winner="${esc(winner)}" style="cursor:pointer" title="Show on the graph canvas"><div class="fld">FIELD: ${esc(name).toUpperCase()}</div>${line}<div class="meta">Show on graph →</div></div>`;
    }).join("");
    $("golden-body").innerHTML =
      `<div class="meta">Golden Record — ${esc(golden.entity_id || "")} · Decision: <span class="mono">${esc(payload.decision_id || "—")}</span>`
      + ` · version <span class="mono">${golden.version}</span> · taxonomy <span class="mono">${esc(golden.taxonomy_version || "—")}</span>`
      + ` <button data-golden-history type="button">History</button></div>`
      + `<div class="gold-grid">${attrs}</div>`;
    const gh = $("golden-body").querySelector("[data-golden-history]");
    if (gh) gh.addEventListener("click", () => explain(payload.decision_id));
    $("golden-body").querySelectorAll(".gold-field[data-winner]").forEach((el) => {
      const winner = el.getAttribute("data-winner");
      if (!winner) return;
      el.addEventListener("click", () => highlightOnCanvas(winner));
    });
  }

  // Verifying checklist: every row is computed from the derive response
  // (plus /health versions). A row is ✓ only when the payload proves it,
  // ✗ when the payload contradicts it, ○ when the payload says nothing.
  // Nothing here is asserted without evidence.
  function verifyingChecklist(payload) {
    const KNOWN_RULES = ["R1", "R2", "R3", "R4", "L1", "C1", "C2", "T1", "STRUCT", "SPLIT", "I1"];
    const SOLE_PROOFS = ["exact_email", "exact_phone", "exact_order"];
    const golden = payload.golden || {};
    const attrs = Object.entries(golden.attributes || {});
    const healthPolicy = fState.policyUser && fState.policyProduct
      ? [fState.policyUser, fState.policyProduct] : [];
    const checks = [];
    const push = (ok, label, detail) => checks.push({ ok, label, detail });
    push(!!payload.decision_id, "Decision id recorded", payload.decision_id ? String(payload.decision_id).slice(0, 8) + "…" : "missing");
    push(!!payload.policy_version, "Policy version pinned",
      payload.policy_version || "missing");
    if (healthPolicy.length && payload.policy_version) {
      push(healthPolicy.includes(payload.policy_version),
        "Policy matches /health in-force versions",
        healthPolicy.includes(payload.policy_version) ? "match" : `response ${payload.policy_version} vs health ${healthPolicy.join(" / ")}`);
    } else {
      push(null, "Policy matches /health in-force versions", "health unknown — cannot confirm");
    }
    push(!!payload.taxonomy_version, "Taxonomy version pinned", payload.taxonomy_version || "missing");
    const winners = attrs.filter(([, v]) => v && typeof v === "object" && v.winner_fact_id);
    push(attrs.length === 0 || winners.length === attrs.length,
      "Every golden field names its winner fact",
      attrs.length ? `${winners.length}/${attrs.length} named` : "no golden fields");
    const badRules = attrs.filter(([, v]) => v && typeof v === "object" && v.rule_fired && !KNOWN_RULES.includes(v.rule_fired));
    push(badRules.length === 0, "Every rule fired is a known rule",
      badRules.length ? `unknown: ${badRules.map(([n]) => n).join(", ")}` : "R1/R2/R3/R4 + L1/C1/C2/T1 only");
    const noLineage = attrs.filter(([, v]) => v && typeof v === "object" && !v.lineage);
    push(attrs.length === 0 || noLineage.length === 0, "Every golden field carries lineage",
      attrs.length ? `${attrs.length - noLineage.length}/${attrs.length} with lineage` : "no golden fields");
    const vlinks = payload.verified_links || [];
    const weakVlinks = vlinks.filter((l) => !SOLE_PROOFS.includes(l.proof));
    push(weakVlinks.length === 0, "Verified links are sole-proof hops only (L1)",
      vlinks.length ? (weakVlinks.length ? `non-sole: ${weakVlinks.map((l) => l.proof).join(", ")}` : `${vlinks.length} sole-proof hop(s)`) : "no verified links this run");
    const badConflicts = (payload.conflicts || []).filter((c) => !c.reason_code);
    push(badConflicts.length === 0, "Every conflict names its reason code",
      (payload.conflicts || []).length ? `${(payload.conflicts || []).length - badConflicts.length}/${(payload.conflicts || []).length} named` : "no conflicts");
    const structFails = (payload.conflicts || []).filter((c) => c.rule_fired === "STRUCT" || c.reason_code === "STRUCTURE_MISMATCH");
    push(!(payload.golden && structFails.length > 0), "Whole-document structure gate held",
      payload.golden ? (structFails.length ? "WITHHELD — field failures escalated instead" : "assembled document validated") : `withheld (${payload.verdict}) — nothing partial published`);
    return `<div class="card"><h3>Verifying checklist <span class="muted">computed from this response, not asserted</span></h3>`
      + `<ul class="checklist">` + checks.map((c) =>
        `<li><span class="${c.ok === true ? "ok" : c.ok === false ? "bad" : "na"}">${c.ok === true ? "✓" : c.ok === false ? "✗" : "○"}</span> ${esc(c.label)} <span class="muted mono">— ${esc(c.detail)}</span></li>`).join("")
      + `</ul></div>`;
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

    const conflictHtml = (payload.conflicts || []).map((c) => {
      const owner = (payload.steward_queue || []).find((s) => s.attribute === c.attribute) || {};
      const odf = owner.steward_id
        ? `<div class="meta mono">owner ${esc(owner.owner || "steward-001")} · deadline ${esc(owner.deadline || "—")} · fallback ${esc(owner.fallback || "—")}</div>`
        : "";
      return `
      <div class="card warn">
        <h3>${esc(c.attribute)} ${badge("escalated")}</h3>
        <div class="meta mono">${esc(c.reason_code)} · rule ${esc(c.rule_fired)} · policy ${esc(c.policy_version)} · taxonomy ${esc(c.taxonomy_version || "—")}</div>
        <div class="meta mono">facts: ${(c.fact_ids || []).map(esc).join(", ")}</div>${odf}
      </div>`;
    }).join("");

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
      </div>` + (goldenHtml || "") + (conflictHtml || "") + vlinkHtml + verifyingChecklist(payload);
    engineBody.querySelectorAll("[data-explain]").forEach((btn) => {
      btn.addEventListener("click", () => explain(btn.getAttribute("data-explain")));
    });
    engineBody.insertAdjacentHTML("beforeend",
      `<div class="card"><span class="muted">Same facts + same policy version always derive the same golden record. A rule change is a new policy version, and old golden records are re-derived, not hand-edited. Human only on escalate: a steward decides only when verifying escalates, never on every merge.</span></div>`);
    renderGolden(payload);
  }

  let lastStewardItems = [];

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
    const showCandidates = !$("graph-candidates") || $("graph-candidates").checked;
    let html = `<text x="8" y="14" font-size="11" fill="#64748B">Data Graph</text>`;
    edges.forEach((e) => {
      const a = pos[e.from_node_id];
      const b = pos[e.to_node_id];
      if (!a || !b) return;
      if (!showCandidates && (e.edge_type === "proposed_link" || e.edge_type === "link_miss")) return;
      const hl = highlight && (highlight.includes(e.from_node_id) || highlight.includes(e.to_node_id));
      html += `<line data-edge="${esc(e.edge_id || "")}" data-etype="${esc(e.edge_type || "")}"${e.edge_type === "proposed_link" || e.edge_type === "verified_link" ? ' data-walk="1"' : ""} x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${edgeColor[e.edge_type] || "#475569"}" stroke-width="${hl ? 3 : 1.2}" stroke-dasharray="${edgeDashed[e.edge_type] || "none"}" opacity="${hl ? 1 : 0.7}"><title>${esc(e.edge_type)}${e.proof ? ": " + esc(e.proof) : ""} (${esc(e.proposer || "?")})</title></line>`;
      if ((e.edge_type === "proposed_link" || e.edge_type === "verified_link") && e.proof) {
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        html += `<text x="${mx}" y="${my - 4}" font-size="9" fill="#475569" text-anchor="middle" stroke="#ffffff" stroke-width="2.5" paint-order="stroke">${esc(e.proof)}</text>`;
      }
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
    $("graph-info").textContent = `${nodes.length} nodes, ${edges.length} edges. ■ Source colour = which system it came from — Solid = verified — Dashed = proposed / loser — Amber dashed = escalated, not published — Grey = denied.`;
    svg.querySelectorAll("[data-node]").forEach((g) => {
      g.addEventListener("click", () => {
        const id = g.getAttribute("data-node");
        const node = nodes.find((n) => n.node_id === id);
        $("graph-detail").textContent = JSON.stringify(node, null, 1);
        highlightGoldenForNode(id);
      });
    });
    return { nodes, edges };
  }

  function valueNodeIdFor(entityType, entityId, factId) {
    return `nd-value-${entityType}-${entityId}-${factId}`;
  }

  function findValueNode(winnerFactId) {
    const nodes = lastGraph.nodes || [];
    const scope = lastGraphEntity || currentEntity();
    const exact = valueNodeIdFor(scope.entityType, scope.entityId, winnerFactId);
    const hit = nodes.find((n) => n.node_id === exact);
    if (hit) return hit.node_id;
    // The golden may belong to a built cluster while the selector still
    // shows the fixture entity: fall back to suffix match. Node ids always
    // end with "-" + fact_id, so this stays exact.
    const suffixed = nodes.find((n) => n.node_id.endsWith(`-${winnerFactId}`));
    return suffixed ? suffixed.node_id : null;
  }

  function highlightOnCanvas(winnerFactId) {
    if (!winnerFactId) return;
    const valueNode = findValueNode(winnerFactId);
    if (!valueNode) {
      setStatus(`No canvas node for ${winnerFactId} — load the graph for its entity first.`);
      return;
    }
    const targets = [valueNode];
    (lastGraph.edges || []).forEach((e) => {
      if (e.edge_type === "has_attribute" && e.to_node_id === valueNode) targets.push(e.from_node_id);
    });
    renderGraph({ nodes: lastGraph.nodes || [], edges: lastGraph.edges || [] }, targets);
    const canvas = $("graph-canvas");
    if (canvas && canvas.scrollIntoView) canvas.scrollIntoView({ behavior: "smooth", block: "nearest" });
    feed(`Canvas highlight: ${winnerFactId} — source node and its proof edges.`);
  }

  function highlightGoldenForNode(nodeId) {
    const golden = (lastFriendly && lastFriendly.golden) || window.__lastGolden || null;
    if (!golden || !nodeId || !nodeId.startsWith("nd-value-")) return;
    // Node ids end with "-" + fact_id (fact ids may themselves contain
    // dashes, so match by suffix, not by splitting segments).
    const hit = Object.entries(golden.attributes || {}).find(([, v]) =>
      v && typeof v === "object" && v.winner_fact_id && nodeId.endsWith(`-${v.winner_fact_id}`));
    if (!hit) return;
    document.querySelectorAll(".gold-field.f-hit").forEach((el) => el.classList.remove("f-hit"));
    const row = document.querySelector(`.gold-field[data-g-field="${CSS.escape(hit[0])}"]`);
    if (row) {
      row.classList.add("f-hit");
      row.scrollIntoView({ behavior: "smooth", block: "nearest" });
      feed(`Golden highlight: ${hit[0]} — won by ${hit[1].winner_fact_id}, lineage below.`);
    }
  }

  let lastGraph = { nodes: [], edges: [] };
  let lastGraphEntity = null;

  async function loadGraph(entityType, entityId) {
    const current = currentEntity();
    const type = entityType || current.entityType;
    const id = entityId || current.entityId;
    try {
      const data = await api(`/graph/${type}/${id}`);
      lastGraph = { nodes: data.nodes || [], edges: data.edges || [] };
      lastGraphEntity = { entityType: type, entityId: id };
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
      <h3>Splink — Matcher
        ${ok ? `<span class="badge">CANDIDATE, NOT A FACT</span>`
        : `<span class="badge escalated">DOWN</span>`}
        <span class="muted mono">${esc(ver || "versions from /health")}</span></h3>
      ${ok
        ? `<div class="meta">${clusters.length} clusters, ${pairs.length} pairs — candidates only, never a golden.</div>${clusterHtml}${pairHtml}`
        : `<div class="meta">Splink unavailable — fallback to Muse + fuzzy. No publish on fallback alone.</div>`}
      ${info.detail ? `<div class="meta">${esc(info.detail)}</div>` : ""}`;
    proposerBody.prepend(card);
  }

  async function buildMosaic(auto) {
    fState.working = true;
    renderFStatus();
    const buildBtn = $("build");
    const prevLabel = buildBtn ? buildBtn.textContent : "";
    if (buildBtn) { buildBtn.disabled = true; buildBtn.textContent = "Building…"; }
    proposerBody.innerHTML = `<div class="card"><span class="muted">Muse is proposing…</span></div>`;
    engineBody.innerHTML = `<div class="card"><span class="muted">Waiting for proposal…</span></div>`;
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
      await afterBuild(payload, auto);
    } catch (error) {
      setStatus(`Build failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto build failed (fail-closed): ${error.message} — nothing published`);
      fState.working = false;
      renderFStatus();
      if (buildBtn) { buildBtn.disabled = false; buildBtn.textContent = prevLabel; }
      return;
    }
    fState.working = false;
    renderFStatus();
    if (buildBtn) { buildBtn.disabled = false; buildBtn.textContent = prevLabel; }
  }

  // Shared build-result rendering for the manual and Scene 0 builds:
  // graph + hop replay, golden/engine/mosaic panels, steward queue.
  async function afterBuild(payload, auto) {
      setStatus(`Build ${payload.decision_id} — verdict: ${payload.verdict} (${payload.splink_status}).`);
      setFriendlyFromBuild(payload);
      announceRunBubbles(payload, (payload.proposals || [])[0]);
      markRan();
      renderFriendly();
      // Show the graph of what was just built (first golden's entity), so the
      // canvas, golden panel and highlight actions all refer to the same run.
      // loadGraphForBuild also pins fState.activeEntity, so the downstream
      // check below queries the built entity, not a stale fixture id.
      await loadGraphForBuild(payload);
      animateChain();
      const firstGolden = (payload.goldens || []).find((g) => g.golden_id) || null;
      const first = firstGolden;
      if (first) {
        renderGolden(first);
        renderEngine({ ...payload, golden: first, policy_version: payload.policy_version_user });
        renderMosaic({ ...payload, golden: first, proposal: { attributes: {} } });
      }
      MosaicDrawers.renderSteward(drawerCtx, payload.steward_queue || []);
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
      // Invariant C: Step 3 completes on every build, manual or auto.
      const agent = ((first && first.entity_type) || currentEntity().entityType) === "product" ? "search" : "otp";
      await askDownstream(agent, { auto: true });
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
    fragments = list.map((f) => Object.assign({}, f, { _uploaded: true }));
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
      MosaicDrawers.renderSteward(drawerCtx, payload.steward_queue || []);
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
      } else {
        feed(`Manual upload resolve (click) — Decision: ${payload.decision_id} — Verdict: ${verdictCap}`);
      }
      setStatus(`Upload resolved — decision ${payload.decision_id}, verdict ${payload.verdict}.`);
      setFriendlyFromResolve(payload);
      announceRunBubbles(payload);
      markRan();
      renderFriendly();
      await askDownstream((window.__uploadedEntityType || "user") === "product" ? "search" : "otp", { auto: true });
    } catch (error) {
      setStatus(`Upload resolve failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto run failed (fail-closed): ${error.message} — nothing published`);
    } finally {
      fState.working = false;
      renderFStatus();
    }
  }

  // ---- Protocol chat + 4-Act Scenario Bar (v2.3) --------------------------
  // Every bubble is rendered from a real API response or ledger row passed
  // in by the caller. Nothing here invents proposers, derivations, or
  // steward items. Steward buttons reuse the drawer handlers verbatim.
  function chatDownstream(lines, opts) {
    return window.MosaicChat.add(Object.assign({ role: "downstream" }, lines, opts));
  }

  function chatProposer(lines, opts) {
    return window.MosaicChat.add(Object.assign({ role: "proposer" }, lines, opts));
  }

  function chatEngine(lines, opts) {
    return window.MosaicChat.add(Object.assign({ role: "engine" }, lines, opts));
  }

  function wireChatBar() {
    const bar = $("f-chat-input-bar");
    if (!bar || bar.dataset.wired) return;
    bar.dataset.wired = "1";
    bar.querySelectorAll("[data-q]").forEach((btn) => {
      btn.addEventListener("click", () => askAbout(btn.getAttribute("data-q")));
    });
    const input = $("f-chat-input");
    if (input) {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && input.value.trim()) {
          askAbout(input.value.trim());
          input.value = "";
        }
      });
    }
    document.querySelectorAll("#f-scenario-bar [data-act]").forEach((btn) => {
      btn.addEventListener("click", () => runScenario(btn.getAttribute("data-act")));
    });
    document.getElementById("f-chat-stream").addEventListener("click", async (event) => {
      const btn = event.target.closest("[data-chat-act]");
      if (!btn) return;
      const act = btn.getAttribute("data-chat-act");
      if (act === "explain") {
        const host = btn.closest("[data-decision]");
        explain(btn.getAttribute("data-chat-arg"), btn.getAttribute("data-chat-arg2") || undefined);
        void host;
      } else if (act === "detail") {
        showDetail(true);
      } else if (act === "datalog") {
        toggleChatDatalog(btn);
      } else if (act === "similar") {
        MosaicDrawers.openSimilar(drawerCtx);
      }
      // NOTE: "restore-engine" is wired directly in act5 (it closes over the
      // built docs + idempotency key), so the delegated handler ignores it.
    });
  }

  // ---- Protocol chat + 4-Act Scenario Bar (v2.3) -----------------------
  // Every bubble below is rendered from a real API response or ledger row
  // passed in by the caller. The chat never invents proposers, derivations,
  // or steward items. Steward buttons reuse the drawer handlers verbatim.
  function chatProposerLines(proposal, splink) {
    if (!proposal) return ["<i>No proposal recorded for this run.</i>"];
    const lines = [];
    const byAgent = {};
    (proposal.subagent_trace || proposal.trace || []).forEach((s) => { byAgent[s.agent || "?"] = s; });
    if (byAgent.Scout) lines.push(`<b>Scout:</b> ${esc(byAgent.Scout.output_summary || "cleaned the fragments")}`);
    if (byAgent.Linker) lines.push(`<b>Linker:</b> ${esc(byAgent.Linker.output_summary || "proposed links")}`);
    if (byAgent.Cluster) lines.push(`<b>Cluster:</b> ${esc(byAgent.Cluster.output_summary || "proposed clusters")}`);
    if (byAgent.Taxonomist) lines.push(`<b>Taxonomist:</b> ${esc(byAgent.Taxonomist.output_summary || "proposed mappings")}`);
    Object.entries(proposal.attributes || {}).forEach(([name, attr]) => {
      const rawConf = attr && attr.confidence;
      const confLabel = (typeof rawConf === "number")
        ? ` <span class="tag">Claim, not a fact · Confidence claim (Muse, not calibrated): ${esc(String(rawConf))}</span>`
        : (rawConf
          ? ` <span class="tag">Claim, not a fact · ${esc(rawConf)}</span>`
          : ` <span class="tag">Claim, not a fact</span>`);
      lines.push(`<b>${esc(name)}:</b> proposes <span class="mono">${esc(attr && attr.proposed_value != null ? attr.proposed_value : "— abstain —")}</span>${confLabel}`);
    });
    (proposal.links || []).slice(0, 6).forEach((link) => {
      lines.push(`Linker proposes: <span class="mono">${esc(link.from_fact_id)} —${esc(link.proof || "?")}→ ${esc(link.to_fact_id)}</span> <span class="tag">Claim, not a fact</span> <button type="button" data-chat-act="similar">Similar past cases — click to view</button>`);
    });
    (proposal.new_terms || []).forEach((term) => {
      lines.push(`<b>New term proposed:</b> ${esc(term.raw_value || term)} — PROPOSED, not created.`);
    });
    if (byAgent.Critic) {
      lines.push(`<b>Critic:</b> ${esc(byAgent.Critic.output_summary || "reviewed the merge")}`);
    } else if (!(proposal.critiques || []).length) {
      lines.push(`<b>Critic:</b> Critic found no critical risk in this cluster.`);
    }
    (proposal.critiques || []).forEach((c) => {
      lines.push(`<b>Critic flag:</b> ${esc(typeof c === "string" ? c : (c.kind || c.explain || JSON.stringify(c)))}`);
    });
    if (splink && splink.status === "ok") {
      const pairs = splink.pairs || [];
      lines.push(`<b>Splink:</b> ${pairs.length} candidate pair(s), weights are signals only — <span class="tag">CANDIDATE, NOT A FACT</span>`);
    } else if (splink) {
      lines.push(`<b>Splink:</b> unavailable — fallback to Muse + fuzzy. <span class="tag">No publish on fallback alone.</span>`);
    }
    return lines.length ? lines : ["<i>Empty proposal — nothing claimed.</i>"];
  }

  function chatEngineLines(payload, golden) {
    const lines = [];
    lines.push(`Verdict: <b>${esc(payload.verdict || "unknown")}</b> — Decision <span class="mono">${esc(payload.decision_id || "—")}</span>`);
    lines.push(`Policy: <span class="mono">${esc(payload.policy_version || payload.policy_version_user || "—")}</span> · Taxonomy: <span class="mono">${esc(payload.taxonomy_version || "—")}</span>`);
    if (golden && golden.attributes) {
      Object.entries(golden.attributes).forEach(([name, v]) => {
        if (name === "purchased_products" || !v || typeof v !== "object") return;
        lines.push(`${esc(name)}: <b>${esc(v.value)}</b> wins — ${esc(v.rule_fired || "?")} · loser kept as Tombstone`);
      });
    }
    (payload.conflicts || []).slice(0, 3).forEach((c) => {
      lines.push(`Escalated: ${esc(c.attribute)} — ${esc(c.rule_fired || c.reason_code || "escalated")}. ${esc(c.fallback || "Steward decides.")}`);
    });
    return lines;
  }

  function announceProposer(proposal, splink, decisionId) {
    window.MosaicChat.add({
      role: "proposer",
      title: "Muse Spark + Matching Engine",
      lines: chatProposerLines(proposal, splink),
      policyLine: decisionId ? `Decision: ${decisionId} — claims only, nothing published yet.` : undefined,
    });
  }

  function announceEngine(payload, golden, opts) {
    opts = opts || {};
    const actions = [];
    const firstAttr = golden && golden.attributes
      ? Object.keys(golden.attributes).find((k) => k !== "purchased_products") : null;
    if (firstAttr && payload.decision_id) {
      actions.push({ label: `Why this value? · Proof ${payload.decision_id}`, act: "explain", arg: payload.decision_id, arg2: firstAttr });
    }
    actions.push({ label: "Show Datalog", act: "datalog" });
    actions.push({ label: "Open in Detail View", act: "detail" });
    window.MosaicChat.add({
      role: "engine",
      title: opts.title || "Trust Engine (manglekit)",
      tone: payload.verdict === "escalated" ? "escalated" : (payload.verdict === "denied" ? "failed" : undefined),
      lines: chatEngineLines(payload, golden),
      policyLine: payload.policy_version ? `Policy: ${payload.policy_version}` : undefined,
      actions,
    });
  }

  function announceStewards(stewardQueue) {
    (stewardQueue || []).forEach((item) => {
      const node = window.MosaicChat.add({
        role: "steward",
        title: "Human Steward (steward-001)",
      });
      if (!node) return;
      const wrap = document.createElement("div");
      wrap.innerHTML = MosaicDrawers.stewardCardHtml(drawerCtx, item, true);
      node.appendChild(wrap.firstElementChild);
      MosaicDrawers.bindStewardActions(drawerCtx, node);
      node.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  function announceDownstream(payload, agent) {
    if (!payload) return;
    const entityType = payload.entity_type || currentEntity().entityType;
    const entityId = payload.entity_id || currentEntity().entityId;
    if (payload.answer === "golden") {
      const label = agent === "search" ? `Search uses ${payload.value}` : `OTP uses ${payload.value}`;
      window.MosaicChat.add({
        role: "downstream",
        title: "Downstream Agent",
        lines: [`Downstream agent called GET /golden/${entityType}/${entityId} — Decision: ${payload.decision_id || "—"} — then acted (${label}).`],
      });
    } else {
      window.MosaicChat.add({
        role: "downstream",
        title: "Downstream Agent",
        lines: [`Downstream agent called GET /golden/${entityType}/${entityId} — No trusted value — downstream stopped.`],
      });
    }
  }

  function setScenario(id) {
    window.MosaicChat.setScenarioActive(id);
  }

  // Transcript for every run (resolve, upload-resolve, build): proposer
  // claims, engine verdict, steward exceptions — all from this payload.
  function announceRunBubbles(payload, proposal) {
    if (!payload || typeof payload.decision_id !== "string") return;
    announceProposer(proposal !== undefined ? proposal : payload.proposal, payload.splink, payload.decision_id);
    announceEngine(payload, (lastFriendly && lastFriendly.golden) || payload.golden || null);
    announceStewards(payload.steward_queue || []);
  }

  async function actScene0() {
    setScenario("scene0");
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["I have a pile of messy JSON. No schema. No config. Can anything here be trusted?"],
    });
    loadScene0();
  }

  function fragmentsFromDocs(docs) {
    // Convert built documents back to fragment rows for the friendly view
    // (Invariant A): same fields, same source ids, trap flags preserved.
    const out = [];
    (docs || []).forEach((doc) => {
      const fields = doc.fields || {};
      Object.entries(fields).forEach(([rawName, field]) => {
        const value = field && typeof field === "object" ? field.value : field;
        if (value === undefined || value === null || value === "") return;
        out.push({
          attribute: rawName,
          value: String(value),
          source: doc.source || "unknown",
          source_id: `${doc.document_id}-${rawName}`,
          entity_type: doc.entity_type,
          authority_rank: (field && field.authority_rank) || 3,
          verified: !!(field && field.verified),
          event_time: (field && field.event_time) || "2026-10-27T09:00:00+07:00",
          ttl_seconds: (field && field.ttl_seconds) || 3600,
          _docId: doc.document_id,
          _trap: !!doc.trap,
        });
      });
    });
    return out;
  }

  function docLabelFor(entity) {
    if (!entity) return "";
    return entity.entity_id || entity.entityId || "";
  }

  async function act1() {
    setScenario("act1");
    switchEntity("user/P-001");
    ticketBox.checked = false;
    await loadFragments();
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["Requesting verified phone for User P-001 to send login OTP."],
    });
    await resolve(true);
  }

  async function act2() {
    setScenario("act2");
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["Are these five fragments one person, or five different people?"],
    });
    let docs;
    try {
      const data = await api("/datasets/hidden");
      docs = data.documents || [];
    } catch (error) {
      setStatus(`Hidden dataset unavailable: ${error.message}`);
      return;
    }
    fragments = fragmentsFromDocs(docs);
    renderFriendly();
    const trapDocs = docs.filter((d) => d.trap).map((d) => d.document_id);
    fState.trapDocs = trapDocs;
    const key = "act2-hidden-v1";
    const payload = await api("/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ work_id: `ui-${key}`, idempotency_key: key, dataset: { documents: docs } }),
    });
    setFriendlyFromBuild(payload);
    markRan();
    announceProposer((payload.proposals || [])[0], payload.splink);
    announceEngine(payload, (payload.goldens || []).find((g) => g.golden_id) || null);
    renderFriendly();
    // The hop replay is the money shot: candidates must be visible for it.
    const candBox = $("graph-candidates");
    if (candBox) candBox.checked = true;
    await loadGraphForBuild(payload);
    animateChain();
    renderSimulation(payload, trapDocs);
    markTrapNodes(trapDocs);
    // Trap exclusion is proactive in Act 2, not only on question: F6's
    // separation is the point of the scene.
    if (findTrapEvidence().found) {
      window.MosaicChat.add({
        role: "engine",
        title: "Trap exclusion — F6",
        lines: trapExclusionLines(),
        actions: [{ label: "Open in Detail View", act: "detail" }],
      });
    }
    announceStewards(payload.steward_queue || []);
    await askDownstream("otp", { auto: true });
    setStatus(`Act 2 complete — Decision: ${payload.decision_id} — Verdict: ${payload.verdict}.`);
  }

  async function act3() {
    setScenario("act3");
    switchEntity("user/P-001");
    ticketBox.checked = true;
    await loadFragments();
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["A support ticket proposes a new phone for P-001. Critical field — I will not act unless the engine proves it."],
    });
    await resolve(true);
  }

  async function act4() {
    setScenario("act4");
    switchEntity("product/prod-001");
    ticketBox.checked = false;
    await loadFragments();
    // The T1 beat needs a genuinely unknown term: inject an unverified
    // marketplace fragment carrying "Gaming Chair" (not a taxonomy term),
    // exactly as the demo script specifies. No fixture is modified.
    fragments.push({
      attribute: "category",
      value: "Gaming Chair",
      source: "marketplace-like",
      source_id: "mp-gc-1",
      authority_rank: 3,
      verified: false,
      event_time: "2026-10-27T11:00:00+07:00",
      ttl_seconds: 3600,
    });
    renderFriendly();
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["Indexing product prod-001 for search. Which category is proven — and what is this 'Gaming Chair'?"],
    });
    await resolve(true);
    const queue = (lastFriendly && lastFriendly.steward_queue) || [];
    const t1 = queue.find((s) => (s.suggested_action || "").includes("Gaming Chair"));
    if (t1) {
      window.MosaicChat.add({
        role: "engine",
        title: "Trust Engine (manglekit)",
        tone: "escalated",
        lines: [
          `T1 taxonomy gate: "Gaming Chair" matches no term or synonym under <span class="mono">${esc((lastFriendly && lastFriendly.taxonomy_version) || "")}</span>. Nothing auto-created.`,
          `A steward decision (Accept suggested) creates the new taxonomy version and re-derives — it never publishes on the spot.`,
        ],
        actions: [{ label: "Open in Detail View", act: "detail" }],
      });
    }
  }

  async function act5() {
    setScenario("act5");
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: ["Stopping the trust engine to prove fail-closed. No new golden may publish while it is down."],
    });
    await api("/demo/engine/stop", { method: "POST" });
    await refreshHealth();
    window.MosaicChat.add({
      role: "engine",
      title: "Trust Engine (manglekit)",
      tone: "failed",
      lines: ["Trust-engine is stopped. No new golden record can be published."],
    });
    const key = "act5-hidden-v1";
    let docs;
    try {
      const data = await api("/datasets/hidden");
      docs = data.documents || [];
    } catch (error) {
      setStatus(`Hidden dataset unavailable: ${error.message}`);
      return;
    }
    fragments = fragmentsFromDocs(docs);
    renderFriendly();
    try {
      await api("/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ work_id: `ui-${key}`, idempotency_key: key, dataset: { documents: docs } }),
      });
      feed("Act 5: build unexpectedly succeeded while stopped — investigating.");
    } catch (error) {
      window.MosaicChat.add({
        role: "proposer",
        title: "Muse Spark + Matching Engine",
        lines: ["Proposal ready, derivation unavailable.", `Build refused: ${esc(error.message)}`],
      });
      window.MosaicChat.add({
        role: "downstream",
        title: "Downstream Agent",
        lines: ["Downstream stopped — Trust-engine is down."],
      });
      announceStewards([]);
      feed("Act 5: build failed closed while engine stopped — escalated, nothing published.");
    }
    const node = window.MosaicChat.add({
      role: "steward",
      title: "Human Steward (steward-001)",
      lines: ["Restore the engine and retry with the same idempotency key."],
      actions: [{ label: "Restore Trust Engine & Retry Same Key", act: "restore-engine", primary: true }],
    });
    if (node) {
      node.querySelector("[data-chat-act]").addEventListener("click", async () => {
        await api("/demo/engine/start", { method: "POST" });
        await refreshHealth();
        const first = await api("/build", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ work_id: `ui-${key}`, idempotency_key: key, dataset: { documents: docs } }),
        });
        const again = await api("/build", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ work_id: `ui-${key}-again`, idempotency_key: key, dataset: { documents: docs } }),
        });
        const same = first.decision_id && first.decision_id === again.decision_id;
        window.MosaicChat.add({
          role: "engine",
          title: "Trust Engine (manglekit)",
          lines: same
            ? [`Same decision id returned (${esc(first.decision_id)}). No double derivation.`]
            : [`Restored. Decision: ${esc(first.decision_id || "?")}, retry: ${esc(again.decision_id || "?")}.`],
        });
        setStatus(same ? "Same decision id returned. No double derivation." : "Engine restored; build completed.");
        setFriendlyFromBuild(first);
        markRan();
        renderFriendly();
        await loadGraphForBuild(first);
        announceStewards(first.steward_queue || []);
        await askDownstream("otp", { auto: true });
      });
    }
  }

  function runScenario(id) {
    if (id === "scene0") return actScene0();
    if (id === "act1") return act1();
    if (id === "act2") return act2();
    if (id === "act3") return act3();
    if (id === "act4") return act4();
    if (id === "act5") return act5();
    return null;
  }

  function renderSimulation(payload, trapDocs) {
    let host = $("f-simulation");
    if (!host) {
      const anchor = $("f-answer");
      host = document.createElement("div");
      host.id = "f-simulation";
      host.className = "f-simulation";
      anchor.parentNode.insertBefore(host, anchor.nextSibling);
    }
    // Inline hop-by-hop chain strip: sole-proof hops (exact email/phone/order)
    // across different documents, in link order, with the trap never linked.
    const hopSeen = new Set();
    const hops = [];
    const docOf = (fid) => {
      const m = /-(F\d+|SC0-\d+|DOC-\d+)-[^-\s]+$/.exec(String(fid || ""));
      return m ? m[1] : null;
    };
    ((payload && payload.proposals) || []).forEach((proposal) => {
      if (!proposal || proposal.replayed) return;
      (proposal.links || []).forEach((link) => {
        if (!String(link.proof || "").startsWith("exact_")) return;
        const from = docOf(link.from_fact_id);
        const to = docOf(link.to_fact_id);
        if (!from || !to || from === to) return;
        const key = `${from}→${to}:${link.proof}`;
        if (hopSeen.has(key)) return;
        hopSeen.add(key);
        const kind = String(link.proof).replace(/^exact_/, "");
        hops.push(`${from} → ${to} (${kind})`);
      });
    });
    const chainStrip = hops.length
      ? `<p><b>Hop chain:</b> ${esc(hops.join(" · "))} — One person, proved hop by hop. F1 and F5 share nothing directly.</p>`
      : "";
    const proposals = (payload && payload.proposals) || [];
    const goldens = ((payload && payload.goldens) || []).filter((g) => g.golden_id);
    const byCluster = {};
    goldens.forEach((g) => { byCluster[g.entity_id] = g; });
    const rows = [];
    const trapSet = new Set(trapDocs || []);
    const trapLinkNotes = [];
    proposals.forEach((proposal) => {
      if (!proposal || proposal.replayed) return;
      const golden = byCluster[proposal.cluster_id] || byCluster[proposal.entity_id];
      const proposed = (proposal.attributes && proposal.attributes) || {};
      const won = (golden && golden.attributes) || {};
      Object.entries(proposed).forEach(([attr, claim]) => {
        const pv = claim && claim.proposed_value;
        const wv = won[attr] && won[attr].value;
        if (pv != null && wv != null && String(pv) !== String(wv)) {
          rows.push(`Muse proposed <b>${esc(String(pv))}</b> for ${esc(attr)} (newest); engine derived <b>${esc(String(wv))}</b> (${esc((won[attr] || {}).rule_fired || "?")}).`);
        }
      });
      (proposal.links || []).forEach((link) => {
        const touchesTrap = [link.from_fact_id, link.to_fact_id].some(
          (fid) => fid && [...trapSet].some((doc) => fid.includes(`-${doc}-`) || fid.endsWith(`-${doc}`) || fid === doc)
        );
        if (touchesTrap && !["exact_email", "exact_phone", "exact_order"].includes(link.proof)) {
          const key = `${link.from_fact_id}→${link.to_fact_id}:${link.proof}`;
          if (!trapLinkNotes.includes(key)) trapLinkNotes.push(key);
          rows.push(`Non-sole link <span class="mono">${esc(link.from_fact_id)} → ${esc(link.to_fact_id)} (${esc(link.proof)})</span> touches the trap cluster — without L1 it would merge the neighbour.`);
        }
      });
    });
    const body = rows.length ? `<ul>${rows.map((r) => `<li>${r}</li>`).join("")}</ul>`
      : `<p>No proposed-only link reaches the trap. The engine verified every published hop with exact identifiers.</p>`;
    window.__lastSimulationRows = rows;
    host.hidden = false;
    host.innerHTML = `<h3>SIMULATION — Muse only, no trust-engine</h3>`
      + `<p><b>Wrong golden — would publish Binh as An.</b> Same proposer, same data. Without the engine, the neighbour gets merged. With the engine, he does not.</p>`
      + chainStrip
      + body
      + `<p class="meta">This panel is a what-if rendering of proposer claims already on the ledger — it never writes a golden. Downstream: BLOCKED in real run.</p>`;
    host.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return rows;
  }

  function markTrapNodes(trapDocIds) {
    if (!trapDocIds || !trapDocIds.length) return;
    document.querySelectorAll("#graph-canvas g[data-node]").forEach((g) => {
      const id = g.getAttribute("data-node") || "";
      if (trapDocIds.some((doc) => id.includes(`-${doc}-`) || id.endsWith(`-${doc}`))) {
        g.classList.add("trap-node");
        if (!g.querySelector("[data-trap-label]")) {
          const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
          text.setAttribute("data-trap-label", "1");
          text.setAttribute("x", "18");
          text.setAttribute("y", "-12");
          text.setAttribute("font-size", "10");
          text.setAttribute("fill", "#64748B");
          text.textContent = "✕ Excluded Trap — Not linked";
          g.appendChild(text);
        }
      }
    });
    const info = $("graph-info");
    if (info) info.textContent += " Grey dashed = trap neighbour (address match only, never merged).";
  }

  function findTrapEvidence() {
    // Strongest real evidence first: the trap doc clustered alone (own
    // cluster, own golden) — exclusion by separation, never merged.
    const proposals = (window.__lastProposals || []);
    const goldens = (window.__lastGoldens || (window.__lastGolden ? [window.__lastGolden] : []));
    const trapDocs = fState.trapDocs || [];
    if (trapDocs.length) {
      const alone = proposals.filter((p) => {
        if (!p || p.replayed) return false;
        const touched = new Set();
        (p.links || []).forEach((l) => { touched.add(l.from_fact_id); touched.add(l.to_fact_id); });
        if (!touched.size) return false;
        return [...touched].every((fid) => trapDocs.some((doc) => fid && fid.includes(doc)));
      });
      if (alone.length) {
        const p = alone[0];
        return {
          found: true,
          detail: `${trapDocs.join(", ")} clustered alone (${p.cluster_id || p.entity_id || "own cluster"}) with its own golden — address matched, but no exact email, phone, or order proof, so no verified link to F1–F5 was created.`,
        };
      }
      // Replayed runs carry no proposal links; the goldens still prove
      // separation: the trap doc's winner fact ids live in their own golden.
      const trapGoldens = (goldens || []).filter((g) => {
        if (!g || !g.golden_id) return false;
        const hay = JSON.stringify([g.entity_id, g.attributes || {}]);
        return trapDocs.some((doc) => hay.includes(doc));
      });
      const mainGoldens = (goldens || []).filter((g) => g && g.golden_id && !trapGoldens.includes(g));
      if (trapGoldens.length && mainGoldens.length) {
        return {
          found: true,
          detail: `${trapDocs.join(", ")} derived into its own golden (${trapGoldens[0].golden_id}) apart from ${(mainGoldens[0] || {}).golden_id || "the main golden"} — address matched, but no exact email, phone, or order proof, so no verified link to F1–F5 was created.`,
        };
      }
      for (const proposal of proposals) {
        for (const link of (proposal.links || [])) {
          const touches = [link.from_fact_id, link.to_fact_id].some(
            (fid) => fid && trapDocs.some((doc) => fid.includes(`-${doc}-`) || fid.endsWith(`-${doc}`) || fid === doc)
          );
          if (touches && !["exact_email", "exact_phone", "exact_order"].includes(link.proof)) {
            return {
              found: true,
              detail: `${link.from_fact_id} → ${link.to_fact_id} (${link.proof}) — medium proof only, never verified.`,
            };
          }
        }
      }
      for (const proposal of proposals) {
        const hit = (proposal.critiques || []).find((c) => {
          const s = typeof c === "string" ? c : JSON.stringify(c);
          return trapDocs.some((doc) => s.includes(doc));
        });
        if (hit) {
          const s = typeof hit === "string" ? hit : (hit.explanation || hit.explain || JSON.stringify(hit));
          return { found: true, detail: String(s).slice(0, 280) };
        }
      }
    }
    return { found: false, detail: "" };
  }

  function trapExclusionLines() {
    const trap = findTrapEvidence();
    return trap.found
      ? [`F6 shares <b>address only</b> — no exact email, phone, or order proof. Engine rule L1 verifies sole-proof hops only, so no verified link was created.`, `<b>Similar is not same. Mosaic did not merge the neighbour in the same building.</b>`, `Evidence: ${esc(trap.detail)}`]
      : ["Run Act 2 (Hidden Connection) first — I only answer from ledger results, never from memory."];
  }

  async function toggleChatDatalog(btn) {
    const host = btn.closest(".chat-bubble");
    if (!host) return;
    let box = host.querySelector("[data-datalog]");
    if (box) {
      box.hidden = !box.hidden;
      btn.textContent = box.hidden ? "Show Datalog" : "Hide Datalog";
      return;
    }
    box = document.createElement("div");
    box.setAttribute("data-datalog", "1");
    box.className = "meta mono";
    box.textContent = "Loading policy source…";
    host.appendChild(box);
    btn.textContent = "Hide Datalog";
    try {
      const data = await api(`/policies/user/${encodeURIComponent(fState.policyUser || "pol-2026-10-28-v1")}`);
      const src = String((data && data.rules_datalog) || "Policy source unavailable in this run.");
      const pre = document.createElement("pre");
      pre.style.whiteSpace = "pre-wrap";
      pre.textContent = src.split("\n").slice(0, 60).join("\n");
      box.textContent = "";
      box.appendChild(pre);
    } catch (error) {
      box.textContent = `Policy source unavailable: ${error.message}`;
    }
  }

  async function askAbout(query) {
    const q = String(query || "").toLowerCase();
    const f = (lastFriendly && lastFriendly.golden) || window.__lastGolden || null;
    if (/datalog|rule/.test(q)) {
      chatEngine({ title: "Show Datalog", lines: ["<i>Fetching the runnable policy…</i>"] });
      try {
        const data = await api(`/policies/user/${encodeURIComponent(fState.policyUser || "pol-2026-10-28-v1")}`);
        const src = String((data && data.rules_datalog) || "Policy source unavailable in this run.");
        window.MosaicChat.add({
          role: "engine",
          title: "Trust Engine — Datalog",
          lines: [`<details open><summary>Runnable policy source (first 60 lines)</summary><pre style="white-space:pre-wrap">${esc(src.split("\n").slice(0, 60).join("\n"))}</pre></details>`],
        });
      } catch (error) {
        window.MosaicChat.add({ role: "engine", title: "Show Datalog", lines: [`Policy source unavailable: ${esc(error.message)}`] });
      }
      return;
    }
    if (q.includes("f6") || q.includes("trap") || q.includes("excluded") || q.includes("neighbour") || q.includes("neighbor")) {
      const trap = findTrapEvidence();
      chatEngine({
        title: "Why was F6 excluded?",
        lines: trapExclusionLines(),
        actions: trap.found ? [{ label: "Open in Detail View", act: "detail" }] : [],
      });
      return;
    }
    if (q.includes("muse-only") || q.includes("muse only") || q.includes("compare") || q.includes("without the engine")) {
      renderSimulationBubble();
      return;
    }
    const attr = ["phone", "email", "address", "category", "display_name", "order_id"].find((a) => q.includes(a));
    if (attr && f && f.attributes && f.attributes[attr]) {
      const v = f.attributes[attr];
      chatEngine({
        title: `Why did ${attr} win?`,
        lines: [
          `<b>${esc(String(v.value))}</b> won under <b>${esc(v.rule_fired || "?")}</b> — winner <span class="mono">${esc(v.winner_fact_id || "?")}</span>.`,
          `Policy: <span class="mono">${esc(v.policy_version || "")}</span> · Taxonomy: <span class="mono">${esc(v.taxonomy_version || "")}</span>`,
        ],
        actions: f.decision_id ? [{ label: `Why this value? · Proof ${f.decision_id}`, act: "explain", arg: f.decision_id, arg2: attr }] : [],
      });
      return;
    }
    window.MosaicChat.add({
      role: "steward",
      title: "Ask from ledger results",
      lines: ["I only answer from run results: try <b>phone</b>, <b>email</b>, <b>address</b>, <b>F6</b>, <b>datalog</b>, or run a scenario first."],
    });
  }

  function renderSimulationBubble() {
    const rows = window.__lastSimulationRows;
    if (!rows) {
      window.MosaicChat.add({
        role: "engine",
        title: "Compare with Muse-only",
        lines: ["Run Act 2 (Hidden Connection) first — the simulation is computed from that run's proposer claims, never invented."],
      });
      return;
    }
    window.MosaicChat.add({
      role: "engine",
      title: "SIMULATION — Muse only, no trust-engine",
      tone: "failed",
      lines: rows.length ? rows : ["No proposed-only link reaches the trap. The engine verified every published hop with exact identifiers."],
    });
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
      if (auto) announceDownstream(payload, agent);
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

  // Explain drawer lives in drawers.js; this thin wrapper keeps existing
  // in-file call sites working while new code calls MosaicDrawers directly.
  async function explain(decisionId, field) {
    return window.MosaicDrawers.explain(drawerCtx, decisionId, field);
  }

  async function loadJobs() {
    try {
      const data = await api("/jobs?limit=20");
      renderJobs(data.jobs || []);
    } catch (error) {
      $("jobs-info").textContent = `Jobs unavailable: ${error.message}`;
    }
  }

  function renderJobs(jobs) {
    const host = $("jobs-list");
    if (!jobs.length) {
      host.textContent = "No jobs yet.";
      $("jobs-info").textContent = "No jobs yet — enqueue one to run unattended.";
      return;
    }
    host.innerHTML = jobs.map((job) => {
      const verdict = job.result && job.result.verdict ? ` · verdict ${esc(job.result.verdict)}` : "";
      const error = job.error ? ` · ${esc(job.error.slice(0, 120))}` : "";
      const tokens = ` · ${job.tokens_used} tokens / ${job.attempts} attempts`;
      const actions = job.status === "failed"
        ? ` <button data-job-retry="${esc(job.job_id)}">Retry</button>`
        : (job.status === "queued" || job.status === "running"
          ? ` <button data-job-cancel="${esc(job.job_id)}">Cancel</button>`
          : "");
      return `<div><b>${esc(job.job_id.slice(0, 8))}</b> ${esc(job.kind)} — ${esc(job.status)}${verdict}${tokens}${error}${actions}</div>`;
    }).join("");
    const counts = {};
    jobs.forEach((job) => { counts[job.status] = (counts[job.status] || 0) + 1; });
    $("jobs-info").textContent = Object.entries(counts)
      .map(([status, count]) => `${count} ${status}`).join(" · ");
    host.querySelectorAll("[data-job-retry]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/jobs/${btn.getAttribute("data-job-retry")}/retry`, { method: "POST" });
          feed(`Job ${btn.getAttribute("data-job-retry").slice(0, 8)} re-queued by steward — worker resumes from persisted clusters.`);
          await loadJobs();
        } catch (error) {
          setStatus(`Retry failed: ${error.message}`);
        }
      });
    });
    host.querySelectorAll("[data-job-cancel]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/jobs/${btn.getAttribute("data-job-cancel")}`, { method: "DELETE" });
          feed(`Job ${btn.getAttribute("data-job-cancel").slice(0, 8)} cancelled — running clusters finish their gate, nothing new starts.`);
          await loadJobs();
        } catch (error) {
          setStatus(`Cancel failed: ${error.message}`);
        }
      });
    });
  }

  function currentViewDocuments() {
    const { entityType } = currentEntity();
    const docsBySource = {};
    for (const f of activeFragments()) {
      (docsBySource[f.source] = docsBySource[f.source] || { source: f.source, fields: {} });
    }
    return Object.values(docsBySource).map((d, i) => ({
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
  }

  // ---- Scene 0: messy-JSON config proposal ---------------------------------
  // First-run gate only. Loading the Scene 0 template uploads it (manual,
  // never auto-run) and renders Scout/Linker proposals plus the in-force
  // config. Approving pins that config for the next Build — it records the
  // policy version in force, mints nothing, and publishes nothing. Policies
  // are baked versioned files; the UI cannot and must not invent versions.
  const SCENE0_FILE = "scene0-messy.json";
  const SCENE0_FRAGMENTS = [
    { source: "salesforce-like", source_id: "sc0-crm-1", attribute: "full_name", value: "Nguyen Van An", authority_rank: 2, event_time: "2026-10-27T09:00:00+07:00", verified: true, ttl_seconds: 3600 },
    { source: "salesforce-like", source_id: "sc0-crm-2", attribute: "email", value: "an.van@example.com", authority_rank: 2, event_time: "2026-10-27T09:00:00+07:00", verified: true, ttl_seconds: 3600 },
    { source: "marketplace-like", source_id: "sc0-mkt-1", attribute: "reviewer", value: "A. Nguyen", authority_rank: 3, event_time: "2026-10-27T09:12:00+07:00", verified: false, ttl_seconds: 3600 },
    { source: "marketplace-like", source_id: "sc0-mkt-2", attribute: "email", value: "an.van@example.com", authority_rank: 3, event_time: "2026-10-27T09:12:00+07:00", verified: false, ttl_seconds: 3600 },
    { source: "marketplace-like", source_id: "sc0-mkt-3", attribute: "dien_thoai", value: "0901234567", authority_rank: 3, event_time: "2026-10-27T09:12:00+07:00", verified: false, ttl_seconds: 3600 },
  ];

  function scene0ConfigInForce() {
    const policy = [fState.policyUser, fState.policyProduct].filter(Boolean).join(" / ") || "checking…";
    const taxonomy = loop.taxonomy || (lastFriendly && lastFriendly.taxonomy_version) || "checking…";
    return { policy, taxonomy };
  }

  function renderScene0Config(payload) {
    const inForce = scene0ConfigInForce();
    const version = (fState.scene0 && fState.scene0.version) || 1;
    const edited = version > 1 ? ` <span class="tag">edited fields highlighted</span>` : "";
    $("f-config-title").innerHTML = `Proposed Pipeline Config — v${version} (proposed, not yet in force) <span class="tag">PROPOSED</span>${edited}`;
    const entityType = window.__uploadedEntityType || payload.detected_entity_type || "user";
    const rawAttrs = [...new Set((payload.fragments || []).map((f) => f.attribute))];
    const links = payload.link_proposals || [];
    const linkRows = links.length
      ? links.map((l) => `<div class="cfg-row">Linker proposes: <span class="mono">${esc(l.from_fact_id)} —${esc(l.proof)}→ ${esc(l.to_fact_id)}</span> <span class="tag">${esc(l.proposer || "Muse")}</span></div>`).join("")
      : `<div class="cfg-row">Linker proposes: no exact-identifier links — nothing claimed.</div>`;
    $("f-config-body").innerHTML =
      `<div class="cfg-row">Here is how we would clean this mess.</div>`
      + `<div class="cfg-row">We found 2 kinds of things: people and products.</div>`
      + `<div class="cfg-row">We will match people by exact phone, email, or order number.</div>`
      + `<div class="cfg-row">We recognise these product names: Smartphone, and its other spellings.</div>`
      + `<div class="cfg-row"${version > 1 ? ` style="outline:2px solid #B45309"` : ""}><b>Scout</b> detected entity types: ${esc(entityType)} — confidence claim ${esc(payload.detected_confidence || "?")} — ${payload.fragments_parsed} fragment(s) parsed`
      + `${(payload.fragments_rejected || []).length ? `, ${(payload.fragments_rejected || []).length} rejected` : ""}. <span class="tag">Claim, not a fact</span></div>`
      + `<div class="cfg-row"><b>Scout</b> detected attributes: ${(rawAttrs.map((a) => `<span class="mono">${esc(a)}</span>`).join(", ") || "—")} — canonical mapping proposed at build time. <span class="tag">Claim, not a fact</span></div>`
      + `<div class="cfg-row"><b>Taxonomist</b> will map raw names at build time under taxonomy <span class="mono">${esc(inForce.taxonomy)}</span>. No terms are auto-created. <span class="tag">Claim, not a fact</span></div>`
      + linkRows
      + `<div class="cfg-row"><b>Config in force</b> — Policy: <span class="mono">${esc(inForce.policy)}</span> · Taxonomy: <span class="mono">${esc(inForce.taxonomy)}</span> (from /health).</div>`
      + `<div class="cfg-row">This approves how we clean, not the final answer. Every final value still has to pass every check.</div>`;
    $("f-config-note").textContent = "Approving pins this config for the next Build. It publishes nothing — the engine still has to derive every value.";
    $("f-config").hidden = false;
    $("f-config-summary").hidden = true;
    const unclear = entityType === "unknown" || (payload.detected_confidence || "") === "low";
    if (unclear) {
      $("f-config-note").textContent = "Type unclear — please confirm type before Build Mosaic.";
      $("f-config-body").insertAdjacentHTML("afterbegin",
        `<div class="cfg-row" style="outline:2px solid #B45309">Type unclear — please confirm type before Build Mosaic.</div>`);
    }
    fState.lastScene0Payload = payload;
    $("f-config-build").disabled = true;
    $("f-config-approve").disabled = unclear;
    feed("Scene 0 config proposed — Scout/Linker claims rendered, Taxonomist mapping queued for build time. Nothing published.");
    window.MosaicChat.add({
      role: "proposer",
      title: "Muse Spark — config proposal",
      lines: [
        "Here is how we would clean this mess.",
        "We found 2 kinds of things: people and products.",
        "We will match people by exact phone, email, or order number.",
        "We recognise these product names: Smartphone, and its other spellings.",
        `<b>Scout:</b> detected entity types: ${esc(window.__uploadedEntityType || payload.detected_entity_type || "unknown")} — confidence claim ${esc(payload.detected_confidence || "?")} — ${payload.fragments_parsed} fragment(s). <span class="tag">Claim, not a fact</span>`,
        `<b>Linker:</b> ${(payload.link_proposals || []).length} link proposal(s). <span class="tag">Claim, not a fact</span>`,
        `<b>Taxonomist:</b> raw names mapped at build time under <span class="mono">${esc(inForce.taxonomy)}</span>. No terms auto-created.`,
        "This approves how we clean, not the final answer. Every final value still has to pass every check.",
      ],
    });
  }

  async function loadScene0() {
    if (fState.scene0 && fState.scene0.decided) {
      // Subsequent loads auto-run like any other upload when the loop runs.
      if (loop.mode === "running") {
        stageScene0File();
        const uploaded = await uploadFile(true, $("f-upload-file"));
        if (uploaded && (window.__uploadedFragments || []).length) {
          await resolveUpload(true);
        }
      } else {
        setStatus("Loop Paused: Scene 0 already decided — click Upload to run manually.");
      }
      return;
    }
    stageScene0File();
    const payload = await uploadFile(false, $("f-upload-file"));
    if (payload) {
      fragments = (payload.fragments || []).map((f) => Object.assign({}, f, { _uploaded: true }));
      tickets = [];
      renderFragments();
      renderFriendly();
      renderScene0Config(payload);
    }
  }

  function stageScene0File() {
    const input = $("f-upload-file");
    const file = new File([JSON.stringify(SCENE0_FRAGMENTS, null, 1)], SCENE0_FILE, { type: "application/json" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
  }

  async function approveScene0() {
    const inForce = scene0ConfigInForce();
    const staged = (window.__uploadedFragments || []).slice();
    if (!staged.length) {
      $("f-config-note").textContent = "Nothing staged — upload the Scene 0 file first.";
      return;
    }
    const version = (fState.scene0 && fState.scene0.version) || 1;
    fState.scene0 = {
      decided: true,
      version,
      approvedAt: new Date().toISOString(),
      policyVersion: inForce.policy,
      taxonomyVersion: inForce.taxonomy,
      entityType: window.__uploadedEntityType || "user",
      fragments: staged,
    };
    $("f-config").hidden = true;
    const summary = $("f-config-summary");
    summary.hidden = false;
    summary.innerHTML = `Config approved — Policy: ${esc(inForce.policy)} — Config approval is not golden approval. `
      + `<button id="f-config-build2" class="primary" type="button">Build Mosaic</button>`;
    $("f-config-build2").addEventListener("click", buildScene0);
    $("f-config-build").disabled = false;
    window.MosaicChat.add({
      role: "downstream",
      title: "Downstream Agent",
      lines: [`Config approved — Policy ${esc(inForce.policy)} pinned. Building now; nothing publishes until the engine derives it.`],
    });
    feed(`Scene 0 config approved — Policy: ${inForce.policy}. No golden published; the engine has not run yet.`);
    // After approval the loop runs the first build — no extra click needed.
    await buildScene0();
  }

  // Edit: the steward corrects the proposed entity type, producing a new
  // proposed version (v2) that must be approved again. Never silent.
  function editScene0() {
    const staged = (window.__uploadedFragments || []);
    if (!staged.length) {
      $("f-config-note").textContent = "Nothing staged — upload the Scene 0 file first.";
      return;
    }
    const current = window.__uploadedEntityType || "user";
    window.__uploadedEntityType = current === "user" ? "product" : "user";
    const version = ((fState.scene0 && fState.scene0.version) || 1) + 1;
    fState.scene0 = { decided: false, version };
    feed(`Scene 0 edited — entity type ${current} → ${window.__uploadedEntityType} (v${version}). Approve again; nothing runs on edit alone.`);
    setStatus(`Config v${version} proposed — entity type ${window.__uploadedEntityType}. Approve to pin it.`);
    renderScene0Config({
      detected_entity_type: window.__uploadedEntityType,
      detected_confidence: "?",
      fragments: staged,
      fragments_parsed: staged.length,
      fragments_rejected: [],
      link_proposals: [],
    });
  }

  function rejectScene0() {
    fState.scene0 = null;
    $("f-config").hidden = true;
    $("f-config-summary").hidden = false;
    $("f-config-summary").innerHTML = "Config rejected. No pipeline run. No golden published.";
    const input = $("f-upload-file");
    if (input) input.value = "";
    window.__uploadedFragments = [];
    window.MosaicChat.add({
      role: "steward",
      title: "Human Steward",
      lines: ["Config rejected. No pipeline run. No golden published."],
    });
    setStatus("Config rejected. No pipeline run. No golden published.");
    feed("Scene 0 config rejected by the steward — nothing runs, nothing published.");
  }

  function cancelScene0() {
    rejectScene0();
  }

  function scene0Documents() {
    const staged = (fState.scene0 && fState.scene0.fragments) || [];
    const entityType = (fState.scene0 && fState.scene0.entityType) || window.__uploadedEntityType || "user";
    const bySource = {};
    staged.forEach((f) => {
      (bySource[f.source] = bySource[f.source] || []).push(f);
    });
    return Object.entries(bySource).map(([source, frags], i) => ({
      document_id: `SC0-${i + 1}`,
      entity_type: entityType,
      source,
      synthetic: true,
      fields: Object.fromEntries(frags.map((f) => [f.attribute, {
        value: f.value, authority_rank: f.authority_rank, event_time: f.event_time,
        verified: f.verified, ttl_seconds: f.ttl_seconds,
      }])),
    }));
  }

  async function buildScene0() {
    if (!fState.scene0 || !fState.scene0.decided) {
      setStatus("Approve the Scene 0 config first — Build runs only on pinned config.");
      return;
    }
    const note = $("f-config-note");
    const summary = $("f-config-summary");
    // 2-second visual pause: config approval must visibly precede any golden,
    // proving approval alone publishes nothing.
    if (note) note.textContent = "Config approved — proving no golden publishes without the engine…";
    fState.working = true;
    renderFStatus();
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const documents = scene0Documents();
      fragments = fragmentsFromDocs(documents);
      tickets = [];
      renderFragments();
      renderFriendly();
      const payload = await api("/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          work_id: `ui-scene0-${Date.now()}`,
          idempotency_key: `ui-scene0-${Date.now()}`,
          dataset: { documents },
        }),
      });
      await afterBuild(payload, false);
      feed(`Scene 0 built — Decision: ${payload.decision_id} — Verdict: ${payload.verdict}. Config approval published nothing; the engine did.`);
    } catch (error) {
      setStatus(`Scene 0 build failed (fail-closed): ${error.message}`);
      feed(`Scene 0 build failed (fail-closed): ${error.message} — nothing published`);
    }
    if (fState.scene0) fState.scene0.done = true;
    fState.working = false;
    renderFStatus();
  }

  async function enqueueCurrentView() {
    const documents = currentViewDocuments();
    if (!documents.length) {
      setStatus("Nothing to enqueue — load fragments first.");
      return;
    }
    try {
      const key = `ui-job-${Date.now()}`;
      const payload = await api("/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ work_id: key, idempotency_key: key, dataset: { documents } }),
      });
      feed(`Job ${payload.job_id.slice(0, 8)} enqueued — worker runs it unattended; same key replays, never duplicates.`);
      setStatus(`Job ${payload.job_id} queued — poll the jobs panel for the verdict.`);
      await loadJobs();
    } catch (error) {
      setStatus(`Enqueue failed (fail-closed): ${error.message}`);
    }
  }

  // Stepwise dashed-to-solid green link transition. Walks the link edges in
  // document order, lighting each one green before moving on; capped so the
  // animation always finishes. Pure presentation: the derivation already
  // happened, this only replays its order on the canvas.
  function animateChain(done) {
    const lines = [...document.querySelectorAll('#graph-canvas line[data-walk="1"]')];
    const steps = lines.slice(0, 14);
    if (!steps.length) {
      if (done) done();
      return;
    }
    feed(`Chain replay: ${steps.length} link hop(s) — dashed proposal to solid verified green.`);
    let i = 0;
    const step = () => {
      if (i >= steps.length) {
        if (done) done();
        return;
      }
      steps[i].classList.add("lit");
      i += 1;
      setTimeout(step, 300);
    };
    setTimeout(step, 250);
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

  // Context shared with drawers.js. Drawers never touch app state directly:
  // reads go through getters, writes through setters, rendering and API
  // through the callbacks below. Anything not listed here is unreachable
  // from drawer code by construction.
  const drawerCtx = {
    $,
    api,
    feed,
    setStatus,
    activeFragments,
    fragmentForAttribute,
    renderFriendly,
    setFriendlyFromResolve,
    renderEngine,
    renderMosaic,
    renderEscalatedCount,
    stewardBody,
    dialog,
    loop,
    currentEntity,
    get policyUser() { return fState.policyUser; },
    get policyProduct() { return fState.policyProduct; },
    get lastResolve() { return lastResolve; },
    set lastResolve(v) { lastResolve = v; },
    get lastStewardItems() { return lastStewardItems; },
    setLastStewardItems(v) { lastStewardItems = v; },
    setPendingAll(v) { pendingAll = v; },
    get lastFriendly() { return lastFriendly; },
  };

  entitySel.addEventListener("change", onEntityChange);
  $("jobs-refresh").addEventListener("click", loadJobs);
  $("jobs-enqueue-view").addEventListener("click", enqueueCurrentView);
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
  $("graph-candidates").addEventListener("change", () => {
    if (lastGraph.nodes.length) renderGraph(lastGraph, null);
  });
  $("downstream-otp").addEventListener("click", () => askDownstream("otp"));
  $("downstream-search").addEventListener("click", () => askDownstream("search"));
  $("pause-btn").addEventListener("click", toggleLoop);
  $("f-pause").addEventListener("click", toggleLoop);
  $("demo-stop").addEventListener("click", async () => {
    await api("/demo/engine/stop", { method: "POST" });
    await refreshHealth();
    feed("Demo: trust-engine stopped (DEMO ONLY) — new runs escalate, nothing publishes.");
  });
  $("demo-start").addEventListener("click", async () => {
    await api("/demo/engine/start", { method: "POST" });
    await refreshHealth();
    feed("Demo: trust-engine started (DEMO ONLY) — retry with the same key to prove the same decision id.");
  });
  $("demo-ticket").addEventListener("click", async () => {
    switchEntity("user/P-001");
    ticketBox.checked = true;
    await loadFragments();
    await resolve(true);
  });
  $("f-loop-review").addEventListener("click", () => MosaicDrawers.openDrawer());
  $("f-try-answer").addEventListener("click", () => runScenario("scene0"));
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
  $("f-try-example").addEventListener("click", () => runScenario("scene0"));
  $("f-scene0").addEventListener("click", () => runScenario("scene0"));
  $("f-config-approve").addEventListener("click", approveScene0);
  $("f-config-edit").addEventListener("click", editScene0);
  $("f-config-reject").addEventListener("click", rejectScene0);
  $("f-config-build").addEventListener("click", buildScene0);
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
  $("steward-open").addEventListener("click", () => MosaicDrawers.openDrawer());
  $("steward-open-2").addEventListener("click", () => MosaicDrawers.openDrawer());
  $("similar-open").addEventListener("click", () => MosaicDrawers.openSimilar(drawerCtx));
  $("explain-decision").addEventListener("click", () => {
    const decisionId = lastFriendly && lastFriendly.decision_id;
    if (decisionId) explain(decisionId);
    else setStatus("Run the picture first — Explain needs a decision id.");
  });
  $("drawer-close").addEventListener("click", () => MosaicDrawers.closeDrawer());
  $("drawer-scrim").addEventListener("click", () => MosaicDrawers.closeDrawer());
  $("taxonomy-drawer-close").addEventListener("click", () => MosaicDrawers.closeDrawer());
  $("similar-drawer-close").addEventListener("click", () => MosaicDrawers.closeDrawer());
  $("f-taxonomy").addEventListener("click", () => MosaicDrawers.openTaxonomy(drawerCtx));
  $("f-similar").addEventListener("click", () => MosaicDrawers.openSimilar(drawerCtx));
  $("taxonomy-badge").addEventListener("click", () => MosaicDrawers.openTaxonomy(drawerCtx));
  $("policy-badge").addEventListener("click", () => MosaicDrawers.openPolicy(drawerCtx));
  $("upload-collapse").addEventListener("click", () => {
    $("upload-body").hidden = true;
    $("upload-collapsed").hidden = false;
  });
  $("upload-expand").addEventListener("click", () => {
    $("upload-body").hidden = false;
    $("upload-collapsed").hidden = true;
  });
  $("feed-toggle-inline").addEventListener("click", () => {
    const list = $("feed-list");
    const hidden = list.style.display === "none";
    list.style.display = hidden ? "" : "none";
    $("feed-toggle-inline").textContent = hidden ? "Activity feed: shown below" : "Activity feed: hidden";
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") MosaicDrawers.closeDrawer();
  });

  renderLoop();
  applyPreRunState();
  renderFriendly();
  wireChatBar();
  MosaicChat.add({
    role: "downstream",
    title: "Downstream Agent",
    lines: ["Waiting for a first run — pick a scenario above, or press Build Mosaic. I only act on published goldens, never on proposals."],
  });
  feed("Autonomous Loop started — Running. Next auto check: on next upload.");
  refreshHealth();
  window.setInterval(refreshHealth, 5000);
  loadJobs();
  window.setInterval(loadJobs, 10000);
  loadEntities()
    .catch(() => {})
    .finally(() => loadFragments().catch((error) => setStatus(`Cannot load fragments: ${error.message}`)));
  MosaicDrawers.loadPending(drawerCtx);
})();
