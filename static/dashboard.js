/* Dashboard front-end — fetches /api/dashboard, renders Chart.js timelines. */

const COLORS = {
  navy:   "#07182D",
  cyan:   "#02C8FF",
  blue:   "#0A60FF",
  pink:   "#FF007F",
  orange: "#FF9000",
  green:  "#45991F",
  red:    "#EB4651",
  gray500:"#6B6B6B",
  gray100:"#D6D6D6",
  // Dark theme axis/grid colors
  axisText: "#95A6BD",
  gridLine: "rgba(149,166,189,0.15)",
};

const charts = {};      // org-level widgets
const rowCharts = {};   // per-test row charts (re-created on each refresh)
// Cache of last data rendered per table container — used by the
// "Export to Excel" button on each card.
const lastTableData = {};

function _readIntFrom(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const raw = (el.value !== undefined ? el.value : el.textContent) || "";
  const n = parseInt(String(raw).trim(), 10);
  return Number.isFinite(n) ? n : fallback;
}

let currentInterval = _readIntFrom("interval-select", _readIntFrom("meta-interval", 5));
let currentRangeHours = _readIntFrom("range-select", 8);
let currentOrgKey = (() => {
  const el = document.getElementById("switch-org-select");
  return (el && el.dataset && el.dataset.active) || "";
})();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function rgba(hex, alpha) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function makeFilledLine(canvas, labels, data, color, opts = {}) {
  const ctx = canvas.getContext("2d");
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [{
        data: data,
        borderColor: color,
        backgroundColor: rgba(color, 0.22),
        fill: true,
        tension: 0.25,
        borderWidth: 2,
        pointRadius: opts.pointRadius ?? 2,
        pointBackgroundColor: color,
        spanGaps: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { ticks: { color: COLORS.axisText, maxRotation: 0, autoSkip: true,
                       autoSkipPadding: 12 },
             grid: { display: false } },
        y: { ticks: { color: COLORS.axisText, precision: 0 }, beginAtZero: true,
             grid: { color: COLORS.gridLine },
             min: opts.yMin, max: opts.yMax },
      },
    },
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;",
  }[c]));
}

function testLink(testId) {
  return `https://app.thousandeyes.com/network-app-synthetics/views/?testId=${encodeURIComponent(testId)}`;
}

// Format the TE test ``interval`` (seconds) as a compact human label, e.g.
// 60 -> "1 min", 120 -> "2 min", 300 -> "5 min", 3600 -> "1 h".
function formatInterval(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n < 60) return `${n}s`;
  if (n % 3600 === 0) {
    const h = n / 3600;
    return `${h} h`;
  }
  const m = Math.round(n / 60);
  return `${m} min`;
}

// ---------------------------------------------------------------------------
// Org-level widgets
// ---------------------------------------------------------------------------
function renderWidgets(d) {
  const labels = d.labels || [];
  const cfgs = [
    ["w1", d.widget1, COLORS.red,    "#w1-now"],
    ["w2", d.widget2, COLORS.blue,   "#w2-now"],
    ["w3", d.widget3, COLORS.orange, "#w3-now"],
    ["w4", d.widget4, COLORS.cyan,   "#w4-now"],
  ];
  for (const [id, series, color, sel] of cfgs) {
    const canvas = document.getElementById(id);
    if (charts[id]) charts[id].destroy();
    charts[id] = makeFilledLine(canvas, labels, series, color);
    // Walk backwards to find the last numeric value in the series so the
    // "now" tile shows the most recent real measurement instead of a
    // trailing 0/null gap (averages are null for blocks with no samples).
    let v = null;
    if (Array.isArray(series)) {
      for (let i = series.length - 1; i >= 0; i--) {
        if (typeof series[i] === "number" && Number.isFinite(series[i])) {
          v = series[i];
          break;
        }
      }
    }
    document.querySelector(sel).textContent =
      (v === null) ? "—" : (Math.round(v * 100) / 100);
  }

  // KPI strip — use the latest block that actually has data so the "now"
  // numbers reflect the last known value when test results are older than
  // a single block (e.g. >15 minutes).
  const lastNonEmpty = (arr) => {
    if (!arr || !arr.length) return 0;
    for (let i = arr.length - 1; i >= 0; i--) {
      const v = arr[i];
      if (v !== null && v !== undefined && !Number.isNaN(v) && v !== 0) {
        return v;
      }
    }
    // No non-zero values — fall back to the last numeric (could be 0).
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i] !== null && arr[i] !== undefined) return arr[i];
    }
    return 0;
  };
  document.getElementById("kpi-tx-err").textContent  = lastNonEmpty(d.widget1) ?? 0;
  document.getElementById("kpi-tx-time").textContent = (lastNonEmpty(d.widget2) ?? 0).toFixed(2);
  document.getElementById("kpi-pl-err").textContent  = lastNonEmpty(d.widget3) ?? 0;
  document.getElementById("kpi-pl-time").textContent = (lastNonEmpty(d.widget4) ?? 0).toFixed(2);
}

