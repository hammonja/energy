import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import requests


DEFAULT_PRICE_URL = os.environ.get(
    "OCTOPUS_URL",
    "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/"
    "E-1R-AGILE-24-10-01-L/standard-unit-rates/",
)
API_BASE = "https://api.octopus.energy/v1"
DATA_FILE = Path(os.environ.get("OCTOPUS_DATA_FILE", "octopus_prices.json"))
APP_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.environ.get("OCTOPUS_DB_FILE", APP_DIR / "energy.db"))
CONFIG_FILE = Path(os.environ.get("OCTOPUS_CONFIG_FILE", "octopus_config.json"))
POLL_SECONDS = int(os.environ.get("OCTOPUS_POLL_SECONDS", "300"))
HOST = os.environ.get("OCTOPUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("OCTOPUS_PORT", "8080"))

DATA_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
COLLECTOR_STATE = {
    "last_ok": None,
    "last_error": None,
    "last_comparison_error": None,
    "last_consumption_error": None,
    "last_consumption_query": None,
    "last_attempt": None,
    "account_linked": False,
    "active_tariff": None,
}


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Octopus Electricity Price Tracker</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #202124;
      --muted: #5f6368;
      --line: #d7d9dd;
      --accent: #0078a8;
      --accent-2: #7252a3;
      --good: #20894d;
      --bad: #b3261e;
      --warn: #a75d00;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111315;
        --panel: #191c1f;
        --text: #f0f2f4;
        --muted: #a9b0b7;
        --line: #30363d;
        --accent: #50b4d8;
        --accent-2: #b798e2;
        --good: #79d18b;
        --bad: #ff8a80;
        --warn: #f4b860;
      }
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }

    header {
      display: grid;
      gap: 12px;
      padding: 22px 24px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }

    h1 {
      margin: 0;
      font-size: clamp(1.5rem, 2.4vw, 2.1rem);
      font-weight: 700;
      letter-spacing: 0;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      max-width: 1180px;
    }

    .metric {
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 90%, var(--bg));
      padding: 10px 12px;
      min-height: 72px;
      border-radius: 6px;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 0.8rem;
      margin-bottom: 4px;
    }

    .metric strong {
      display: block;
      font-size: 1.15rem;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    main {
      padding: 18px 24px 24px;
    }

    .account-panel {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) minmax(220px, 1.5fr) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }

    label {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 0.82rem;
    }

    input {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
    }

    button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }

    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #ffffff;
    }

    button.active {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 16%, var(--panel));
      color: var(--text);
    }

    #chart {
      width: 100%;
      height: calc(100vh - 330px);
      min-height: 420px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
    }

    .status {
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .status.error { color: var(--bad); }
    .status.ok { color: var(--good); }
    .status.warn { color: var(--warn); }

    @media (max-width: 840px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      .account-panel { grid-template-columns: 1fr; }
      #chart { height: 56vh; min-height: 360px; }
      .toolbar button { flex: 1 1 86px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Octopus Electricity Price Tracker</h1>
    <section class="summary" aria-label="Current price summary">
      <div class="metric"><span>Current price</span><strong id="currentPrice">--</strong></div>
      <div class="metric"><span>Actual product</span><strong id="productName">--</strong></div>
      <div class="metric"><span>Tariff code</span><strong id="tariffCode">--</strong></div>
      <div class="metric"><span>Meter serial</span><strong id="meterSerial">--</strong></div>
      <div class="metric"><span>Latest sample</span><strong id="latestSample">--</strong></div>
      <div class="metric"><span>Samples</span><strong id="sampleCount">0</strong></div>
      <div class="metric"><span>Collector</span><strong id="collectorState">--</strong></div>
    </section>
  </header>

  <main>
    <form id="accountForm" class="account-panel">
      <label>
        Account number
        <input id="accountNumber" name="account_number" autocomplete="off" placeholder="A-XXXXXXXX" required>
      </label>
      <label>
        API key
        <input id="apiKey" name="api_key" type="password" autocomplete="off" placeholder="sk_live_...">
      </label>
      <button type="submit" class="primary">Link account</button>
    </form>

    <div class="toolbar" aria-label="Chart range">
      <button type="button" data-range="30m">30 min</button>
      <button type="button" data-range="6h">6 hours</button>
      <button type="button" data-range="1d">Day</button>
      <button type="button" data-range="1w">Week</button>
      <button type="button" data-range="1mo">Month</button>
      <button type="button" data-range="all" class="active">All</button>
    </div>
    <div id="chart"></div>
    <div id="status" class="status">Loading data...</div>
  </main>

  <script>
    const ranges = {
      "30m": 30 * 60 * 1000,
      "6h": 6 * 60 * 60 * 1000,
      "1d": 24 * 60 * 60 * 1000,
      "1w": 7 * 24 * 60 * 60 * 1000,
      "1mo": 31 * 24 * 60 * 60 * 1000
    };

    let allSamples = [];
    let allConsumption = [];
    let latestStatus = {};
    let activeRange = "all";

    const els = {
      chart: document.getElementById("chart"),
      status: document.getElementById("status"),
      currentPrice: document.getElementById("currentPrice"),
      latestSample: document.getElementById("latestSample"),
      sampleCount: document.getElementById("sampleCount"),
      collectorState: document.getElementById("collectorState"),
      productName: document.getElementById("productName"),
      tariffCode: document.getElementById("tariffCode"),
      meterSerial: document.getElementById("meterSerial"),
      accountForm: document.getElementById("accountForm"),
      accountNumber: document.getElementById("accountNumber"),
      apiKey: document.getElementById("apiKey")
    };

    function formatTime(iso) {
      if (!iso) return "--";
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "medium"
      }).format(new Date(iso));
    }

    function rangeSamples() {
      if (activeRange === "all") return allSamples;
      const cutoff = Date.now() - ranges[activeRange];
      return allSamples.filter(point => new Date(point.collected_at).getTime() >= cutoff);
    }

    function rangeConsumption() {
      if (activeRange === "all") return allConsumption;
      const cutoff = Date.now() - ranges[activeRange];
      return allConsumption.filter(point => new Date(point.interval_start).getTime() >= cutoff);
    }

    function setStatus(text, kind = "") {
      els.status.textContent = text;
      els.status.className = `status ${kind}`.trim();
    }

    function updateAccount(config, status) {
      if (config && config.account_number) {
        els.accountNumber.value = config.account_number;
      }
      els.apiKey.placeholder = config && config.has_api_key ? "Stored on server" : "sk_live_...";

      const tariff = (status && status.active_tariff) || {};
      els.productName.textContent = tariff.product_name || tariff.product_code || "--";
      els.tariffCode.textContent = tariff.tariff_code || "--";
      els.meterSerial.textContent = tariff.meter_serial || (tariff.meter_serials || []).join(", ") || "--";
    }

    function updateSummary(status) {
      const activeTariffCode = status.active_tariff && status.active_tariff.tariff_code;
      const accountSamples = activeTariffCode
        ? allSamples.filter(point => point.tariff_code === activeTariffCode)
        : allSamples.filter(point => point.account_linked);
      const latest = (accountSamples.length ? accountSamples : allSamples).at(-1);
      els.currentPrice.textContent = latest ? `${latest.value_inc_vat.toFixed(2)} p/kWh` : "--";
      els.latestSample.textContent = latest ? formatTime(latest.collected_at) : "--";
      els.sampleCount.textContent = `${allSamples.length.toLocaleString()} prices / ${allConsumption.length.toLocaleString()} usage`;
      els.collectorState.textContent = status.last_error ? "Error" : (status.last_ok ? "Running" : "Starting");
      els.collectorState.style.color = status.last_error ? "var(--bad)" : "var(--good)";
    }

    function drawChart() {
      const samples = rangeSamples();
      const consumption = rangeConsumption();
      const activeTariffCode = latestStatus.active_tariff && latestStatus.active_tariff.tariff_code;
      const comparisonColours = [
        "#c9d8ff",
        "#cdeedc",
        "#f6d4dc",
        "#f8e1b8",
        "#d9cff5",
        "#bfe7ec",
        "#ead6c7",
        "#d7e8b4"
      ];
      const groups = new Map();
      for (const point of samples) {
        const key = `${point.tariff_code || "unknown"}::${point.rate_name || "standard"}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(point);
      }

      const consumptionTrace = {
        x: consumption.map(point => new Date(point.interval_start)),
        y: consumption.map(point => point.consumption_kwh),
        customdata: consumption.map(point => [point.interval_end || "", point.meter_serial || ""]),
        type: "bar",
        name: "Actual consumption",
        yaxis: "y2",
        marker: { color: "rgba(114, 82, 163, 0.26)", line: { color: "rgba(114, 82, 163, 0.55)", width: 1 } },
        width: 28 * 60 * 1000,
        hovertemplate: "%{x|%d %b %Y %H:%M}<br>%{y:.3f} kWh<br>to %{customdata[0]}<extra></extra>"
      };

      const priceTraces = Array.from(groups.entries()).map(([key, points], index) => {
        const first = points[0] || {};
        const isActive = first.tariff_code === activeTariffCode || first.account_linked;
        const colour = isActive ? "#0078a8" : comparisonColours[index % comparisonColours.length];
        const label = [
          first.account_linked ? "Linked account" : "Comparison tariff",
          first.tariff_code || "Unknown tariff",
          first.rate_name || "standard"
        ].join(" - ");

        return {
          x: points.map(point => new Date(point.collected_at)),
          y: points.map(point => point.value_inc_vat),
          customdata: points.map(point => [
            point.tariff_code || "",
            point.product_name || point.product_code || "",
            point.rate_name || "standard"
          ]),
          type: "scatter",
          mode: "lines+markers",
          name: label,
          line: { color: colour, width: isActive ? 3 : 1.4 },
          marker: { color: colour, size: isActive ? 7 : 4 },
          opacity: isActive ? 1 : 0.55,
          hovertemplate: "%{x|%d %b %Y %H:%M}<br>%{y:.2f} p/kWh<br>%{customdata[1]}<br>%{customdata[0]}<br>%{customdata[2]}<extra></extra>"
        };
      });
      const traces = consumption.length ? [consumptionTrace, ...priceTraces] : priceTraces;

      const rootStyle = getComputedStyle(document.documentElement);
      const layout = {
        margin: { l: 58, r: 20, t: 20, b: 48 },
        paper_bgcolor: rootStyle.getPropertyValue("--panel").trim(),
        plot_bgcolor: rootStyle.getPropertyValue("--panel").trim(),
        font: { color: rootStyle.getPropertyValue("--text").trim() },
        xaxis: {
          title: "Collected at",
          gridcolor: rootStyle.getPropertyValue("--line").trim(),
          rangeselector: {
            buttons: [
              { count: 30, label: "30m", step: "minute", stepmode: "backward" },
              { count: 6, label: "6h", step: "hour", stepmode: "backward" },
              { count: 1, label: "1d", step: "day", stepmode: "backward" },
              { count: 7, label: "1w", step: "day", stepmode: "backward" },
              { count: 1, label: "1mo", step: "month", stepmode: "backward" },
              { step: "all", label: "All" }
            ]
          }
        },
        yaxis: {
          title: "p/kWh",
          zeroline: true,
          gridcolor: rootStyle.getPropertyValue("--line").trim()
        },
        yaxis2: {
          title: "kWh",
          overlaying: "y",
          side: "right",
          zeroline: true,
          gridcolor: "rgba(0,0,0,0)",
          rangemode: "tozero"
        },
        barmode: "overlay",
        hovermode: "x unified",
        showlegend: true,
        legend: { orientation: "h", y: -0.25 }
      };

      Plotly.react(els.chart, traces, layout, {
        responsive: true,
        displaylogo: false,
        scrollZoom: true,
        modeBarButtonsToRemove: ["lasso2d", "select2d"]
      });

      setStatus(
        samples.length
          ? `Showing ${samples.length.toLocaleString()} price sample${samples.length === 1 ? "" : "s"} and ${consumption.length.toLocaleString()} usage chunk${consumption.length === 1 ? "" : "s"}. Drag to zoom, double-click to reset, scroll to zoom.`
          : "No price samples in this range yet."
      );
    }

    async function refresh() {
      try {
        const response = await fetch("/api/prices", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        allSamples = payload.samples || [];
        allConsumption = payload.consumption || [];
        latestStatus = payload.status || {};
        updateAccount(payload.config || {}, payload.status || {});
        updateSummary(payload.status || {});
        drawChart();
        if (payload.status && payload.status.last_error) {
          setStatus(`${els.status.textContent} Last collector error: ${payload.status.last_error}`, "error");
        } else if (payload.status && payload.status.last_consumption_error) {
          setStatus(`${els.status.textContent} Consumption unavailable: ${payload.status.last_consumption_error}`, "warn");
        } else if ((payload.consumption || []).length === 0 && payload.status && payload.status.active_tariff && payload.status.active_tariff.meter_serial) {
          const query = payload.status.last_consumption_query || {};
          const windowText = query.period_from && query.period_to ? ` from ${formatTime(query.period_from)} to ${formatTime(query.period_to)}` : "";
          const serialText = (query.tried_serials || [query.meter_serial || payload.status.active_tariff.meter_serial]).filter(Boolean).join(", ");
          setStatus(`${els.status.textContent} No consumption data returned by Octopus yet for MPAN ${query.mpan || payload.status.active_tariff.mpan || "--"} meter ${serialText || "--"}${windowText}.`, "warn");
        } else if (payload.status && payload.status.last_comparison_error) {
          setStatus(`${els.status.textContent} Some comparison tariffs could not be read: ${payload.status.last_comparison_error}`, "warn");
        }
      } catch (error) {
        setStatus(`Could not load price data: ${error.message}`, "error");
      }
    }

    async function saveAccount(event) {
      event.preventDefault();
      setStatus("Linking account...", "warn");
      const body = {
        account_number: els.accountNumber.value.trim(),
        api_key: els.apiKey.value.trim()
      };

      try {
        const response = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        els.apiKey.value = "";
        allSamples = payload.samples || allSamples;
        allConsumption = payload.consumption || allConsumption;
        latestStatus = payload.status || latestStatus;
        updateAccount(payload.config || {}, payload.status || {});
        updateSummary(payload.status || {});
        drawChart();
        setStatus("Account linked. The next sample will use your active electricity tariff.", "ok");
      } catch (error) {
        setStatus(`Could not link account: ${error.message}`, "error");
      }
    }

    document.querySelectorAll("[data-range]").forEach(button => {
      button.addEventListener("click", () => {
        activeRange = button.dataset.range;
        document.querySelectorAll("[data-range]").forEach(b => b.classList.toggle("active", b === button));
        drawChart();
      });
    });

    els.accountForm.addEventListener("submit", saveAccount);
    refresh();
    setInterval(refresh, 60 * 1000);
    window.addEventListener("resize", () => Plotly.Plots.resize(els.chart));
  </script>
</body>
</html>
"""


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat().replace("+00:00", "Z")


def local_now():
    return datetime.now().astimezone()


def local_now_iso():
    return local_now().isoformat(timespec="seconds")


def parse_octopus_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json_file(path, default):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def read_samples():
    data = read_json_file(DATA_FILE, [])

    if isinstance(data, list):
        return data

    return data.get("samples", [])


def read_consumption():
    init_config_db()
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT interval_start, interval_end, consumption_kwh, collected_at
            FROM consumption_readings
            ORDER BY interval_start
            """
        ).fetchall()

    return [dict(row) for row in rows]


def read_legacy_consumption():
    data = read_json_file(DATA_FILE, [])

    if isinstance(data, dict):
        return data.get("consumption", [])

    return []


def write_samples(samples, source_url, active_tariff, consumption=None):
    payload = {
        "updated_at": local_now_iso(),
        "source_url": source_url,
        "poll_seconds": POLL_SECONDS,
        "active_tariff": active_tariff,
        "samples": samples,
    }
    temp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
    temp_file.replace(DATA_FILE)


def load_config():
    with CONFIG_LOCK:
        init_config_db()
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute(
                "SELECT account_id, api_key, mpan, meter_serial FROM account_config LIMIT 1"
            ).fetchone()

    return {
        "account_number": str(row[0] if row else "").strip(),
        "api_key": str(row[1] if row else "").strip(),
        "mpan": str(row[2] if row and row[2] else "").strip(),
        "meter_serial": str(row[3] if row and row[3] else "").strip(),
    }


def public_config(config=None):
    config = config or load_config()
    return {
        "account_number": config.get("account_number", ""),
        "has_api_key": bool(config.get("api_key")),
        "config_db": str(DB_FILE),
    }


def init_config_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_config (
                account_id TEXT NOT NULL,
                api_key TEXT NOT NULL,
                mpan TEXT,
                meter_serial TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumption_readings (
                interval_start TEXT NOT NULL,
                interval_end TEXT NOT NULL,
                consumption_kwh REAL NOT NULL,
                collected_at TEXT,
                PRIMARY KEY (interval_start, interval_end)
            )
            """
        )
        account_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(account_config)").fetchall()
        }
        if "mpan" not in account_columns:
            conn.execute("ALTER TABLE account_config ADD COLUMN mpan TEXT")
        if "meter_serial" not in account_columns:
            conn.execute("ALTER TABLE account_config ADD COLUMN meter_serial TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_consumption_interval_start
            ON consumption_readings (interval_start)
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM account_config").fetchone()[0]
        if count == 0 and CONFIG_FILE.exists():
            legacy = read_json_file(CONFIG_FILE, {})
            account_id = str(legacy.get("account_number", "")).strip()
            api_key = str(legacy.get("api_key", "")).strip()
            if account_id and api_key:
                conn.execute(
                    "INSERT INTO account_config (account_id, api_key) VALUES (?, ?)",
                    (account_id, api_key),
                )

        consumption_count = conn.execute("SELECT COUNT(*) FROM consumption_readings").fetchone()[0]
        if consumption_count == 0:
            upsert_consumption_rows(conn, read_legacy_consumption())


def upsert_consumption_rows(conn, rows):
    for row in rows:
        interval_start = row.get("interval_start")
        interval_end = row.get("interval_end")
        consumption_kwh = row.get("consumption_kwh")
        if not all([interval_start, interval_end]) or consumption_kwh is None:
            continue

        conn.execute(
            """
            INSERT INTO consumption_readings (
                interval_start, interval_end, consumption_kwh, collected_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(interval_start, interval_end)
            DO UPDATE SET
                consumption_kwh = excluded.consumption_kwh,
                collected_at = excluded.collected_at
            """,
            (
                str(interval_start),
                str(interval_end),
                float(consumption_kwh),
                row.get("collected_at"),
            ),
        )


def save_config(config):
    cleaned = {
        "account_number": config.get("account_number", "").strip(),
        "api_key": config.get("api_key", "").strip(),
        "mpan": config.get("mpan", "").strip(),
        "meter_serial": config.get("meter_serial", "").strip(),
    }
    with CONFIG_LOCK:
        init_config_db()
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("DELETE FROM account_config")
            conn.execute(
                """
                INSERT INTO account_config (account_id, api_key, mpan, meter_serial)
                VALUES (?, ?, ?, ?)
                """,
                (
                    cleaned["account_number"],
                    cleaned["api_key"],
                    cleaned["mpan"],
                    cleaned["meter_serial"],
                ),
            )
    return cleaned


def octopus_get(url, api_key=None, params=None):
    auth = (api_key, "") if api_key else None
    response = requests.get(url, auth=auth, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def octopus_get_all_results(url, api_key=None, params=None):
    auth = (api_key, "") if api_key else None
    next_url = url
    next_params = dict(params or {})
    results = []

    while next_url:
        response = requests.get(next_url, auth=auth, params=next_params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        results.extend(payload.get("results", []))
        next_url = payload.get("next")
        next_params = None

    return results


def product_code_from_tariff(tariff_code):
    parts = tariff_code.split("-")
    if len(parts) < 5:
        raise ValueError(f"Cannot derive product code from tariff code {tariff_code!r}")
    return "-".join(parts[2:-1])


def active_agreement(agreements):
    now = utc_now()
    active = []
    for agreement in agreements:
        valid_from = parse_octopus_time(agreement.get("valid_from"))
        valid_to = parse_octopus_time(agreement.get("valid_to"))
        if valid_from and valid_from <= now and (valid_to is None or now < valid_to):
            active.append(agreement)

    if active:
        return sorted(active, key=lambda item: item.get("valid_from") or "", reverse=True)[0]

    open_ended = [item for item in agreements if item.get("valid_to") is None]
    if open_ended:
        return sorted(open_ended, key=lambda item: item.get("valid_from") or "", reverse=True)[0]

    return sorted(agreements, key=lambda item: item.get("valid_from") or "", reverse=True)[0] if agreements else None


def find_current_electricity_tariff(account_data):
    for prop in account_data.get("properties", []):
        for meter_point in prop.get("electricity_meter_points", []):
            if meter_point.get("is_export"):
                continue
            agreement = active_agreement(meter_point.get("agreements", []))
            if agreement:
                return prop, meter_point, agreement

    raise RuntimeError("No active import electricity tariff was found on this account")


def meter_serials_for_point(meter_point):
    meters = meter_point.get("meters") or []
    serials = []
    for meter in meters:
        serial = meter.get("serial_number") or meter.get("serial")
        if serial and serial not in serials:
            serials.append(serial)
    return serials


def meter_serial_for_point(meter_point):
    serials = meter_serials_for_point(meter_point)
    return serials[0] if serials else None


def rate_endpoints_for_tariff(tariff_code):
    if tariff_code.startswith("E-2R-"):
        return [
            ("day", "day-unit-rates"),
            ("night", "night-unit-rates"),
            ("standard", "standard-unit-rates"),
        ]
    return [("standard", "standard-unit-rates")]


def price_url(product_code, tariff_code, endpoint):
    return f"{API_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/{endpoint}/"


def consumption_url(mpan, meter_serial):
    return (
        f"{API_BASE}/electricity-meter-points/{quote(str(mpan), safe='')}/"
        f"meters/{quote(str(meter_serial), safe='')}/consumption/"
    )


def fetch_product_detail(product_code):
    try:
        return octopus_get(f"{API_BASE}/products/{product_code}/")
    except requests.HTTPError:
        return {"code": product_code, "display_name": product_code}


def fetch_product_summary(product_code):
    product = fetch_product_detail(product_code)

    return {
        "product_code": product_code,
        "product_name": product.get("display_name") or product.get("full_name") or product_code,
        "product_full_name": product.get("full_name"),
    }


def walk_tariff_entries(value):
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and code.startswith(("E-1R-", "E-2R-")):
            yield code, value

        for child in value.values():
            yield from walk_tariff_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_tariff_entries(child)


def product_electricity_tariffs(product_code):
    product = fetch_product_detail(product_code)
    product_name = product.get("display_name") or product.get("full_name") or product_code
    product_full_name = product.get("full_name")
    tariff_groups = (
        "single_register_electricity_tariffs",
        "dual_register_electricity_tariffs",
        "single_register_business_electricity_tariffs",
        "dual_register_business_electricity_tariffs",
    )
    tariffs = {}

    for group_name in tariff_groups:
        group = product.get(group_name)
        if not isinstance(group, dict):
            continue

        for tariff_code, tariff_data in walk_tariff_entries(group):
            tariffs[tariff_code] = {
                "account_number": None,
                "account_linked": False,
                "product_code": product_code,
                "product_name": product_name,
                "product_full_name": product_full_name,
                "tariff_code": tariff_code,
                "tariff_display_name": tariff_data.get("display_name") or tariff_data.get("description"),
                "mpan": None,
            }

    return list(tariffs.values())


def fallback_tariff():
    return {
        "account_number": None,
        "account_linked": False,
        "product_code": "AGILE-24-10-01",
        "product_name": "Agile Octopus",
        "tariff_code": "E-1R-AGILE-24-10-01-L",
        "mpan": None,
        "rate_name": "standard",
        "price_url": DEFAULT_PRICE_URL,
    }


def resolve_account_tariff(config=None):
    config = config or load_config()
    if not config["account_number"] or not config["api_key"]:
        return fallback_tariff()

    account_url = f"{API_BASE}/accounts/{config['account_number']}/"
    account = octopus_get(account_url, api_key=config["api_key"])
    prop, meter_point, agreement = find_current_electricity_tariff(account)
    tariff_code = agreement["tariff_code"]
    product_code = product_code_from_tariff(tariff_code)
    product = fetch_product_summary(product_code)
    rate_name, endpoint = rate_endpoints_for_tariff(tariff_code)[0]
    meter_serials = meter_serials_for_point(meter_point)

    return {
        "account_number": account.get("number") or config["account_number"],
        "account_linked": True,
        "property_id": prop.get("id"),
        "postcode": prop.get("postcode"),
        "mpan": meter_point.get("mpan"),
        "meter_serial": meter_serials[0] if meter_serials else None,
        "meter_serials": meter_serials,
        "product_code": product_code,
        "product_name": product["product_name"],
        "product_full_name": product.get("product_full_name"),
        "tariff_code": tariff_code,
        "tariff_valid_from": agreement.get("valid_from"),
        "tariff_valid_to": agreement.get("valid_to"),
        "rate_name": rate_name,
        "price_url": price_url(product_code, tariff_code, endpoint),
    }


def current_rate_sample(tariff, rate_name, url, rate):
    return {
        "collected_at": local_now_iso(),
        "value_inc_vat": float(rate["value_inc_vat"]),
        "value_exc_vat": float(rate["value_exc_vat"]),
        "valid_from": rate["valid_from"],
        "valid_to": rate["valid_to"],
        "payment_method": rate.get("payment_method"),
        "account_number": tariff.get("account_number"),
        "account_linked": tariff.get("account_linked", False),
        "mpan": tariff.get("mpan"),
        "product_code": tariff.get("product_code"),
        "product_name": tariff.get("product_name"),
        "product_full_name": tariff.get("product_full_name"),
        "tariff_code": tariff.get("tariff_code"),
        "tariff_display_name": tariff.get("tariff_display_name"),
        "rate_name": rate_name,
        "price_url": url,
    }


def fetch_current_prices_for_tariff(tariff):
    now = utc_now()
    period_from = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    period_to = (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    last_error = None
    samples = []

    for rate_name, endpoint in rate_endpoints_for_tariff(tariff["tariff_code"]):
        url = price_url(tariff["product_code"], tariff["tariff_code"], endpoint)

        try:
            data = octopus_get(url, params={"period_from": period_from, "period_to": period_to})
        except Exception as exc:
            last_error = exc
            continue

        for rate in data.get("results", []):
            start = parse_octopus_time(rate["valid_from"])
            end = parse_octopus_time(rate["valid_to"])
            if start and end and start <= now < end:
                samples.append(current_rate_sample(tariff, rate_name, url, rate))
                break

    if samples:
        return samples

    if last_error:
        raise last_error
    raise RuntimeError("Octopus response did not contain a current price window")


def fetch_current_price_for_tariff(tariff):
    return fetch_current_prices_for_tariff(tariff)[0]


def consumption_sample(tariff, row):
    interval_start = row.get("interval_start")
    interval_end = row.get("interval_end")
    return {
        "interval_start": interval_start,
        "interval_end": interval_end,
        "consumption_kwh": float(row["consumption"]),
        "collected_at": local_now_iso(),
    }


def consumption_period():
    now = utc_now()
    return (
        (now - timedelta(days=31)).isoformat().replace("+00:00", "Z"),
        now.isoformat().replace("+00:00", "Z"),
    )


def save_consumption(rows):
    init_config_db()
    with sqlite3.connect(DB_FILE) as conn:
        upsert_consumption_rows(conn, rows)


def fetch_consumption_for_tariff(tariff, api_key):
    mpan = tariff.get("mpan")
    meter_serials = tariff.get("meter_serials") or []
    if tariff.get("meter_serial") and tariff.get("meter_serial") not in meter_serials:
        meter_serials.insert(0, tariff.get("meter_serial"))
    if not mpan or not meter_serials or not api_key:
        COLLECTOR_STATE["last_consumption_query"] = {
            "mpan": mpan,
            "meter_serial": tariff.get("meter_serial"),
            "tried_serials": meter_serials,
            "period_from": None,
            "period_to": None,
            "rows": 0,
            "rows_by_serial": {},
        }
        return []

    period_from, period_to = consumption_period()
    rows_by_serial = {}
    last_rows = []

    for meter_serial in meter_serials:
        rows = octopus_get_all_results(
            consumption_url(mpan, meter_serial),
            api_key=api_key,
            params={
                "period_from": period_from,
                "period_to": period_to,
                "order_by": "period",
                "page_size": 25000,
            },
        )
        rows_by_serial[meter_serial] = len(rows)
        last_rows = rows
        if rows:
            tariff["meter_serial"] = meter_serial
            break
    else:
        rows = last_rows

    COLLECTOR_STATE["last_consumption_query"] = {
        "mpan": mpan,
        "meter_serial": tariff.get("meter_serial"),
        "tried_serials": meter_serials,
        "period_from": period_from,
        "period_to": period_to,
        "rows": len(rows),
        "rows_by_serial": rows_by_serial,
    }
    return [consumption_sample(tariff, row) for row in rows if row.get("consumption") is not None]


def fetch_current_price():
    tariff = resolve_account_tariff()
    COLLECTOR_STATE["account_linked"] = tariff.get("account_linked", False)
    COLLECTOR_STATE["active_tariff"] = {
        key: value for key, value in tariff.items() if key != "price_url"
    }
    return fetch_current_price_for_tariff(tariff)


def fetch_all_current_prices(config=None):
    config = config or load_config()
    active_tariff = resolve_account_tariff(config)
    active_tariff["account_linked"] = active_tariff.get("account_linked", False)
    COLLECTOR_STATE["account_linked"] = active_tariff.get("account_linked", False)
    COLLECTOR_STATE["active_tariff"] = {
        key: value for key, value in active_tariff.items() if key != "price_url"
    }

    active_samples = fetch_current_prices_for_tariff(active_tariff)
    samples = list(active_samples)
    comparison_errors = []

    for tariff in product_electricity_tariffs(active_tariff["product_code"]):
        if tariff["tariff_code"] == active_tariff["tariff_code"]:
            continue

        try:
            samples.extend(fetch_current_prices_for_tariff(tariff))
        except Exception as exc:
            comparison_errors.append(f"{tariff['tariff_code']}: {exc}")

    COLLECTOR_STATE["last_comparison_error"] = "; ".join(comparison_errors[:5]) if comparison_errors else None
    try:
        consumption = fetch_consumption_for_tariff(active_tariff, config.get("api_key"))
        COLLECTOR_STATE["active_tariff"]["meter_serial"] = active_tariff.get("meter_serial")
        COLLECTOR_STATE["last_consumption_error"] = None
    except Exception as exc:
        consumption = []
        COLLECTOR_STATE["last_consumption_error"] = str(exc)
        COLLECTOR_STATE["last_consumption_query"] = {
            "mpan": active_tariff.get("mpan"),
            "meter_serial": active_tariff.get("meter_serial"),
            "tried_serials": active_tariff.get("meter_serials") or [active_tariff.get("meter_serial")],
            "period_from": None,
            "period_to": None,
            "rows": 0,
            "rows_by_serial": {},
        }

    return active_samples[0], samples, consumption


def collect_once():
    COLLECTOR_STATE["last_attempt"] = local_now_iso()
    sample, batch, consumption_batch = fetch_all_current_prices()

    with DATA_LOCK:
        samples = read_samples()
        samples.extend(batch)
        samples.sort(key=lambda point: parse_octopus_time(point["collected_at"]))
        write_samples(samples, sample["price_url"], COLLECTOR_STATE["active_tariff"])
    save_consumption(consumption_batch)

    COLLECTOR_STATE["last_ok"] = sample["collected_at"]
    COLLECTOR_STATE["last_error"] = None
    return sample


def collector_loop():
    while True:
        try:
            sample = collect_once()
            print(
                f"Collected {sample['value_inc_vat']:.2f} p/kWh from {sample.get('tariff_code')} "
                f"at {sample['collected_at']}",
                flush=True,
            )
        except Exception as exc:
            COLLECTOR_STATE["last_error"] = str(exc)
            print(f"Collector error: {exc}", flush=True)

        time.sleep(POLL_SECONDS)


class OctopusHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}", flush=True)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self.send_html(INDEX_HTML)
            return

        if path == "/api/prices":
            with DATA_LOCK:
                samples = read_samples()
                consumption = read_consumption()
            self.send_json(
                {
                    "samples": samples,
                    "consumption": consumption,
                    "status": COLLECTOR_STATE,
                    "config": public_config(),
                }
            )
            return

        if path == "/api/status":
            self.send_json({"status": COLLECTOR_STATE, "config": public_config()})
            return

        if path == "/api/config":
            self.send_json({"config": public_config(), "status": COLLECTOR_STATE})
            return

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/config":
            try:
                incoming = self.read_json_body()
                existing = load_config()
                account_number = str(incoming.get("account_number", "")).strip()
                api_key = str(incoming.get("api_key", "")).strip() or existing.get("api_key", "")
                if not account_number or not api_key:
                    raise ValueError("Account number and API key are required")

                pending_config = {"account_number": account_number, "api_key": api_key}
                tariff = resolve_account_tariff(pending_config)
                config = save_config(
                    {
                        "account_number": account_number,
                        "api_key": api_key,
                        "mpan": tariff.get("mpan") or "",
                        "meter_serial": tariff.get("meter_serial") or "",
                    }
                )
                sample = collect_once()
                with DATA_LOCK:
                    samples = read_samples()
                    consumption = read_consumption()
                self.send_json(
                    {
                        "config": public_config(config),
                        "status": COLLECTOR_STATE,
                        "sample": sample,
                        "samples": samples,
                        "consumption": consumption,
                    }
                )
            except Exception as exc:
                COLLECTOR_STATE["last_error"] = str(exc)
                self.send_json({"error": str(exc), "status": COLLECTOR_STATE}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/collect":
            try:
                sample = collect_once()
                with DATA_LOCK:
                    consumption = read_consumption()
                self.send_json(
                    {
                        "sample": sample,
                        "consumption": consumption,
                        "status": COLLECTOR_STATE,
                        "config": public_config(),
                    }
                )
            except Exception as exc:
                COLLECTOR_STATE["last_error"] = str(exc)
                self.send_json({"error": str(exc), "status": COLLECTOR_STATE}, HTTPStatus.BAD_GATEWAY)
            return

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    init_config_db()
    server = ThreadingHTTPServer((HOST, PORT), OctopusHandler)
    print(f"Serving Octopus price tracker at http://{HOST}:{PORT}/", flush=True)
    print(f"Writing samples to {DATA_FILE.resolve()}", flush=True)
    print(f"Reading account config from {DB_FILE.resolve()}", flush=True)

    collector = threading.Thread(target=collector_loop, daemon=True)
    collector.start()

    server.serve_forever()


if __name__ == "__main__":
    main()
