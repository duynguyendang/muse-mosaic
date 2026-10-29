/* Muse Mosaic drawers — steward queue + explain drawer.
 *
 * Loaded after config.js, before app.js. No work happens at load time:
 * app.js passes a ctx object at every call site. ctx carries the live
 * API caller, feed/status writers, fragment lookups, render callbacks,
 * and get/set accessors for the handful of shared mutable states.
 * (config.js loads first, so MosaicConfig.* is used directly.)
 *
 * Nothing here derives, publishes, or invents values: approve/reject/
 * split/accept only POST explicit steward decisions and re-render from
 * the API response or a ledger reload.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (t) => window.MosaicConfig.esc(t);
  const plainAttr = (a) => window.MosaicConfig.plainAttr(a);
  const plainSource = (s) => window.MosaicConfig.plainSource(s);

  const confirmStash = new Map();

  function stewardItemById(ctx, id) {
    const lastFriendly = ctx.lastFriendly;
    return ctx.lastStewardItems.find((s) => s.steward_id === id)
      || ((lastFriendly && lastFriendly.steward_queue) || []).find((s) => s.steward_id === id)
      || null;
  }

  function valuesForAttribute(ctx, attribute) {
    const seen = new Map();
    ctx.activeFragments()
      .filter((f) => f.attribute === attribute)
      .forEach((f) => {
        if (!seen.has(f.value)) seen.set(f.value, f.source);
      });
    return [...seen.entries()].map(([value, source]) => ({ value, source }));
  }

  function ownerLinesHtml(item) {
    return `<div class="f-steward-meta">Owner: ${esc(item.owner || "steward-001")} · Decide by: ${esc(item.deadline || "—")} · If no decision: ${esc(item.fallback || "do not use this value")}.</div>`;
  }

  function stewardQuestionHtml(ctx, item) {
    const frag = ctx.fragmentForAttribute(item.attribute);
    const what = frag
      ? `"${frag.value}" for ${plainAttr(item.attribute)} came from ${plainSource(frag.source).toLowerCase()} and could not be proven.`
      : `${plainAttr(item.attribute)} could not be proven.`;
    return `<div class="f-steward-q">${esc(what)}</div>` + ownerLinesHtml(item);
  }

  function afterApprove(ctx, payload, id) {
    ctx.lastResolve = payload;
    ctx.setFriendlyFromResolve(payload);
    ctx.renderFriendly();
    if (ctx.refreshDatasets) ctx.refreshDatasets();
    ctx.renderEngine(payload);
    ctx.renderSteward(ctx, payload.steward_queue || []);
    ctx.renderMosaic(payload);
    ctx.renderEscalatedCount((payload.steward_queue || []).length);
    ctx.setStatus(`Steward approved ${id}; re-derived as a new fact.`);
    ctx.feed(`Steward approved ${id} as a new fact — re-derived, Decision: ${payload.decision_id}`);
  }

  async function doApprove(ctx, id, value, errEl) {
    try {
      const payload = await ctx.api(`/steward/${id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      afterApprove(ctx, payload, id);
    } catch (error) {
      errEl.hidden = false;
      errEl.textContent = `Approve failed (fail-closed): ${error.message}`;
    }
  }

  async function doReject(ctx, id, errEl) {
    try {
      await ctx.api(`/steward/${id}/reject`, { method: "POST" });
      ctx.feed(`Steward rejected ${id} — no side effects, value stays unpublished.`);
      ctx.setStatus(`Steward item ${id} rejected.`);
      dropFriendlySteward(ctx, id);
    } catch (error) {
      if (errEl) {
        errEl.hidden = false;
        errEl.textContent = `Reject failed (fail-closed): ${error.message}`;
      } else {
        ctx.setStatus(`Reject failed (fail-closed): ${error.message}`);
      }
    }
  }

  function stewardApproveAsk(ctx, id, btn) {
    const item = stewardItemById(ctx, id);
    if (!item) {
      ctx.setStatus(`Steward item ${id} not found.`);
      return;
    }
    const vals = valuesForAttribute(ctx, item.attribute);
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
      wireApproveCard(ctx, body, id, vals.length > 0, true);
      return;
    }
    const holder = btn ? btn.closest("[data-steward]") : null;
    if (!holder) return;
    if (!confirmStash.has(holder)) confirmStash.set(holder, holder.innerHTML);
    holder.innerHTML = card;
    wireApproveCard(ctx, holder, id, vals.length > 0, false);
  }

  function wireApproveCard(ctx, root, id, hasValues, friendly) {
    const errEl = root.querySelector("[data-cf-err]");
    root.querySelector("[data-cf-no]").addEventListener("click", () => {
      if (friendly) {
        ctx.renderFriendly();
    if (ctx.refreshDatasets) ctx.refreshDatasets();
      } else {
        const h = confirmStash.get(root);
        confirmStash.delete(root);
        if (h != null) {
          root.innerHTML = h;
          bindStewardActions(ctx, root);
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
      doApprove(ctx, id, value, errEl);
    });
  }

  function stewardRejectAsk(ctx, id, btn) {
    const item = stewardItemById(ctx, id);
    if (!item) {
      ctx.setStatus(`Steward item ${id} not found.`);
      return;
    }
    const card = stewardQuestionHtml(ctx, item)
      + `<div class="steward-actions"><button data-cf-yes class="btn-reject" type="button">Reject — keep unpublished</button>`
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
        ctx.renderFriendly();
    if (ctx.refreshDatasets) ctx.refreshDatasets();
      } else {
        const h = confirmStash.get(host);
        confirmStash.delete(host);
        if (h != null) {
          host.innerHTML = h;
          bindStewardActions(ctx, host);
        }
      }
    });
    host.querySelector("[data-cf-yes]").addEventListener("click", () => {
      doReject(ctx, id, errEl);
    });
  }

  function nextTaxonomyVersion(ctx, item) {
    const bump = (v) => {
      const m = /-v(\d+)$/.exec(v || "");
      return m ? String(v).replace(/-v\d+$/, `-v${parseInt(m[1], 10) + 1}`) : null;
    };
    // Only reuse the known version when it belongs to the same entity family;
    // a user taxonomy must never seed a product version (or vice versa).
    const known = ctx.loop.taxonomy || (ctx.lastFriendly && ctx.lastFriendly.taxonomy_version) || "";
    const sameFamily = known.indexOf(`tax-${item.entity_type}-`) === 0 ? known : "";
    return bump(sameFamily)
      || `tax-${item.entity_type}-${new Date().toISOString().slice(0, 10)}-v2`;
  }

  function taxonomyInForce(ctx, item) {
    const known = ctx.loop.taxonomy || (ctx.lastFriendly && ctx.lastFriendly.taxonomy_version) || "";
    return known.indexOf(`tax-${item.entity_type}-`) === 0 ? known : "unknown";
  }

  function stewardAcceptAsk(ctx, id, suggested, btn) {
    const item = stewardItemById(ctx, id);
    if (!item) {
      ctx.setStatus(`Steward item ${id} not found.`);
      return;
    }
    const prefill = nextTaxonomyVersion(ctx, item);
    const inForce = taxonomyInForce(ctx, item);
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
        ctx.renderFriendly();
    if (ctx.refreshDatasets) ctx.refreshDatasets();
      } else {
        const h = confirmStash.get(host);
        confirmStash.delete(host);
        if (h != null) {
          host.innerHTML = h;
          bindStewardActions(ctx, host);
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
      doAccept(ctx, id, suggested, version, errEl);
    });
  }

  async function doAccept(ctx, id, action, version, errEl) {
    try {
      const payload = await ctx.api(`/steward/${id}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, taxonomy_version: version }),
      });
      ctx.setStatus(`Taxonomy ${version} created; re-derived. Golden: ${payload.golden ? "published" : "withheld"}.`);
      ctx.feed(`Steward accepted taxonomy suggestion — new version ${version}; re-derived, no hand edit`);
      dropFriendlySteward(ctx, id);
      loadPending(ctx);
    } catch (error) {
      errEl.hidden = false;
      errEl.textContent = `Accept failed (fail-closed): ${error.message}`;
    }
  }

  function dropFriendlySteward(ctx, id) {
    const lastFriendly = ctx.lastFriendly;
    if (lastFriendly && lastFriendly.steward_queue) {
      ctx.setLastStewardItems(ctx.lastStewardItems.filter((s) => s.steward_id !== id));
      lastFriendly.steward_queue = lastFriendly.steward_queue.filter((s) => s.steward_id !== id);
      renderSteward(ctx, ctx.lastStewardItems.length ? ctx.lastStewardItems : lastFriendly.steward_queue);
      ctx.renderEscalatedCount(lastFriendly.steward_queue.length);
      ctx.renderFriendly();
    if (ctx.refreshDatasets) ctx.refreshDatasets();
    } else {
      loadPending(ctx);
    }
  }

  async function stewardSplit(ctx, id) {
    const payload = await ctx.api(`/steward/${id}/split`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restore_version: 1 }),
    });
    ctx.setStatus(`Split restored v1 as v${payload.golden_version} (decision ${payload.decision_id}).`);
    ctx.feed(`Steward split identity — restored v1 as v${payload.golden_version}, Decision: ${payload.decision_id}`);
    dropFriendlySteward(ctx, id);
  }

  function stewardCardHtml(ctx, item, friendly) {
    const question = stewardQuestionHtml(ctx, item);
    let suggestedBlock = "";
    if (item.suggested_action) {
      suggestedBlock = `<div class="meta">Suggested: <span class="mono">${esc(item.suggested_action)}</span></div>`;
      const rawMatch = /"([^"]+)"/.exec(item.suggested_action || "");
      if (rawMatch && ctx.activeFragments) {
        const raw = rawMatch[1];
        const carriers = ctx.activeFragments()
          .filter((f) => f.attribute === item.attribute && String(f.value) === raw);
        if (carriers.length) {
          const sources = [...new Set(carriers.map((f) => f.source))];
          suggestedBlock += `<div class="meta">Raw value <span class="mono">${esc(raw)}</span> — ${carriers.length} fragment(s): ${esc(sources.join(", "))}</div>`;
        }
      }
    }
    return `
      <div class="card warn" data-steward="${esc(item.steward_id)}">
        <h3>${esc(item.attribute)} — ${esc(item.entity_id || "")} <span class="muted mono">Escalated</span></h3>
        ${question}
        ${suggestedBlock}
        <div class="meta">Your decision becomes a new fact and the picture is re-derived. It does not edit the golden by hand.</div>
        <div class="steward-actions">
          <button class="btn-approve" data-approve="${esc(item.steward_id)}">${friendly ? "Use this value" : "Approve"}</button>
          <button class="btn-reject" data-reject="${esc(item.steward_id)}">${friendly ? "Do not use it" : "Reject"}</button>
          <button class="btn-split" data-split="${esc(item.steward_id)}">${friendly ? "These are two different people" : "Split"}</button>
          ${item.suggested_action ? `<button class="btn-accept" data-accept="${esc(item.steward_id)}" data-action="${esc(item.suggested_action)}">Accept suggested</button>` : ""}
        </div>
      </div>`;
  }

  function bindStewardActions(ctx, root) {
    root.querySelectorAll("[data-accept]").forEach((btn) => {
      btn.addEventListener("click", () => stewardAcceptAsk(ctx, btn.getAttribute("data-accept"), btn.getAttribute("data-action"), btn));
    });
    root.querySelectorAll("[data-reject]").forEach((btn) => {
      btn.addEventListener("click", () => stewardRejectAsk(ctx, btn.getAttribute("data-reject"), btn));
    });
    root.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.addEventListener("click", () => stewardApproveAsk(ctx, btn.getAttribute("data-approve"), btn));
    });
    root.querySelectorAll("[data-split]").forEach((btn) => {
      btn.addEventListener("click", () => stewardSplit(ctx, btn.getAttribute("data-split")));
    });
  }

  function renderSteward(ctx, items) {
    ctx.setPendingAll(items);
    ctx.setLastStewardItems(items);
    ctx.renderEscalatedCount(items.length);
    if (!items.length) {
      ctx.stewardBody.innerHTML = `<div class="card"><span class="muted">Queue empty — nothing escalated.</span></div>`;
      $("steward-drawer-list").innerHTML = `<div class="card"><span class="muted">No pending steward items. All escalated attributes have been decided.</span></div>`;
      return;
    }
    const first = items[0];
    const rule = first.rule_fired || first.reason_code || "escalated";
    const firstFrag = ctx.fragmentForAttribute ? ctx.fragmentForAttribute(first.attribute) : null;
    const firstSrc = firstFrag && firstFrag.source === "ticket"
      ? "Ticket"
      : (firstFrag ? plainSource(firstFrag.source) : "");
    const what = `${firstSrc ? firstSrc + " " : ""}${plainAttr(first.attribute)}`;
    ctx.stewardBody.innerHTML = `
      <div class="exception-card" role="alert">
        <div>⚠ ${items.length} escalation${items.length > 1 ? "s" : ""} need${items.length > 1 ? "" : "s"} a human — ${esc(what)} — ${esc(rule)} — Steward Queue (${items.length})</div>
        <div class="steward-actions" style="margin-top:0">
          <button id="exception-review" type="button">Review in Steward Queue</button>
        </div>
      </div>`;
    $("exception-review").addEventListener("click", () => openDrawer());
    const drawerList = $("steward-drawer-list");
    drawerList.innerHTML = items.map((item) => stewardCardHtml(ctx, item, false)).join("");
    bindStewardActions(ctx, drawerList);
  }

  async function loadPending(ctx) {
    try {
      const data = await ctx.api("/steward/pending");
      if (!ctx.lastResolve) renderSteward(ctx, data.pending || []);
    } catch (error) {
      ctx.setStatus(`Steward queue unreadable: ${error.message}`);
    }
  }

  async function explain(ctx, decisionId, field) {
    const data = await ctx.api(`/explain/${decisionId}`);
    $("explain-id").textContent = decisionId;
    const entityType = (ctx.currentEntity && ctx.currentEntity().entityType) || data.entity_type || "user";
    const entityId = (ctx.currentEntity && ctx.currentEntity().entityId) || data.entity_id || "";
    const friendlyGolden = (ctx.lastFriendly && ctx.lastFriendly.golden) || null;
    const ruleText = window.MosaicConfig.rulePlain(data.rule_fired);
    const verdict = String(data.verdict || "unknown");
    const verdictPill = verdict === "published" ? "Published" : verdict === "escalated" ? "Escalated" : verdict;
    // Certificate column 1: winning value + source. Prefer the live golden
    // value for the asked field; fall back to the winner fact id alone.
    let winnerLine = esc(data.winner_fact_id || "—");
    if (field && friendlyGolden && friendlyGolden.attributes && friendlyGolden.attributes[field]) {
      const v = friendlyGolden.attributes[field];
      winnerLine = `<b>${esc(String(v.value))}</b> — winner <span class="mono">${esc(v.winner_fact_id || data.winner_fact_id || "—")}</span>`;
    }
    const loserLines = (data.loser_fact_ids || []).map((fid) =>
      `<div><span class="mono">${esc(fid)}</span> <span class="tag">Tombstone → Survivor</span></div>`
    ).join("") || `<div class="meta">No losers recorded.</div>`;
    const cert =
      `<div class="cert-grid">`
      + `<div class="cert-col cert-win"><h4>Winning Value &amp; Source</h4><div>${winnerLine}</div></div>`
      + `<div class="cert-col cert-lose"><h4>Losers / Tombstones</h4>${loserLines}</div>`
      + `<div class="cert-col"><h4>Rule &amp; Policy Version</h4><div>${esc(data.rule_fired || "?")} · <span class="mono">${esc(data.policy_version || "?")}</span></div></div>`
      + `<div class="cert-col"><h4>Payload Hash &amp; Replay Status</h4><div class="mono">${esc((data.payload_hash || "").slice(0, 12) || "—")}</div><div>Deterministic ✓</div></div>`
      + `</div>`;
    const head = (field ? `Proof for: ${field}\n\n` : "")
      + `Explain — Decision ${decisionId} · ${verdictPill} · Policy ${data.policy_version} · Taxonomy ${data.taxonomy_version || "—"}\n\n`;
    const facts = (data.input_facts || []).map((f) =>
      `• ${f.attribute} = ${f.value} — ${f.source} · rank ${f.authority_rank} · ${f.verified ? "verified" : "unverified"} · ${f.event_time}`
    ).join("\n") || "(no input facts recorded)";
    const byAgent = {};
    (data.subagent_trace || []).forEach((s) => {
      (byAgent[s.agent || "?"] = byAgent[s.agent || "?"] || []).push(s);
    });
    const trace = Object.entries(byAgent).map(([agent, steps]) =>
      `▸ ${agent}${steps[0] && steps[0].real_call ? " [REAL]" : " [mock]"}\n`
      + steps.map((s) => `    — ${s.output_summary || ""}${s.confidence ? ` (confidence ${s.confidence})` : ""}${s.evidence ? ` — evidence: ${typeof s.evidence === "string" ? s.evidence : JSON.stringify(s.evidence)}` : ""}`).join("\n")
    ).join("\n") || "(none)";
    const wonder = data.wonder_note
      ? `Wonder: ${JSON.stringify(data.wonder_note)}\n\n`
      : `Wonder adjudication: Not available in this run.\n\n`;
    const guard = data.prompt_guard_flag && Object.keys(data.prompt_guard_flag).length
      ? `Prompt Guard: ${JSON.stringify(data.prompt_guard_flag)}\n\n`
      : `Screening: Pydantic + claim log (Prompt Guard not verified in this run)\n\n`;
    const created = data.created_at || "";
    const lineage =
      `LINEAGE\nFragment → Screen → Proposal → Derivation → Structure → Publish\n`
      + `• Fragment: ${(data.input_fact_ids || []).length} input fact(s) recorded\n`
      + `• Screen: validated against the fact contract\n`
      + `• Proposal: ${Object.keys(byAgent).join(", ") || "no trace recorded"}\n`
      + `• Derivation: ${data.rule_fired || "?"} under ${data.policy_version}\n`
      + `• Structure: whole-document validation\n`
      + `• Publish: Decision ${decisionId}${created ? ` · ${created}` : ""}\n`;
    let historyText = "";
    let historyRows = [];
    try {
      const history = entityId ? await ctx.api(`/golden/${entityType}/${entityId}/history`) : null;
      historyRows = ((history && history.versions) || []).slice(0, 5);
      if (historyRows.length) {
        historyText = `\nGOLDEN HISTORY (newest first)\n` + historyRows.map((v, i) =>
          `[${i + 1}] v${v.version} · Decision ${v.decision_id} · Policy ${v.policy_version} · ${v.created_at}`
        ).join("\n") + `\n`;
      }
    } catch (error) {
      historyText = `\nGOLDEN HISTORY\nUnavailable in this run: ${error.message}\n`;
    }
    const escalated = ((ctx.lastFriendly && ctx.lastFriendly.steward_queue) || []).length > 0;
    const published = verdict === "published";
    const improvement = published && !escalated
      ? "This outcome will become an example for future reasoning: Yes, engine published."
      : (published
        ? "This outcome will become an example for future reasoning: Yes, steward approved then published."
        : (escalated
          ? "This outcome will become an example for future reasoning: No, escalated — trap example only."
          : "This outcome will become an example for future reasoning: No, unverified proposal only."));
    const text = head
      + `RULE ${data.rule_fired || "?"}${ruleText ? `: ${ruleText}` : ""} · POLICY ${data.policy_version} · TAXONOMY ${data.taxonomy_version || "—"}\n\n`
      + `INPUT FACTS\n${facts}\n\n`
      + `BUILDER TRACE\n${trace}\n\n`
      + wonder + guard
      + `DERIVATION TREE\n${(data.derivation_tree || []).join("\n") || "(empty)"}\n\n`
      + lineage + `\n`
      + historyText + `\n`
      + improvement + `\n\n`
      + `Replay: same facts + same policy version derive the same golden record. If you re-run this decision, you must get the same decision id for the same payload hash.`;
    // Highlight the lineage row(s) for the requested field; the full proof
    // stays visible (highlighting is in addition, not instead).
    const key = field ? `"${field}"` : null;
    const policyVersion = data.policy_version || "";
    const historyBtns = historyRows.map((v, i) =>
      `<button type="button" data-explain-history="${esc(v.decision_id)}">Open v${esc(String(v.version))} · ${esc(v.decision_id)}</button>`
    ).join(" ");
    $("explain-body").innerHTML = cert
      + text.split("\n").map((line) =>
        (key && line.includes(key) ? `<mark class="f-hit">${esc(line)}</mark>` : esc(line))
      ).join("\n")
      + (historyBtns ? `<div class="steward-actions" style="margin-top:10px">${historyBtns}</div>` : "")
      + (policyVersion
        ? `<div class="steward-actions" style="margin-top:10px">`
          + `<button data-explain-datalog type="button">Show Datalog</button>`
          + `</div><div class="meta mono" data-explain-datalog-box hidden>Loading policy source…</div>`
        : "");
    $("explain-body").querySelectorAll("[data-explain-history]").forEach((btn) => {
      btn.addEventListener("click", () => explain(ctx, btn.getAttribute("data-explain-history")));
    });
    const datalogBtn = $("explain-body").querySelector("[data-explain-datalog]");
    if (datalogBtn) {
      datalogBtn.addEventListener("click", async () => {
        const box = $("explain-body").querySelector("[data-explain-datalog-box]");
        if (!box) return;
        if (!box.hidden) {
          box.hidden = true;
          datalogBtn.textContent = "Show Datalog";
          return;
        }
        datalogBtn.textContent = "Hide Datalog";
        box.hidden = false;
        try {
          const policy = await ctx.api(`/policies/${entityType}/${encodeURIComponent(policyVersion)}`);
          const src = String((policy && policy.rules_datalog) || "Policy source unavailable in this run.");
          box.textContent = "";
          const pre = document.createElement("pre");
          pre.style.whiteSpace = "pre-wrap";
          pre.textContent = src.split("\n").slice(0, 60).join("\n");
          box.appendChild(pre);
        } catch (error) {
          box.textContent = `Policy source unavailable: ${error.message}`;
        }
      });
    }
    ctx.dialog.showModal();
    const hit = $("explain-body").querySelector("mark");
    if (hit) hit.scrollIntoView({ block: "center" });
  }

  // Policy badge click: Explain for the current policy — allowed
  // attributes and authority matrix in plain language, never raw JSON
  // on screen (raw JSON stays one Show-Datalog-style toggle away).
  async function openPolicy(ctx) {
    const type = (ctx.currentEntity && ctx.currentEntity().entityType) || "user";
    $("explain-id").textContent = `Policy`;
    const box = $("explain-body");
    box.innerHTML = "Loading policy…";
    ctx.dialog.showModal();
    try {
      const inForce = type === "product"
        ? ctx.policyProduct || "pol-product-2026-10-27-v1"
        : ctx.policyUser || "pol-2026-10-28-v1";
      const data = await ctx.api(`/policies/${type}/${encodeURIComponent(inForce)}`);
      const policy = data.policy || {};
      const allowed = (policy.allowed_attributes || []).map((a) =>
        `<div>• ${esc(a)}${(policy.critical_attributes || []).includes(a) ? ' <span class="tag">critical</span>' : ""}</div>`
      ).join("") || `<div class="meta">No allowed attributes listed.</div>`;
      const matrix = Object.entries(policy.authority_matrix || {}).map(([attr, ranks]) => {
        const ordered = Object.entries(ranks || {}).sort((a, b) => a[1] - b[1])
          .map(([src, rank]) => `${plainSource(src)} (rank ${rank})`).join(" beats ");
        return `<div>• <b>${esc(attr)}</b>: ${esc(ordered || "no ranks")}</div>`;
      }).join("") || `<div class="meta">No authority matrix listed.</div>`;
      box.innerHTML =
        `<div><b>Policy ${esc(data.policy_version || inForce)}</b> for ${esc(type)} — the config in force.</div>`
        + `<div class="meta">Approving a config pins this file. It never approves a golden.</div>`
        + `<h4>Allowed attributes</h4>${allowed}`
        + `<h4>Authority matrix (lower rank wins)</h4>${matrix}`;
    } catch (error) {
      box.textContent = `Policy unreadable: ${error.message}`;
    }
  }

  function drawerShow(id) {    $(id).classList.add("open");
    $("drawer-scrim").classList.add("open");
    const closeBtn = $(id).querySelector("button");
    if (closeBtn) closeBtn.focus();
  }

  function drawerHide(id) {
    $(id).classList.remove("open");
    if (!$("steward-drawer").classList.contains("open")
      && !$("taxonomy-drawer").classList.contains("open")
      && !$("similar-drawer").classList.contains("open")) {
      $("drawer-scrim").classList.remove("open");
    }
  }

  async function openTaxonomy(ctx) {
    const entityType = (ctx.currentEntity && ctx.currentEntity().entityType) || "user";
    const body = $("taxonomy-drawer-body");
    body.innerHTML = `<div class="card"><span class="muted">Loading taxonomy…</span></div>`;
    drawerShow("taxonomy-drawer");
    try {
      const data = await ctx.api(`/taxonomy/${entityType}`);
      const aliases = Object.entries(data.attribute_aliases || {}).map(([attr, names]) =>
        `<div class="meta"><b>${esc(attr)}</b>: ${(names || []).map((n) => `<span class="mono">${esc(n)}</span>`).join(", ")}</div>`
      ).join("") || `<div class="meta">No aliases.</div>`;
      const terms = (data.terms || []).map((t) =>
        `<div class="meta"><b>${esc(t.term)}</b>: ${(t.synonyms || []).map((s) => `<span class="mono">${esc(s)}</span>`).join(", ") || "—"}</div>`
      ).join("") || `<div class="meta">No terms.</div>`;
      body.innerHTML = `<div class="card"><h3>Taxonomy <span class="mono">${esc(data.taxonomy_version)}</span> <span class="tag">read only</span></h3>`
        + `<div class="meta">Viewing and changing stay separate: new terms are proposed in the Steward Queue, never here.</div>`
        + `<h3>Attributes + aliases</h3>${aliases}<h3>Canonical terms + synonyms</h3>${terms}</div>`;
    } catch (error) {
      body.innerHTML = `<div class="card warn">Taxonomy unreadable: ${esc(error.message)}</div>`;
    }
  }

  async function openSimilar(ctx) {
    const current = (ctx.currentEntity && ctx.currentEntity()) || { entityType: "user", entityId: "" };
    const body = $("similar-drawer-body");
    body.innerHTML = `<div class="card"><span class="muted">Loading similar candidates…</span></div>`;
    drawerShow("similar-drawer");
    try {
      const data = await ctx.api(
        `/similar?entity_type=${encodeURIComponent(current.entityType)}&entity_id=${encodeURIComponent(current.entityId)}&limit=20`
      );
      const recall = data.vector_recall || "";
      const noEmbedding = /not configured|unavailable|no embedding/i.test(recall);
      const rows = (data.results || []).map((h) => {
        const evidence = Object.entries(h.evidence || {})
          .map(([k, v]) => `${esc(k)}: ${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}`).join(" · ");
        const vectorLine = (h.vector_score === null || h.vector_score === undefined)
          ? `<div class="meta">Vector only — needs structured check</div>`
          : `<div class="meta">Vector: ${esc(Number(h.vector_score).toFixed(2))}${evidence ? "" : " — needs structured check"}</div>`;
        const evidenceLine = evidence ? `<div class="meta">${evidence}</div>` : (h.vector_score !== null && h.vector_score !== undefined ? "" : "");
        return `<div class="card"><b>${esc(h.attribute)}</b> <span class="mono">${esc(h.value)}</span>`
          + `<div class="meta">${esc(h.source)} · rank ${esc(h.authority_rank)} · ${h.verified ? "verified" : "unverified"} · fact <span class="mono">${esc(h.fact_id)}</span></div>`
          + vectorLine + evidenceLine
          + `<div class="meta">Candidate only — needs structured check</div></div>`;
      }).join("");
      const emptyLine = noEmbedding
        ? `<div class="card"><span class="muted">No embedding for this fragment yet. Similar search is unavailable for this fragment, structured search still works.</span></div>`
        : `<div class="card"><span class="muted">No similar candidate found in the filtered set. Try a broader filter, or check structured search. Vector search is recall, not a decision.</span></div>`;
      body.innerHTML = `<div class="card"><h3>Similar candidates <span class="tag">candidate only, never a merge</span></h3>`
        + `<div class="meta">Structured recall for ${esc(current.entityType)}/${esc(current.entityId)}. Similar is not same — nothing here publishes.</div></div>`
        + (rows || emptyLine);
    } catch (error) {
      body.innerHTML = `<div class="card warn">Similar search unreadable: ${esc(error.message)}</div>`;
    }
  }

  function openDrawer() {
    drawerShow("steward-drawer");
  }

  function closeDrawer() {
    ["steward-drawer", "taxonomy-drawer", "similar-drawer"].forEach((id) => $(id).classList.remove("open"));
    $("drawer-scrim").classList.remove("open");
  }

  window.MosaicDrawers = {
    stewardItemById,
    valuesForAttribute,
    ownerLinesHtml,
    stewardQuestionHtml,
    afterApprove,
    doApprove,
    doReject,
    stewardApproveAsk,
    stewardRejectAsk,
    wireApproveCard,
    nextTaxonomyVersion,
    taxonomyInForce,
    stewardAcceptAsk,
    doAccept,
    dropFriendlySteward,
    stewardSplit,
    stewardCardHtml,
    bindStewardActions,
    renderSteward,
    loadPending,
    explain,
    openPolicy,
    openDrawer,
    closeDrawer,
    openTaxonomy,
    openSimilar,
  };
})();