// ---------------------------------------------------------------------------
// Test row tables
// ---------------------------------------------------------------------------
function renderTable(containerId, tests, labels, mode) {
  const container = document.getElementById(containerId);
  // Destroy old per-row charts in this container.
  if (rowCharts[containerId]) {
    rowCharts[containerId].forEach((c) => c.destroy());
  }
  rowCharts[containerId] = [];

  if (!tests || tests.length === 0) {
    const msg = (mode === "no-data")
      ? "No tests with data-collection problems (all tests have rounds within the last hour)."
      : (mode === "availability")
        ? "No tests with errors in the last hour."
        : "No tests in this category.";
    container.innerHTML = `<div class="empty-state">${msg}</div>`;
    return;
  }

  const rows = tests.map((t, idx) => {
    const m = t.meta || {};
    const cid = `${containerId}-row-${idx}`;
    const inErr = t.in_error_now;
    // ``no-data`` rows describe a data-collection problem (no rounds in
    // the last hour). They get their own pill/right-column treatment.
    const noData = mode === "no-data";
    let pill;
    if (noData) {
      pill = `<span class="error-pill">No data &gt; 1h</span>`;
    } else if (inErr) {
      pill = `<span class="error-pill">Errors in last 1h</span>`;
    } else {
      pill = `<span class="ok-pill">OK</span>`;
    }
    const lastVal = (mode === "availability")
      ? (t.availability && t.availability.length ? t.availability[t.availability.length - 1] : null)
      : (t.latest_avg ?? null);
    const valText = (mode === "availability")
      ? (lastVal == null ? "—" : `${lastVal}% avail`)
      : (lastVal == null ? "—" : `${lastVal.toFixed(2)} s`);
    const safeName = escapeHtml(m.testName || m.testId || "");
    const intervalText = formatInterval(m.interval);
    let rightLabel, rightValue;
    if (noData) {
      rightLabel = "Last Data Age";
      rightValue = `${escapeHtml(t.last_data_age || "—")} &nbsp; ${pill}`;
    } else if (mode === "availability") {
      rightLabel = "Total Errors in last hour";
      const errCount = (typeof t.errors_last_hour === "number")
        ? t.errors_last_hour : 0;
      rightValue = `${errCount} &nbsp; ${pill}`;
    } else {
      rightLabel = "Latest Avg";
      rightValue = `${valText} &nbsp; ${pill}`;
    }
    // "Test Health" cell — % of rounds in the user-selected time range
    // where the test was NOT outstanding. Only shown on outstanding
    // tables (availability mode).
    let healthCell = "";
    let chartColspan = 4;
    if (mode === "availability") {
      const hv = t.test_health;
      let healthText, healthClass;
      if (typeof hv !== "number") {
        healthText = "—";
        healthClass = "";
      } else {
        healthText = `${hv.toFixed(1)}%`;
        if (hv >= 95) healthClass = "health-ok";
        else if (hv >= 80) healthClass = "health-warn";
        else healthClass = "health-bad";
      }
      const rounds = (typeof t.rounds_in_range === "number")
        ? t.rounds_in_range : 0;
      const errs = (typeof t.errors_in_range === "number")
        ? t.errors_in_range : 0;
      const title = `Healthy rounds ${rounds - errs} of ${rounds} in selected range`;
      healthCell = `
          <td style="width:12%">
            <span class="label">Test Health</span>
            <span class="${healthClass}" title="${escapeHtml(title)}">${healthText}</span>
          </td>`;
      chartColspan = 5;
    }
    return `
      <table class="test-table">
        <tr class="header-row">
          <td class="ignore-col" rowspan="2">
            <button type="button" class="btn btn-sm btn-ignore-row"
                    data-test-id="${escapeHtml(m.testId || "")}"
                    data-test-name="${safeName}"
                    title="Ignore this test from all metrics">Ignore</button>
          </td>
          <td style="width:${mode === "availability" ? "18" : "22"}%">
            <span class="label">Account Group</span>
            ${escapeHtml(m.accountGroupName || "—")}
          </td>
          <td style="width:${mode === "availability" ? "36" : "42"}%">
            <span class="label">Test Name</span>
            <a href="${testLink(m.testId)}" target="_blank" rel="noopener">${safeName}</a>
          </td>
          <td style="width:12%">
            <span class="label">Test Interval</span>
            ${escapeHtml(intervalText)}
          </td>${healthCell}
          <td style="width:${mode === "availability" ? "22" : "22"}%">
            <span class="label">${rightLabel}</span>
            ${rightValue}
          </td>
        </tr>
        <tr class="chart-row">
          <td colspan="${chartColspan}"><canvas id="${cid}"></canvas></td>
        </tr>
      </table>`;
  }).join("");
  container.innerHTML = rows;

  // Now build the charts.
  tests.forEach((t, idx) => {
    const cid = `${containerId}-row-${idx}`;
    const canvas = document.getElementById(cid);
    if (!canvas) return;
    if (mode === "availability" || mode === "no-data") {
      const series = (t.availability || []).map((v) => v == null ? null : v);
      const chart = makeFilledLine(canvas, labels, series,
                                   mode === "no-data" ? COLORS.orange : COLORS.green,
                                   { yMin: 0, yMax: 100, pointRadius: 2 });
      rowCharts[containerId].push(chart);
    } else {
      const chart = makeFilledLine(canvas, labels, t.avg_time || [], COLORS.blue,
                                   { pointRadius: 2 });
      rowCharts[containerId].push(chart);
    }
  });

  bindIgnoreButtons(container);
  lastTableData[containerId] = { tests, labels, mode };
}

