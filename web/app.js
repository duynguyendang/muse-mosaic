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

  function renderEscalatedCount(k) {
    $("steward-count").textContent = String(k);
    $("loop-escalated").textContent =
      `Escalated, needs a human: ${k} items — see Steward Queue`;
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
      if (loop.taxonomy) {
        $("taxonomy-badge").textContent = `Taxonomy: ${loop.taxonomy}`;
      }
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
    } catch (error) {
      $("api-dot").className = "dot down";
      $("api-status").textContent = "mosaic-api: unreachable";
    }
  }

  function currentEntity() {
    const [entityType, entityId] = entitySel.value.split("/");
    return { entityType, entityId };
  }

  async function loadFragments() {
    const { entityType, entityId } = currentEntity();
    const data = await api(`/fragments/${entityType}/${entityId}`);
    fragments = data.fragments || [];
    tickets = data.tickets || [];
    renderFragments();
    setStatus(
      `Loaded ${fragments.length} synthetic fragments${tickets.length ? ` + ${tickets.length} ticket(s)` : ""}.`
    );
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
    proposerBody.innerHTML = list.map((f) => `
      <div class="card">
        <h3>${esc(f.attribute)} <span class="muted mono">${esc(f.value)}</span></h3>
        <div class="meta mono">${esc(f.source)} / ${esc(f.source_id)} · rank ${esc(f.authority_rank)} ·
        ${f.verified ? "verified" : "unverified"} · ${esc(f.event_time)}</div>
      </div>`).join("") || `<div class="card"><span class="muted">No fragments.</span></div>`;
  }

  function badge(text) {
    return `<span class="badge ${esc(text)}">${esc(text)}</span>`;
  }

  async function resolve(auto) {
    resolveBtn.disabled = true;
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
        $("taxonomy-badge").textContent = `Taxonomy: ${loop.taxonomy}`;
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
    } catch (error) {
      setStatus(`Resolve failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto run failed (fail-closed): ${error.message} — nothing published`);
    } finally {
      resolveBtn.disabled = false;
    }
  }

  function renderProposal(proposal) {
    if (!proposal) return;
    const attrs = Object.entries(proposal.attributes || {});
    const trace = (proposal.subagent_trace || []).map((step) =>
      `<div class="meta">▸ <b>${esc(step.agent || "?")}</b>`
      + `${step.real_call ? " <span class='badge'>REAL</span>" : " <span class='badge'>mock</span>"}`
      + ` — ${esc(step.output_summary || "")}</div>`).join("");
    proposerBody.innerHTML = (trace ? `<div class="card"><h3>Sub-agent trace</h3>${trace}</div>` : "")
      + attrs.map(([name, attr]) => `
      <div class="card">
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
        return `<div><b>${esc(name)}</b>: ${value.map((p, i) =>
          `<div class="mono">#${i} category=${esc(p.category || "?")}`
          + (p._lineage ? ` · ${esc(JSON.stringify(p._lineage))}` : "") + `</div>`).join("")}</div>`;
      }
      const line = typeof value === "object" && value !== null
        ? `${esc(value.value)} <span class="muted">· ${esc(value.rule_fired || "")} · ${esc(value.taxonomy_version || "")}</span>`
        : esc(value);
      return `<div><b>${esc(name)}</b>: ${line}</div>`;
    }).join("");
    $("golden-body").innerHTML =
      `<div>version <span class="mono">${golden.version}</span> · taxonomy <span class="mono">${esc(golden.taxonomy_version || "—")}</span></div>` + attrs;
  }

  function renderEngine(payload) {
    const golden = payload.golden;
    const attrs = golden ? Object.entries(golden.attributes || {}) : [];
    const goldenHtml = attrs.map(([name, value]) => {
      if (name === "purchased_products") return "";
      return `
      <div class="card">
        <h3>${esc(name)} <span class="mono">${esc(value.value)}</span> ${badge("published")}</h3>
        <div class="meta mono">winner ${esc(value.winner_fact_id)} · rule ${esc(value.rule_fired)} ·
        policy ${esc(value.policy_version)} · taxonomy ${esc(value.taxonomy_version || "—")}</div>
        <div class="meta mono">losers: ${(value.loser_fact_ids || []).map(esc).join(", ") || "—"}</div>
        <div class="meta mono">${esc(value.lineage)}</div>
      </div>`;
    }).join("");

    const conflictHtml = (payload.conflicts || []).map((c) => `
      <div class="card">
        <h3>${esc(c.attribute)} ${badge("escalated")}</h3>
        <div class="meta mono">${esc(c.reason_code)} · rule ${esc(c.rule_fired)} · policy ${esc(c.policy_version)} · taxonomy ${esc(c.taxonomy_version || "—")}</div>
        <div class="meta mono">facts: ${(c.fact_ids || []).map(esc).join(", ")}</div>
      </div>`).join("");

    const vlinkHtml = ((payload.verified_links && payload.verified_links.length) ? `
      <div class="card"><h3>Verified links (L1)</h3>
      <div class="meta mono">${payload.verified_links.map((l) =>
        `${esc(l.from_fact_id)} —${esc(l.proof)}→ ${esc(l.to_fact_id)}`).join("<br/>")}</div></div>` : "");

    engineBody.innerHTML = `
      <div class="card">
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

  async function stewardAccept(id, suggested) {
    const version = window.prompt("New taxonomy version:", "tax-product-2026-10-27-v2");
    if (!version) return;
    try {
      const payload = await api(`/steward/${id}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: suggested, taxonomy_version: version }),
      });
      setStatus(`Taxonomy ${version} created; re-derived. Golden: ${payload.golden ? "published" : "withheld"}.`);
      feed(`Steward accepted taxonomy suggestion — new version ${version}; re-derived, no hand edit`);
      loadPending();
    } catch (error) {
      setStatus(`Accept failed (fail-closed): ${error.message}`);
    }
  }

  async function stewardReject(id) {
    await api(`/steward/${id}/reject`, { method: "POST" });
    setStatus("Steward rejected the escalated attribute. No golden written.");
    feed(`Steward rejected escalated attribute — golden unchanged, decision recorded`);
    loadPending();
  }

  async function stewardApprove(id) {
    const value = window.prompt("Approve with exact value:");
    if (value == null) return;
    const payload = await api(`/steward/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    lastResolve = payload;
    renderEngine(payload);
    renderSteward(payload.steward_queue || []);
    renderMosaic(payload);
    renderEscalatedCount((payload.steward_queue || []).length);
    loadPending();
    setStatus(`Steward approved ${id}; re-derived as a new fact.`);
    feed(`Steward approved ${id} as a new fact — re-derived, Decision: ${payload.decision_id}`);
  }

  async function stewardSplit(id) {
    const payload = await api(`/steward/${id}/split`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restore_version: 1 }),
    });
    setStatus(`Split restored v1 as v${payload.golden_version} (decision ${payload.decision_id}).`);
    feed(`Steward split identity — restored v1 as v${payload.golden_version}, Decision: ${payload.decision_id}`);
    loadPending();
  }

  function stewardCardHtml(item) {
    return `
      <div class="card" data-steward="${esc(item.steward_id)}">
        <h3>${esc(item.attribute)} <span class="muted mono">${esc(item.status)}</span></h3>
        <div class="meta">owner ${esc(item.owner)} · deputy ${esc(item.deputy || "—")}</div>
        <div class="meta">deadline <span class="mono">${esc(item.deadline)}</span></div>
        <div class="meta">fallback: ${esc(item.fallback)}</div>
        ${item.suggested_action ? `<div class="meta">suggested: <span class="mono">${esc(item.suggested_action)}</span></div>` : ""}
        <div class="steward-actions">
          <button data-approve="${esc(item.steward_id)}">Approve w/ value…</button>
          <button data-reject="${esc(item.steward_id)}">Reject</button>
          <button data-split="${esc(item.steward_id)}">Split to v1</button>
          ${item.suggested_action ? `<button data-accept="${esc(item.steward_id)}" data-action="${esc(item.suggested_action)}">Accept suggested</button>` : ""}
        </div>
      </div>`;
  }

  function bindStewardActions(root) {
    root.querySelectorAll("[data-accept]").forEach((btn) => {
      btn.addEventListener("click", () => stewardAccept(btn.getAttribute("data-accept"), btn.getAttribute("data-action")));
    });
    root.querySelectorAll("[data-reject]").forEach((btn) => {
      btn.addEventListener("click", () => stewardReject(btn.getAttribute("data-reject")));
    });
    root.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.addEventListener("click", () => stewardApprove(btn.getAttribute("data-approve")));
    });
    root.querySelectorAll("[data-split]").forEach((btn) => {
      btn.addEventListener("click", () => stewardSplit(btn.getAttribute("data-split")));
    });
  }

  function renderSteward(items) {
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
    const color = { Fragment: "#38bdf8", Entity: "#a78bfa", Golden: "#34d399", Taxonomy: "#fbbf24", Steward: "#f472b6", Document: "#94a3b8" };
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
        + `<circle cx="${p.x}" cy="${p.y}" r="9" fill="${color[n.node_type] || "#64748b"}" stroke="#0b1020" stroke-width="2"><title>${esc(n.node_id)} [${esc(n.node_type)}]${n.source ? " · " + esc(n.source) : ""}</title></circle>`
        + `<text x="${p.x + 12}" y="${p.y + 4}" font-size="10" fill="#93a0c2">${esc(label)}</text></g>`;
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
      setStatus(`Graph loaded: ${lastGraph.nodes.length} nodes, ${lastGraph.edges.length} edges.`);
    } catch (error) {
      setStatus(`Graph failed: ${error.message}`);
    }
  }

  async function graphPath() {
    const { entityType, entityId } = currentEntity();
    try {
      const from = window.prompt("Pathfinder from node id:", "nd-fragment-user-P-001-sf-001-phone");
      const to = window.prompt("...to node id:", "nd-fragment-user-P-001-tck-001-phone");
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
      const field = window.prompt("Blast radius for golden field:", "phone");
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
    card.className = "card";
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
    const { entityType } = currentEntity();
    const docsBySource = {};
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
    }
  }

  async function uploadFile(auto) {
    const input = $("upload-file");
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
        $("taxonomy-badge").textContent = `Taxonomy: ${loop.taxonomy}`;
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
    } catch (error) {
      setStatus(`Upload resolve failed (fail-closed): ${error.message}`);
      if (auto) feed(`Auto run failed (fail-closed): ${error.message} — nothing published`);
    }
  }

  async function askDownstream(agent, opts) {
    const auto = !!(opts && opts.auto);
    const { entityType, entityId } = currentEntity();
    const field = agent === "search" ? "category" : "phone";
    try {
      const payload = await api(`/downstream/${agent}?entity_type=${entityType}&entity_id=${entityId}&field=${field}`);
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

  async function explain(decisionId) {
    const data = await api(`/explain/${decisionId}`);
    $("explain-id").textContent = decisionId;
    const tree = (data.derivation_tree || []).join("\n");
    const trace = (data.subagent_trace || []).map((s) =>
      `▸ ${s.agent || "?"}${s.real_call ? " [REAL]" : " [mock]"} — ${s.output_summary || ""}`).join("\n");
    const wonder = data.wonder_note ? `Wonder: ${JSON.stringify(data.wonder_note)}\n\n` : "";
    $("explain-body").textContent =
      `RULE ${data.rule_fired} · POLICY ${data.policy_version} · TAXONOMY ${data.taxonomy_version || "—"}\n\n`
      + `DERIVATION TREE\n${tree || "(empty)"}\n\n`
      + `SUBAGENT TRACE\n${trace || "(none)"}\n\n`
      + wonder
      + JSON.stringify(data, null, 1);
    dialog.showModal();
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
      tiles.push({ kind: "user", label: `${name}=${value.value}`, wide: false, detail: value });
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

  entitySel.addEventListener("change", loadFragments);
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
  $("pause-btn").addEventListener("click", () => {
    loop.mode = loop.mode === "running" ? "paused" : "running";
    renderLoop();
    feed(loop.mode === "running"
      ? "Autonomous Loop resumed — next auto check: on next upload."
      : "Autonomous Loop paused — no auto run until resumed.");
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
  feed("Autonomous Loop started — Running. Next auto check: on next upload.");
  refreshHealth();
  window.setInterval(refreshHealth, 10000);
  loadFragments().catch((error) => setStatus(`Cannot load fragments: ${error.message}`));
  loadPending();
})();
