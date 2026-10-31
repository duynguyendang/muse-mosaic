/* Live telemetry ticker (UX 2.5, Section 4.2 Step 2).
 *
 * Deterministic event stream only: every row is rendered from real API
 * responses or ledger rows passed in by app.js — there is no free-form LLM
 * anywhere in this file. Roles: downstream (SYNTHETIC), proposer
 * (REAL · CLAIM, NOT A FACT), engine (REAL · DETERMINISTIC DATALOG),
 * steward (EXCEPTION ONLY). Rows keep their data-chat-act buttons, which
 * app.js handles through a document-level delegated listener.
 */
(function () {
  "use strict";

  const ROLE_META = {
    downstream: { icon: "🤖", label: "Downstream Agent" },
    proposer: { icon: "🧠", label: "Muse Spark + Matching Engine" },
    engine: { icon: "⚖️", label: "Trust Engine (manglekit)" },
    steward: { icon: "⚑", label: "Human Steward (steward-001)" },
  };

  const MAX_ROWS = 40;

  function escHtml(s) {
    return window.MosaicConfig.esc(s);
  }

  function tickerEl() {
    return document.getElementById("f-telemetry");
  }

  function clear() {
    const ticker = tickerEl();
    if (ticker) {
      ticker.innerHTML = '<span class="muted">Telemetry idle — run a dataset to watch the agents.</span>';
    }
  }

  function rowHtml({ role, title, stamp, lines, actions, tone }) {
    const meta = ROLE_META[role] || ROLE_META.downstream;
    const toneCls = tone === "escalated" ? " tl-escalated" : tone === "failed" ? " tl-failed" : "";
    const text = (lines || []).map((line) => String(line).replace(/<[^>]*>/g, "").slice(0, 220)).join(" · ");
    const buttons = (actions || []).map((a) => {
      let attrs = ` data-chat-act="${escHtml(a.act)}"`;
      if (a.arg) attrs += ` data-chat-arg="${escHtml(a.arg)}"`;
      if (a.arg2) attrs += ` data-chat-arg2="${escHtml(a.arg2)}"`;
      const label = a.label.length > 22 ? a.label.slice(0, 21) + "…" : a.label;
      return `<button type="button"${attrs} title="${escHtml(a.label)}">${escHtml(label)}</button>`;
    }).join("");
    return `<div class="tl-row${toneCls}"${role ? ` data-chat-role="${role}"` : ""}>`
      + `<span class="tl-ico">${meta.icon}</span> <b>${escHtml(title || meta.label)}</b>`
      + (text ? ` <span class="tl-text">${escHtml(text)}</span>` : "")
      + (stamp ? ` <span class="mono muted">${escHtml(stamp)}</span>` : "")
      + (buttons ? ` <span class="tl-actions">${buttons}</span>` : "")
      + `</div>`;
  }

  function append(html) {
    const ticker = tickerEl();
    if (!ticker) return null;
    const idle = ticker.querySelector(".muted");
    if (idle && ticker.children.length === 1) ticker.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.innerHTML = html;
    const node = wrap.firstElementChild;
    if (node) {
      ticker.appendChild(node);
      while (ticker.children.length > MAX_ROWS) ticker.removeChild(ticker.firstChild);
      ticker.scrollTop = ticker.scrollHeight;
    }
    return node;
  }

  function add(opts) {
    return append(rowHtml(opts));
  }

  function setScenarioActive(id) {
    document.querySelectorAll("#f-intake-samples [data-act]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-act") === id);
    });
  }

  window.MosaicChat = {
    ROLE_META,
    clear,
    bubbleHtml: rowHtml,
    append,
    add,
    setScenarioActive,
  };
})();