// ---------------------------------------------------------------------------
// Export to Excel — builds an HTML-formatted .xls file that Excel opens
// natively. Hyperlinks on the Test Name cell are preserved.
// ---------------------------------------------------------------------------
function _excelEscape(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function exportTableToExcel(containerId) {
  const cached = lastTableData[containerId];
  if (!cached || !cached.tests || cached.tests.length === 0) {
    window.alert("Nothing to export — this table has no rows.");
    return;
  }
  const { tests, mode } = cached;

  // Pick column set per table mode.
  let headers, rowFn;
  if (mode === "availability") {
    headers = [
      "Account Group", "Test Name", "Test ID", "Test Link",
      "Test Interval", "Test Health (%)",
      "Rounds In Range", "Errors In Range",
      "Total Errors In Last Hour", "In Error Now",
    ];
    rowFn = (t) => {
      const m = t.meta || {};
      const link = m.testId ? testLink(m.testId) : "";
      const linkedName = link
        ? `<a href="${_excelEscape(link)}">${_excelEscape(m.testName || m.testId || "")}</a>`
        : _excelEscape(m.testName || m.testId || "");
      return [
        _excelEscape(m.accountGroupName || ""),
        linkedName,
        _excelEscape(m.testId || ""),
        _excelEscape(link),
        _excelEscape(formatInterval(m.interval)),
        (typeof t.test_health === "number") ? t.test_health.toFixed(1) : "",
        (typeof t.rounds_in_range === "number") ? t.rounds_in_range : "",
        (typeof t.errors_in_range === "number") ? t.errors_in_range : "",
        (typeof t.errors_last_hour === "number") ? t.errors_last_hour : "",
        t.in_error_now ? "Yes" : "No",
      ];
    };
  } else if (mode === "no-data") {
    headers = [
      "Account Group", "Test Name", "Test ID", "Test Link",
      "Test Interval", "Last Data Age", "Last Data Age (seconds)",
    ];
    rowFn = (t) => {
      const m = t.meta || {};
      const link = m.testId ? testLink(m.testId) : "";
      const linkedName = link
        ? `<a href="${_excelEscape(link)}">${_excelEscape(m.testName || m.testId || "")}</a>`
        : _excelEscape(m.testName || m.testId || "");
      return [
        _excelEscape(m.accountGroupName || ""),
        linkedName,
        _excelEscape(m.testId || ""),
        _excelEscape(link),
        _excelEscape(formatInterval(m.interval)),
        _excelEscape(t.last_data_age || ""),
        (typeof t.last_data_age_seconds === "number")
          ? Math.round(t.last_data_age_seconds) : "",
      ];
    };
  } else {
    // "time" mode — Top 5 tables
    headers = [
      "Account Group", "Test Name", "Test ID", "Test Link",
      "Test Interval", "Latest Avg (s)",
    ];
    rowFn = (t) => {
      const m = t.meta || {};
      const link = m.testId ? testLink(m.testId) : "";
      const linkedName = link
        ? `<a href="${_excelEscape(link)}">${_excelEscape(m.testName || m.testId || "")}</a>`
        : _excelEscape(m.testName || m.testId || "");
      return [
        _excelEscape(m.accountGroupName || ""),
        linkedName,
        _excelEscape(m.testId || ""),
        _excelEscape(link),
        _excelEscape(formatInterval(m.interval)),
        (typeof t.latest_avg === "number") ? t.latest_avg.toFixed(3) : "",
      ];
    };
  }

  const thead = `<tr>${headers.map((h) => `<th>${_excelEscape(h)}</th>`).join("")}</tr>`;
  const tbody = tests.map((t) => {
    const cells = rowFn(t);
    return `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`;
  }).join("");

  const titleEl = document.querySelector(
    `[data-export-target="${containerId}"]`
  );
  const card = titleEl ? titleEl.closest(".card") : null;
  const sheetTitle = (card && card.querySelector("h2"))
    ? card.querySelector("h2").textContent.trim()
    : containerId;

  const html =
    `<html xmlns:o="urn:schemas-microsoft-com:office:office" ` +
    `xmlns:x="urn:schemas-microsoft-com:office:excel" ` +
    `xmlns="http://www.w3.org/TR/REC-html40">` +
    `<head><meta charset="utf-8"></head><body>` +
    `<table border="1"><thead>${thead}</thead><tbody>${tbody}</tbody></table>` +
    `</body></html>`;

  const blob = new Blob(["\ufeff", html], {
    type: "application/vnd.ms-excel;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const safeTitle = sheetTitle.replace(/[^\w\-]+/g, "_").slice(0, 60);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safeTitle}_${ts}.xls`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Delegated click handler — works even when buttons existed at page load.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-export[data-export-target]");
  if (!btn) return;
  exportTableToExcel(btn.dataset.exportTarget);
});

// ---------------------------------------------------------------------------
// Per-row Ignore button (with confirmation)
// ---------------------------------------------------------------------------
function bindIgnoreButtons(root) {
  root.querySelectorAll(".btn-ignore-row").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const testId   = btn.dataset.testId || "";
      const testName = btn.dataset.testName || testId;
      if (!testId) return;
      const ok = window.confirm(
        `Ignore test "${testName}"?\n\n` +
        `Metrics from this test will NOT be considered in the executive ` +
        `summary, widgets or tables until you re-include it from the ` +
        `"Ignored Tests" section at the bottom of the dashboard.`
      );
      if (!ok) return;
      btn.disabled = true;
      btn.classList.add("btn-disabled");
      btn.textContent = "Ignoring…";
      try {
        const res = await actionFetch("/api/ignored/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ orgKey: currentOrgKey, testIds: [testId] }),
        });
        if (res.ok) {
          tick();
        } else {
          btn.disabled = false;
          btn.classList.remove("btn-disabled");
          btn.textContent = "Ignore";
        }
      } catch (e) {
        btn.disabled = false;
        btn.classList.remove("btn-disabled");
        btn.textContent = "Ignore";
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Ignored Tests table (Re-Include)
// ---------------------------------------------------------------------------
function refreshReincludeButton() {
  const btn = document.getElementById("reinclude-btn");
  if (!btn) return;
  const root = document.getElementById("ignored-list");
  const any = root && root.querySelectorAll(".reinclude-cb:checked").length > 0;
  if (any) {
    btn.disabled = false;
    btn.classList.remove("btn-disabled");
    btn.classList.add("btn-green");
  } else {
    btn.disabled = true;
    btn.classList.add("btn-disabled");
    btn.classList.remove("btn-green");
  }
}

function renderIgnoredList(items) {
  const root = document.getElementById("ignored-list");
  if (!root) return;
  if (!items || items.length === 0) {
    root.innerHTML = `<div class="empty-state">No ignored tests in this organization.</div>`;
    refreshReincludeButton();
    return;
  }
  const rows = items.map((it) => `
    <tr>
      <td class="ck-col">
        <input type="checkbox" class="reinclude-cb"
               data-test-id="${escapeHtml(it.testId || "")}">
      </td>
      <td>${escapeHtml(it.accountGroupName || "—")}</td>
      <td>
        <a href="${testLink(it.testId)}" target="_blank" rel="noopener">
          ${escapeHtml(it.testName || it.testId)}
        </a>
      </td>
      <td>${escapeHtml(it.type || "—")}</td>
    </tr>`).join("");
  root.innerHTML = `
    <table class="test-table ignored-table">
      <thead>
        <tr>
          <th class="ck-col"></th>
          <th>Account Group Name</th>
          <th>Test Name</th>
          <th>Test Type</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  root.querySelectorAll(".reinclude-cb").forEach((cb) => {
    cb.addEventListener("change", refreshReincludeButton);
  });
  refreshReincludeButton();
}

function bindReincludeButton() {
  const btn = document.getElementById("reinclude-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const root = document.getElementById("ignored-list");
    const ids = Array.from(root.querySelectorAll(".reinclude-cb:checked"))
      .map((cb) => cb.dataset.testId).filter(Boolean);
    if (!ids.length) return;
    btn.disabled = true;
    try {
      const res = await actionFetch("/api/ignored/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orgKey: currentOrgKey, testIds: ids }),
      });
      if (res.ok) tick();
    } catch (e) { /* keep UI as-is */ }
  });
}

// ---------------------------------------------------------------------------
// Status / interval bar
// ---------------------------------------------------------------------------
function setStatus(payload) {
  const el = document.getElementById("status");
  const cycleRunning = !!payload.cycle_in_progress;
  const hasFinished = !!payload.last_cycle_finished;
  if (cycleRunning && !hasFinished) {
    // First-ever cycle still running and no previous results to fall back on.
    el.textContent = "Getting data…";
    el.className = "status-pill warn";
  } else if (cycleRunning) {
    // A cycle is running but a previous cycle already produced data.
    el.textContent = "Refreshing…";
    el.className = "status-pill warn";
  } else if (payload.last_error) {
    el.textContent = `Error: ${payload.last_error}`;
    el.className = "status-pill err";
  } else {
    const t = payload.last_cycle_finished
      ? new Date(payload.last_cycle_finished).toLocaleTimeString()
      : "now";
    el.textContent = `Last update: ${t}`;
    el.className = "status-pill ok";
  }

  // Loop duration legend
  const dur = document.getElementById("loop-duration");
  if (dur) {
    if (payload.last_cycle_duration) {
      dur.innerHTML = `Last loop: <strong>${payload.last_cycle_duration}</strong>`;
    } else {
      dur.textContent = "Last loop: —";
    }
  }

  // Period header
  const period = document.getElementById("period");
  if (payload.last_cycle_finished) {
    period.textContent = `Last cycle: ${new Date(payload.last_cycle_finished).toLocaleString()}`;
  } else {
    period.textContent = "Initializing…";
  }
}

function bindIntervalControls() {
  const select = document.getElementById("interval-select");
  const button = document.getElementById("change-interval");

  function refreshButton() {
    const val = parseInt(select.value, 10);
    if (val === currentInterval) {
      button.disabled = true;
      button.classList.add("btn-disabled");
      button.classList.remove("btn-cyan");
    } else {
      button.disabled = false;
      button.classList.remove("btn-disabled");
      button.classList.add("btn-cyan");
    }
  }
  select.addEventListener("change", refreshButton);
  refreshButton();

  button.addEventListener("click", async () => {
    const val = parseInt(select.value, 10);
    button.disabled = true;
    try {
      const res = await actionFetch("/api/interval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ minutes: val }),
      });
      if (res.ok) {
        currentInterval = val;
        const meta = document.getElementById("meta-interval");
        if (meta) meta.textContent = String(val);
        refreshButton();
      }
    } catch (e) { /* keep current */ }
  });
}

function bindRangeControls() {
  const select = document.getElementById("range-select");
  const button = document.getElementById("change-range");
  if (!select || !button) return;

  function refreshButton() {
    const val = parseInt(select.value, 10);
    if (val === currentRangeHours) {
      button.disabled = true;
      button.classList.add("btn-disabled");
      button.classList.remove("btn-cyan");
    } else {
      button.disabled = false;
      button.classList.remove("btn-disabled");
      button.classList.add("btn-cyan");
    }
  }
  select.addEventListener("change", refreshButton);
  refreshButton();

  button.addEventListener("click", async () => {
    const val = parseInt(select.value, 10);
    button.disabled = true;
    try {
      const res = await actionFetch("/api/range", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hours: val }),
      });
      if (res.ok) {
        currentRangeHours = val;
        refreshButton();
        tick();
      }
    } catch (e) { /* keep current */ }
  });
}

function bindSwitchOrgControls() {
  const select = document.getElementById("switch-org-select");
  const button = document.getElementById("switch-org");
  if (!select || !button) return;

  function refreshButton() {
    if (select.value === currentOrgKey) {
      button.disabled = true;
      button.classList.add("btn-disabled");
      button.classList.remove("btn-cyan");
    } else {
      button.disabled = false;
      button.classList.remove("btn-disabled");
      button.classList.add("btn-cyan");
    }
  }
  select.addEventListener("change", refreshButton);
  refreshButton();

  button.addEventListener("click", async () => {
    const newKey = select.value;
    button.disabled = true;
    try {
      const res = await actionFetch("/api/active-org", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orgKey: newKey }),
      });
      if (res.ok) {
        currentOrgKey = newKey;
        select.dataset.active = newKey;
        const opt = select.options[select.selectedIndex];
        const orgEl = document.getElementById("meta-org");
        if (opt && orgEl) orgEl.textContent = opt.textContent.trim();
        // Clear UI and re-fetch immediately for the new org.
        document.getElementById("loader").style.display = "block";
        document.getElementById("content").style.display = "none";
        refreshButton();
        tick();
      }
    } catch (e) { /* keep current */ }
  });
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
function setBackendDown(down) {
  const banner = document.getElementById("server-down-banner");
  if (!banner) return;
  banner.hidden = !down;
}

function _formatAge(seconds) {
  if (!Number.isFinite(seconds)) return "older than 15 minutes";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} seconds old`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"} old`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m old` : `${h}h old`;
}

function setStaleData(stale, ageSeconds, cycleInProgress) {
  const banner = document.getElementById("stale-data-banner");
  if (!banner) return;
  banner.hidden = !stale;
  if (!stale) return;
  const ageEl = document.getElementById("stale-data-age");
  if (ageEl) ageEl.textContent = _formatAge(ageSeconds);
  const suffixEl = document.getElementById("stale-data-suffix");
  if (suffixEl) {
    suffixEl.textContent = cycleInProgress
      ? "The script is currently gathering the latest test results."
      : "Waiting for the next data-gathering cycle.";
  }
}

// Wrapper for action endpoints (POST). On 401 the backend has restarted and
// lost the in-memory tokens — bounce the user to the login page so they can
// re-authenticate instead of silently failing.
async function actionFetch(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    // Server is unreachable (process killed). Show the red banner.
    setBackendDown(true);
    throw e;
  }
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthenticated");
  }
  return res;
}

