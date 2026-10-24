/* The board: render a snapshot, and re-render when the conductor says so.
 *
 * No framework, no build step. The whole state is one snapshot object from
 * `/api/snapshot`, replaced wholesale whenever `/api/stream` pushes a new one,
 * so there is no local state to drift out of sync with the desk.
 *
 * It is deliberately read-only. Every way of moving a case goes through the
 * chat room, because that is where the other participants are and where the
 * audit trail lives. A button here would be a decision taken out of sight.
 */

const COLUMNS = {
  triaging:        "Triage",
  needs_info:      "Needs info",
  accepted:        "Accepted",
  in_progress:     "In progress",
  awaiting_review: "Review",
  done:            "Closed",
};

const el = (id) => document.getElementById(id);

const state = {
  snapshot: null,
  filter: "",
  selected: null,
};

/* ── Formatting ────────────────────────────────────────────────────────── */

function duration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 90) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 90) return `${m}m`;
  const h = m / 60;
  if (h < 48) return `${trim(h)}h`;
  return `${trim(h / 24)}d`;
}

const trim = (n) => (n % 1 < 0.05 ? String(Math.round(n)) : n.toFixed(1));

/* The clock has to agree with the colour. A case can breach its acknowledgement
 * promise long before its resolve budget runs down -- a P3 owes an answer in
 * 4h but has 48h to be finished -- and showing "42h left" on a row drawn in red
 * is how a board loses people's trust. Whichever promise is broken is the one
 * reported. */
function clockOf(c) {
  if (c.state === "done" || c.state === "rejected") return duration(c.elapsed_ms);
  if (c.ack_overdue_by_ms > 0) return `+${duration(c.ack_overdue_by_ms)}`;
  if (c.resolve_overdue_by_ms > 0) return `+${duration(c.resolve_overdue_by_ms)} over`;
  const left = c.sla.resolve_minutes * 60000 - c.elapsed_ms;
  return `${duration(Math.max(0, left))} left`;
}

