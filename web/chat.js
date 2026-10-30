/* Live Multi-Agent Protocol Chat (v2.3, Section 4.15).
 *
 * Deterministic transcript only: every bubble is rendered from real API
 * responses or ledger rows passed in by app.js — there is no free-form LLM
 * anywhere in this file. Roles: downstream (SYNTHETIC), proposer
 * (REAL · CLAIM, NOT A FACT), engine (REAL · DETERMINISTIC DATALOG),
 * steward (EXCEPTION ONLY). App code owns all behavior; this module renders
 * bubbles, verdict pills and action buttons, and wires drawer-backed actions
 * through MosaicDrawers with the caller-supplied context.
 */
(function () {
  "use strict";

  const ROLE_META = {
    downstream: { icon: "🤖", label: "Downstream Agent", badge: "SYNTHETIC", cls: "chat-downstream" },
    proposer: { icon: "🧠", label: "Muse Spark + Matching Engine", badge: "REAL · CLAIM, NOT A FACT", cls: "chat-proposer" },
    engine: { icon: "⚖️", label: "Trust Engine (manglekit)", badge: "REAL · DETERMINISTIC DATALOG", cls: "chat-engine" },
    steward: { icon: "👤", label: "Human Steward (steward-001)", badge: "EXCEPTION ONLY", cls: "chat-steward" },
  };

  function escHtml(s) {
    return window.MosaicConfig.esc(s);
  }

  function streamEl() {
    return document.getElementById("f-chat-stream");
  }

  function clear() {
    const stream = streamEl();
    if (stream) stream.innerHTML = "";
  }

  function bubbleHtml({ role, title, stamp, lines, actions, tone, policyLine, extraClass }) {
    const meta = ROLE_META[role] || ROLE_META.downstream;
    const extra = tone === "escalated" ? " escalated" : tone === "failed" ? " failed" : "";
    const body = (lines || []).map((line) => `<li>${line}</li>`).join("");
    const buttons = (actions || []).map((a) =>
      `<button type="button" data-chat-act="${escHtml(a.act)}"${a.arg ? ` data-chat-arg="${escHtml(a.arg)}"` : ""}${a.arg2 ? ` data-chat-arg2="${escHtml(a.arg2)}"` : ""}${a.cls ? ` class="${escHtml(a.cls)}"` : ""}>${escHtml(a.label)}</button>`
    ).join("");
    return `<div class="chat-bubble ${meta.cls}${extra}${extraClass ? " " + escHtml(extraClass) : ""}" data-chat-role="${role}">`
      + `<div class="chat-head">${meta.icon} ${escHtml(title || meta.label)} · <span class="chat-badge">${escHtml(meta.badge)}</span>`
      + (stamp ? ` <span class="mono muted">${escHtml(stamp)}</span>` : "") + `</div>`
      + (body ? `<ul>${body}</ul>` : "")
      + (policyLine ? `<div class="meta mono">${escHtml(policyLine)}</div>` : "")
      + (buttons ? `<div class="chat-actions">${buttons}</div>` : "")
      + `</div>`;
  }

  function append(html) {
    const stream = streamEl();
    if (!stream) return null;
    const wrap = document.createElement("div");
    wrap.innerHTML = html;
    const node = wrap.firstElementChild;
    if (node) {
      stream.appendChild(node);
      node.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    return node;
  }

  function add(opts) {
    return append(bubbleHtml(opts));
  }

  function setScenarioActive(id) {
    document.querySelectorAll("#f-intake-samples [data-act]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-act") === id);
    });
  }

  window.MosaicChat = {
    ROLE_META,
    clear,
    bubbleHtml,
    append,
    add,
    setScenarioActive,
  };
})();