async function tick() {
  try {
    const url = currentOrgKey
      ? `/api/dashboard?org=${encodeURIComponent(currentOrgKey)}`
      : "/api/dashboard";
    const res = await fetch(url, { cache: "no-store" });
    if (res.status === 401) {
      // Logged out / session expired — bounce to login.
      window.location.href = "/login";
      return;
    }
    if (!res.ok) {
      // Server is up but errored (e.g. 500). Surface the banner so the
      // user knows the dashboard data may be stale.
      setBackendDown(true);
      return;
    }
    setBackendDown(false);
    const payload = await res.json();
    setStatus(payload);

    // Stale-data banner: show whenever the newest cached result is older
    // than 15 minutes, regardless of whether a fetch cycle is in progress.
    const ageSec = (payload.data && payload.data.data_age_seconds)
      ?? payload.data_age_seconds;
    const isStale = (typeof ageSec === "number") && (ageSec > 15 * 60);
    setStaleData(isStale, ageSec, payload.cycle_in_progress);

    // Render whenever we have ANY data payload, even if the server hasn't
    // flipped `ready=true` yet. This guarantees the dashboard never sits
    // on "Getting data…" while real (possibly old) test results exist.
    const d = (payload && payload.data) || {};
    const hasAnyData =
      (Array.isArray(d.labels) && d.labels.length > 0) ||
      (d.totals && (d.totals.tx_tests || d.totals.pl_tests)) ||
      (Array.isArray(d.tx_outstanding) && d.tx_outstanding.length > 0) ||
      (Array.isArray(d.pl_outstanding) && d.pl_outstanding.length > 0) ||
      (Array.isArray(d.tx_no_data) && d.tx_no_data.length > 0) ||
      (Array.isArray(d.pl_no_data) && d.pl_no_data.length > 0);

    if (payload.ready || hasAnyData) {
      // Hide the loader as soon as we have something to show.
      document.getElementById("loader").style.display = "none";
      document.getElementById("content").style.display = "block";

      renderWidgets(d);

      const totals = d.totals || {};
      document.getElementById("exec-tx-total").textContent = totals.tx_tests ?? "—";
      const txErrEl = document.getElementById("exec-tx-err");
      const txErr = totals.tx_in_error ?? 0;
      txErrEl.textContent = txErr;
      txErrEl.style.color = (txErr > 0) ? COLORS.red : COLORS.green;
      document.getElementById("exec-pl-total").textContent = totals.pl_tests ?? "—";
      const plErrEl = document.getElementById("exec-pl-err");
      const plErr = totals.pl_in_error ?? 0;
      plErrEl.textContent = plErr;
      plErrEl.style.color = (plErr > 0) ? COLORS.red : COLORS.green;

      renderTable("tx-outstanding", d.tx_outstanding, d.labels, "availability");
      renderTable("tx-no-data",     d.tx_no_data,     d.labels, "no-data");
      renderTable("tx-top5",        d.tx_top5,        d.labels, "time");
      renderTable("pl-outstanding", d.pl_outstanding, d.labels, "availability");
      renderTable("pl-no-data",     d.pl_no_data,     d.labels, "no-data");
      renderTable("pl-top5",        d.pl_top5,        d.labels, "time");

      // Ignored tests card + bottom table
      const ignoredCount = (totals.ignored ?? (d.ignored ? d.ignored.length : 0)) || 0;
      const ignoredEl = document.getElementById("ignored-count");
      if (ignoredEl) ignoredEl.textContent = ignoredCount;
      renderIgnoredList(d.ignored || []);
    }
  } catch (e) {
    // fetch() throws TypeError when the server is unreachable (process
    // killed / port closed). Show the red banner so the user knows the
    // backend is down and they need to restart `python ./app.py`.
    setBackendDown(true);
  }
}

bindIntervalControls();
bindRangeControls();
bindSwitchOrgControls();
bindReincludeButton();
tick();
const pollHandle = setInterval(tick, 10000);   // poll the API every 10s — cheap UI refresh