function when(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* Everything from the server is treated as text, never markup. A case title
 * is whatever someone typed in a chat channel. */
const node = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/* ── Render ────────────────────────────────────────────────────────────── */

function render() {
  const snap = state.snapshot;
  if (!snap) return;

  el("desk-name").textContent = snap.desk.room_name
    ? `#${snap.desk.room_name} · @${snap.desk.agent_name} · ${snap.desk.rooms} rooms`
    : `@${snap.desk.agent_name} · no desk room found`;

  const t = snap.totals;
  figure("s-open", t.open);
  figure("s-breached", t.breached);
  figure("s-risk", t.at_risk);
  figure("s-closed", t.closed);
  el("s-open-foot").textContent = t.open === 1 ? "case on the desk" : "cases on the desk";
  el("s-median").textContent = t.median_close_ms
    ? `median ${duration(t.median_close_ms)} to close`
    : "nothing closed yet";

  const needle = state.filter.trim().toLowerCase();
  const matches = (c) =>
    !needle ||
    c.id.toLowerCase().includes(needle) ||
    c.title.toLowerCase().includes(needle) ||
    (c.owner || "").toLowerCase().includes(needle) ||
    (c.requester || "").toLowerCase().includes(needle);

  const pipeline = el("pipeline");
  pipeline.textContent = "";

  for (const group of snap.pipeline) {
    const shown = group.cases.filter(matches);
    const column = node("section", "column");
    column.dataset.state = group.state;

    const head = node("div", "column-head");
    head.append(
      node("span", "name", COLUMNS[group.state] || group.state),
      node("span", "count", String(shown.length)),
    );

    const body = node("div", "column-body");
    if (!shown.length) {
      body.append(node("div", "empty", needle ? "nothing matching" : "empty"));
    } else {
      /* Closed is unbounded over a long-running desk, so it is capped. The
       * other columns are bounded by the work actually in flight. */
      const slice = group.state === "done" ? shown.slice(-14).reverse() : shown;
      for (const c of slice) body.append(caseCard(c));
    }

    column.append(head, body);
    pipeline.append(column);
  }

  el("stamp").textContent = `updated ${when(snap.now)}`;

  if (state.selected) {
    const live = snap.cases.find((c) => c.id === state.selected);
    if (live) drawCase(live);
    else closeDrawer();
  }
}

function figure(id, value) {
  const node = el(id);
  node.textContent = String(value);
  node.closest(".stat").dataset.zero = value === 0 ? "1" : "0";
}

function caseCard(c) {
  const card = node("button", "case");
  card.type = "button";
  card.dataset.health = c.health;
  card.dataset.prio = c.priority;
  card.dataset.closed = c.state === "done" || c.state === "rejected" ? "1" : "0";
  card.setAttribute("aria-label", `${c.id}: ${c.title}`);

  const top = node("div", "case-top");
  top.append(
    node("span", "case-id", c.id),
    node("span", "prio", c.priority.toUpperCase()),
    node("span", "spacer"),
    node("span", "case-clock", clockOf(c)),
  );

  const meta = node("div", "case-meta");
  meta.append(node("span", "who", c.owner || "unassigned"));
  if (c.requester) meta.append(node("span", null, `· from ${c.requester}`));

  card.append(top, node("p", "case-title", c.title || "(no title)"), meta);

  if (c.state !== "done" && c.state !== "rejected") {
    const budget = c.sla.resolve_minutes * 60000;
    const pct = budget ? Math.min(100, (c.elapsed_ms / budget) * 100) : 0;
    const meter = node("div", "meter");
    const fill = node("i");
    fill.style.width = `${pct}%`;
    meter.append(fill);
    card.append(meter);
  }

  card.addEventListener("click", () => openDrawer(c.id));
  return card;
}

/* ── Drawer ────────────────────────────────────────────────────────────── */

function openDrawer(id) {
  state.selected = id;
  const c = state.snapshot?.cases.find((x) => x.id === id);
  if (c) drawCase(c);
  el("drawer").hidden = false;
  el("scrim").hidden = false;
  el("drawer-close").focus();
}

function closeDrawer() {
  state.selected = null;
  el("drawer").hidden = true;
  el("scrim").hidden = true;
}

function drawCase(c) {
  const body = el("drawer-body");
  body.textContent = "";

  body.append(node("div", "d-id", c.id), node("h2", "d-title", c.title || "(no title)"));

  const dl = node("dl", "d-grid");
  const row = (label, value, tone) => {
    dl.append(node("dt", null, label));
    dl.append(node("dd", tone || null, value));
  };
  row("State", c.state.replace(/_/g, " "));
  row("Priority", `${c.priority.toUpperCase()} — resolve within ${c.sla.resolve_minutes}m`);
  row("Owner", c.owner || "unassigned");
  row("Opened by", c.requester || "unknown");
  row("Age", duration(c.elapsed_ms) + (c.state === "needs_info" ? " (clock paused)" : ""));
  if (c.resolve_overdue_by_ms > 0) {
    row("Over SLA", `by ${duration(c.resolve_overdue_by_ms)}`, "bad");
  } else if (c.ack_overdue_by_ms > 0) {
    row("Unacknowledged", `${duration(c.ack_overdue_by_ms)} past the promise`, "bad");
  } else if (c.health === "at_risk") {
    row("SLA", "inside the last fifth of its budget", "warn");
  }
  if (c.stalled_for_ms > 600000) {
    row("Last moved", `${duration(c.stalled_for_ms)} ago`, "warn");
  }
  if (c.outcome) row("Outcome", c.outcome);
  body.append(dl);

  body.append(node("h3", "d-section", `Trail — ${c.log.length} entries`));
  const trail = node("ol", "trail");
  for (const t of c.log) {
    const li = node("li");
    const isMove = t.from !== t.to;
    li.dataset.kind = isMove ? "move" : "note";

    const head = node("div", "t-head");
    if (isMove) {
      head.append(
        node("span", null, t.from.replace(/_/g, " ")),
        node("span", "arrow", " → "),
        node("span", null, t.to.replace(/_/g, " ")),
      );
    } else {
      head.append(node("span", "arrow", "note"));
    }
    li.append(head);

    const sub = node("div", "t-when");
    sub.append(node("span", "t-actor", t.actor || "someone"), node("span", null, ` · ${when(t.at)}`));
    li.append(sub);

    if (t.note) li.append(node("div", "t-note", t.note));
    trail.append(li);
  }
  body.append(trail);

  if (c.state !== "done" && c.state !== "rejected") {
    const hint = node("div", "d-hint");
    hint.append(document.createTextNode("Cases move from the room, not from here."));
    hint.append(node("code", null, `@relay status ${c.id}`));
    hint.append(document.createTextNode("asks Relay where this one stands."));
    body.append(hint);
  }
}

/* ── Live ──────────────────────────────────────────────────────────────── */

function link(up) {
  const pill = el("link-state");
  pill.dataset.state = up ? "up" : "down";
  el("link-label").textContent = up ? "live" : "reconnecting";
}

function connect() {
  const source = new EventSource("/api/stream");

  source.addEventListener("open", () => link(true));
  source.addEventListener("message", (event) => {
    link(true);
    try {
      state.snapshot = JSON.parse(event.data);
      render();
    } catch (err) {
      console.error("[relay] undecodable snapshot", err);
    }
  });
  source.addEventListener("error", () => {
    link(false);
    /* EventSource reconnects on its own. Closing it here and opening a new
     * one would only add a second retry loop fighting the first. */
  });
}

/* The clocks on the board must keep moving between pushes, or a breach looks
 * frozen until the next transition. Re-rendering from the same snapshot with a
 * fresh `now` is enough. */
function retickLocally() {
  setInterval(() => {
    const snap = state.snapshot;
    if (!snap) return;
    const drift = Date.now() - snap.now;
    if (drift < 1000) return;
    for (const c of snap.cases) {
      if (c.state === "done" || c.state === "rejected" || c.state === "needs_info") continue;
      c.elapsed_ms += drift;
      c.stalled_for_ms += drift;
      const budget = c.sla.resolve_minutes * 60000;
      c.resolve_overdue_by_ms = Math.max(0, c.elapsed_ms - budget);
      if (c.resolve_overdue_by_ms > 0 || c.ack_overdue_by_ms > 0) c.health = "breached";
      else if (budget && c.elapsed_ms >= budget * 0.8) c.health = "at_risk";
    }
    snap.now = Date.now();
    render();
  }, 5000);
}

/* ── Chrome ────────────────────────────────────────────────────────────── */

function theme() {
  const saved = (() => {
    try { return localStorage.getItem("relay-theme"); } catch { return null; }
  })();
  const prefers = window.matchMedia("(prefers-color-scheme: dark)").matches;
  apply(saved || (prefers ? "dark" : "light"));

  el("theme").addEventListener("click", () => {
    apply(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });

  function apply(mode) {
    document.documentElement.dataset.theme = mode;
    try { localStorage.setItem("relay-theme", mode); } catch { /* private mode */ }
  }
}

function keys() {
  const filter = el("filter");
  filter.addEventListener("input", () => {
    state.filter = filter.value;
    render();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement !== filter) {
      event.preventDefault();
      filter.focus();
    } else if (event.key === "Escape") {
      if (state.selected) closeDrawer();
      else if (document.activeElement === filter) {
        filter.value = "";
        state.filter = "";
        filter.blur();
        render();
      }
    }
  });

  el("drawer-close").addEventListener("click", closeDrawer);
  el("scrim").addEventListener("click", closeDrawer);
}

theme();
keys();
connect();
retickLocally();
