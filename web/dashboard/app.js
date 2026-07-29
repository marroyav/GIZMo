(() => {
  "use strict";

  const COLORS = [
    "#b8c41f",
    "#4f7f86",
    "#8d7fb8",
    "#c39a4a",
    "#56836c",
    "#876f62",
  ];
  const HIGH_Z_COLOR = "#b8c41f";
  const NORMAL_COLOR = "#b8c41f";
  const ALARM_COLOR = "#ff6a2a";
  const THRESHOLD_COLOR = "#747d86";
  const HIGH_Z_SERIES_PATH = "__display.high_z_floor__";
  const MAX_HISTORY_SECONDS = 3600;
  const HISTORY_MAX_POINTS = 5000;
  const MAX_EVENTS = 24;
  const VIEW_COPY = {
    alarm: "Authoritative composite relay/beacon alarm from the ZMon engine",
    impedance: "Equivalent resistance, HIGH Z floor, and configured threshold",
    thermal: "Chassis and processor thermal sensors",
    lockin: "Lock-in magnitude and orthogonal components",
    phase: "Recovered phase estimates from the measurement pipeline",
    system: "Processor and filesystem utilization",
  };
  const HEALTH_PATHS = [
    ["Measurement", "Health.Measurement"],
    ["Thermal", "Health.Thermal"],
    ["Clock", "Health.Time"],
    ["System", "Health.OperatingSystem"],
    ["Network", "Health.Network"],
    ["Storage", "Health.Storage"],
    ["Firmware", "Health.Firmware"],
    ["Services", "Health.Services"],
    ["Calibration", "Health.Calibration"],
    ["SDR", "Health.SDR"],
  ];

  const byId = (id) => document.getElementById(id);
  const dom = {
    connectionLabel: byId("connection-label"),
    connectionDetail: byId("connection-detail"),
    connectionDot: byId("connection-dot"),
    headerClock: byId("header-clock"),
    identityHost: byId("identity-host"),
    identityRuntime: byId("identity-runtime"),
    identityTimezone: byId("identity-timezone"),
    resistanceCard: byId("resistance-card"),
    resistanceValue: byId("resistance-value"),
    resistanceUnit: byId("resistance-unit"),
    measurementStatus: byId("measurement-status"),
    measurementQuality: byId("measurement-quality"),
    thresholdFill: byId("threshold-fill"),
    thresholdMarker: byId("threshold-marker"),
    thresholdValue: byId("threshold-value"),
    capacitanceValue: byId("capacitance-value"),
    phaseValue: byId("phase-value"),
    frequencyValue: byId("frequency-value"),
    alarmCard: byId("alarm-card"),
    alarmTitle: byId("alarm-title"),
    alarmReason: byId("alarm-reason"),
    localTime: byId("local-time"),
    latchTime: byId("latch-time"),
    overallHealth: byId("overall-health"),
    pulseGrid: byId("pulse-grid"),
    trendTitle: byId("trend-title"),
    trendSubtitle: byId("trend-subtitle"),
    trendModeLabel: byId("trend-mode-label"),
    modeSelect: byId("mode-select"),
    rangeSelect: byId("range-select"),
    historyRange: byId("history-range"),
    historyFrom: byId("history-from"),
    historyTo: byId("history-to"),
    pauseButton: byId("pause-button"),
    exportButton: byId("export-button"),
    chartStack: byId("chart-stack"),
    plotState: byId("plot-state"),
    healthGrid: byId("health-grid"),
    healthSummary: byId("health-summary"),
    networkGrid: byId("network-grid"),
    networkSummary: byId("network-summary"),
    servicesGrid: byId("services-grid"),
    servicesSummary: byId("services-summary"),
    variableDetails: byId("variables"),
    variableSearch: byId("variable-search"),
    groupSelect: byId("group-select"),
    variableTable: byId("variable-table"),
    variableCount: byId("variable-count"),
    eventList: byId("event-list"),
    clearEvents: byId("clear-events"),
    footerEndpoint: byId("footer-endpoint"),
  };

  const state = {
    catalog: [],
    specs: new Map(),
    views: [],
    serviceUnits: [],
    snapshot: null,
    samples: [],
    historySamples: [],
    plotMode: "live",
    historyAvailable: false,
    historyLoading: false,
    historyResolution: null,
    historyFromMs: null,
    historyToMs: null,
    historyRequest: 0,
    customView: null,
    chartPanels: new Map(),
    hoverViewId: null,
    windowSeconds: Number(dom.rangeSelect.value),
    paused: false,
    streamOpen: false,
    lastSampleSequence: null,
    lastSampleAt: 0,
    tableRows: new Map(),
    transitions: new Map(),
    hoverTime: null,
    drawPending: false,
    highZFloorOhm: 500,
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function valuePayload(path) {
    return state.snapshot?.values?.[path] || {
      value: null,
      status: "BadWaitingForInitialData",
      received_at: null,
    };
  }

  function raw(path) {
    return valuePayload(path).value;
  }

  function number(path) {
    const candidate = raw(path);
    return typeof candidate === "number" && Number.isFinite(candidate)
      ? candidate
      : null;
  }

  function boolean(path) {
    return typeof raw(path) === "boolean" ? raw(path) : null;
  }

  function numericPlotValue(value) {
    if (typeof value === "boolean") return value ? 1 : 0;
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function statusClass(value) {
    const normalized = String(value || "").toLowerCase();
    if (normalized.startsWith("good") || normalized === "active") return "good";
    if (normalized.startsWith("uncertain") || normalized === "degraded") {
      return "uncertain";
    }
    if (
      normalized.startsWith("bad") ||
      normalized === "failed" ||
      normalized === "inactive"
    ) {
      return "bad";
    }
    return "unknown";
  }

  function setChip(node, value) {
    const label = value || "Unknown";
    node.textContent = label;
    node.className = `quality-chip quality-${statusClass(label)}`;
  }

  function formatNumber(value, precision = 1) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
    }).format(value);
  }

  function formatBytes(value) {
    if (!Number.isFinite(value)) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let scaled = value;
    let index = 0;
    while (Math.abs(scaled) >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    const precision = index === 0 ? 0 : scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    return `${formatNumber(scaled, precision)} ${units[index]}`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  }

  function parseDate(value) {
    if (!value || String(value).startsWith("1601-")) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatDateTime(value, includeDate = true) {
    const parsed = parseDate(value);
    if (!parsed) return "—";
    const options = includeDate
      ? {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        }
      : {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        };
    return new Intl.DateTimeFormat(undefined, options).format(parsed);
  }

  function formatValue(spec, payload, compact = false) {
    const value = payload?.value;
    if (value === null || value === undefined || value === "") return "—";
    if (spec.data_type === "Boolean") return value ? "Yes" : "No";
    if (spec.data_type === "DateTime") return formatDateTime(value, !compact);
    if (Array.isArray(value)) return value.join(", ") || "—";
    if (typeof value !== "number") return String(value);
    if (spec.unit === "byte") return formatBytes(value);
    if (spec.path === "Time.UptimeSeconds") return formatDuration(value);
    const rendered = formatNumber(value, spec.precision);
    return spec.unit ? `${rendered} ${spec.unit}` : rendered;
  }

  function relativeTime(value) {
    const date = parseDate(value);
    if (!date) return "never";
    const elapsed = Math.max(0, (Date.now() - date.getTime()) / 1000);
    if (elapsed < 2) return "now";
    if (elapsed < 60) return `${Math.floor(elapsed)}s ago`;
    if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
    return `${Math.floor(elapsed / 3600)}h ago`;
  }

  function displayServiceName(unit) {
    if (unit === "gizmo.target") return "runtime target";
    return unit.replace(/^gizmo-/, "").replace(/\.(service|socket)$/, "");
  }

  function humanize(value) {
    return String(value || "")
      .replaceAll("_", " ")
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/^./, (character) => character.toUpperCase());
  }

  function serviceNodeKey(unit) {
    return unit.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  }

  function plotViews() {
    return state.customView ? [...state.views, state.customView] : state.views;
  }

  function plotPaths() {
    return [...new Set(plotViews().flatMap((view) => view.paths))];
  }

  function setChartEmptyCopy(title, copy) {
    state.chartPanels.forEach((panel) => {
      panel.emptyTitle.textContent = title;
      panel.emptyCopy.textContent = copy;
    });
  }

  function activeSamples() {
    return state.plotMode === "history" ? state.historySamples : state.samples;
  }

  function localInputValue(timestamp) {
    const date = new Date(timestamp);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 19);
  }

  function initializeHistoryInputs() {
    const end = Date.now();
    dom.historyTo.value = localInputValue(end);
    dom.historyFrom.value = localInputValue(end - 3600 * 1000);
  }

  function updateRangeOptions() {
    const history = state.plotMode === "history";
    [...dom.rangeSelect.options].forEach((option) => {
      const supported =
        option.dataset.mode === "both" ||
        option.dataset.mode === (history ? "history" : "live");
      option.hidden = !supported;
      option.disabled = !supported;
    });
    const selected = dom.rangeSelect.selectedOptions[0];
    if (!selected || selected.disabled) {
      dom.rangeSelect.value = history ? "3600" : "900";
    }
    const custom = history && dom.rangeSelect.value === "custom";
    dom.historyRange.hidden = !custom;
    if (!custom) {
      state.windowSeconds = Number(dom.rangeSelect.value);
    }
  }

  function historyBounds() {
    if (dom.rangeSelect.value === "custom") {
      const start = new Date(dom.historyFrom.value).getTime();
      const end = new Date(dom.historyTo.value).getTime();
      if (!Number.isFinite(start) || !Number.isFinite(end)) {
        throw new Error("Choose valid From and To times.");
      }
      if (end <= start) throw new Error("History To must be later than From.");
      return { start, end };
    }
    const end = Date.now();
    const seconds = Number(dom.rangeSelect.value);
    return { start: end - seconds * 1000, end };
  }

  function plotDomain(samples = activeSamples()) {
    if (
      state.plotMode === "history" &&
      state.historyFromMs !== null &&
      state.historyToMs !== null
    ) {
      return {
        start: state.historyFromMs,
        end: state.historyToMs,
        duration: state.historyToMs - state.historyFromMs,
      };
    }
    const end = samples.at(-1)?.time || Date.now();
    return {
      start: end - state.windowSeconds * 1000,
      end,
      duration: state.windowSeconds * 1000,
    };
  }

  async function loadHistory() {
    if (state.plotMode !== "history") return;
    const paths = plotPaths();
    if (!paths.length) return;
    const requestId = ++state.historyRequest;
    state.historyLoading = true;
    dom.pauseButton.disabled = true;
    dom.pauseButton.textContent = "Loading…";
    setChartEmptyCopy(
      "Loading persistent history",
      "Querying the package-owned historian.",
    );
    dom.plotState.textContent = "Historical query in progress…";
    try {
      const { start, end } = historyBounds();
      const parameters = new URLSearchParams({
        series: paths.join(","),
        from: new Date(start).toISOString(),
        to: new Date(end).toISOString(),
        max_points: String(HISTORY_MAX_POINTS),
      });
      const response = await fetch(`/api/history/query?${parameters}`, {
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `history request returned ${response.status}`);
      }
      if (requestId !== state.historyRequest) return;
      state.historySamples = payload.points.map((point) => ({
        time: Number(point.time),
        sequence: point.sequence,
        values: point.values || {},
        resistanceRange: point.resistanceRange,
        resistanceRangeStatus: point.resistanceRangeStatus,
      }));
      state.historyResolution = payload.resolution;
      state.historyFromMs = Date.parse(payload.from);
      state.historyToMs = Date.parse(payload.to);
      state.windowSeconds = Math.max(
        1,
        (state.historyToMs - state.historyFromMs) / 1000,
      );
      setChartEmptyCopy(
        "No retained samples",
        "The historian has no data in this interval for these series.",
      );
      addEvent(
        `Loaded ${payload.point_count} ${payload.resolution} historical points.`,
      );
    } catch (error) {
      if (requestId !== state.historyRequest) return;
      state.historySamples = [];
      state.historyResolution = null;
      state.historyFromMs = null;
      state.historyToMs = null;
      setChartEmptyCopy("History unavailable", error.message);
      dom.plotState.textContent = `History unavailable · ${error.message}`;
      addEvent(`Historical query failed: ${error.message}`, "error");
    } finally {
      if (requestId === state.historyRequest) {
        state.historyLoading = false;
        dom.pauseButton.disabled = false;
        dom.pauseButton.textContent = "Refresh history";
        scheduleDraw();
      }
    }
  }

  function setPlotMode(mode) {
    state.plotMode = mode === "history" ? "history" : "live";
    dom.modeSelect.value = state.plotMode;
    dom.trendModeLabel.textContent =
      state.plotMode === "history" ? "Persistent history" : "Live telemetry";
    dom.pauseButton.classList.remove("is-active");
    dom.pauseButton.textContent =
      state.plotMode === "history"
        ? "Refresh history"
        : state.paused
          ? "Resume plot"
          : "Pause plot";
    updateRangeOptions();
    renderChartHeading();
    hideTooltip();
    if (state.plotMode === "history") {
      loadHistory();
    } else {
      scheduleDraw();
    }
  }

  function addEvent(message, kind = "info") {
    const row = element("li", kind);
    const time = element(
      "time",
      "",
      new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date()),
    );
    row.append(time, element("span", "", message));
    dom.eventList.prepend(row);
    while (dom.eventList.children.length > MAX_EVENTS) {
      dom.eventList.lastElementChild.remove();
    }
  }

  function transition(key, next, message) {
    const previous = state.transitions.get(key);
    state.transitions.set(key, next);
    if (previous !== undefined && previous !== next) addEvent(message(next, previous));
  }

  function updateConnection() {
    const connection = state.snapshot?.connection;
    const connected = Boolean(connection?.connected);
    const live = connected && state.streamOpen;
    dom.connectionDot.className = `status-orb ${live ? "status-good" : "status-bad"}`;
    dom.connectionLabel.textContent = live
      ? "Live instrument data"
      : connected
        ? "Telemetry stream reconnecting"
        : "OPC UA unavailable";
    dom.connectionDetail.textContent = connected
      ? `ns=${connection.namespace_index} · ${relativeTime(connection.last_notification)}`
      : connection?.error || "Waiting for the instrument";
    dom.footerEndpoint.textContent = connection?.endpoint
      ? `OPC UA ${connection.endpoint}`
      : "OPC UA endpoint —";
    transition("connection", live, (next) =>
      next ? "Live OPC UA telemetry restored." : "Live telemetry connection interrupted.",
    );
  }

  function renderIdentity() {
    dom.identityHost.textContent = `host ${raw("Identity.Hostname") || "—"}`;
    dom.identityRuntime.textContent = `runtime ${raw("Identity.RuntimeVersion") || "—"}`;
    dom.identityTimezone.textContent = `timezone ${raw("Time.TimezoneName") || "—"}`;
  }

  function renderMeasurement() {
    const resistance = number("Measurement.ResistanceOhm");
    const range = raw("Measurement.ResistanceRange");
    const threshold = number("Measurement.ThresholdOhm");
    const quality = raw("Measurement.Quality") || "Unknown";
    const diagnostic = raw("Measurement.Diagnostic");
    const highZ = range === "OutOfRange";

    dom.resistanceCard.classList.toggle("high-z", highZ);
    dom.resistanceCard.classList.remove(
      "quality-good",
      "quality-uncertain",
      "quality-bad",
      "quality-unknown",
    );
    dom.resistanceCard.classList.add(`quality-${statusClass(quality)}`);
    dom.resistanceValue.textContent = highZ
      ? "HIGH Z"
      : resistance === null
        ? "—"
        : formatNumber(resistance, 1);
    dom.resistanceUnit.textContent = highZ ? "" : "Ω";
    dom.measurementStatus.textContent = highZ
      ? `Above the validated ${formatNumber(state.highZFloorOhm, 0)} Ω measurement range`
      : diagnostic || `Sample ${raw("Measurement.Sequence") ?? "—"} · ${relativeTime(raw("Measurement.SampleTime"))}`;
    setChip(dom.measurementQuality, quality);
    dom.thresholdValue.textContent =
      threshold === null ? "—" : `${formatNumber(threshold, 1)} Ω`;
    dom.capacitanceValue.textContent =
      number("Measurement.CapacitanceNanofarad") === null
        ? "—"
        : `${formatNumber(number("Measurement.CapacitanceNanofarad"), 2)} nF`;
    dom.phaseValue.textContent =
      number("Measurement.PhaseInterpolatedDegrees") === null
        ? "—"
        : `${formatNumber(number("Measurement.PhaseInterpolatedDegrees"), 2)}°`;
    dom.frequencyValue.textContent =
      number("Measurement.StimulusFrequencyHertz") === null
        ? "—"
        : `${formatNumber(number("Measurement.StimulusFrequencyHertz"), 0)} Hz`;

    const resistancePercent = resistance === null
      ? highZ ? 100 : 0
      : Math.max(0, Math.min(100, (resistance / 500) * 100));
    const thresholdPercent = threshold === null
      ? 0
      : Math.max(0, Math.min(100, (threshold / 500) * 100));
    dom.thresholdFill.style.width = `${resistancePercent}%`;
    dom.thresholdMarker.style.left = `${thresholdPercent}%`;

    transition("measurement-quality", quality, (next) =>
      `Measurement quality changed to ${next}.`,
    );
  }

  function renderAlarm() {
    const active = boolean("Alarm.Active");
    const latched = boolean("Alarm.Latched");
    const reason = raw("Alarm.Reason");
    const activeAlarm = active === true;
    const latchedAlarm = latched === true;
    document.body.classList.toggle("has-alarm", activeAlarm);
    dom.alarmCard.className = `card alarm-card ${
      active === null && latched === null
        ? "state-unknown"
        : activeAlarm
          ? "state-alarm"
          : latchedAlarm
            ? "state-clear state-latched"
            : "state-clear"
    }`;
    dom.alarmTitle.textContent = activeAlarm
      ? "Composite alarm active"
      : latchedAlarm
        ? "Alarm remains latched"
      : active === null
        ? "Establishing state"
        : "Ground path normal";
    dom.alarmReason.textContent = reason
      ? humanize(reason)
      : activeAlarm
        ? "ZMon has asserted the relay/beacon alarm output."
        : latchedAlarm
          ? "Historical latch retained; no alarm is currently active."
        : "No threshold alarm is active.";
    dom.localTime.textContent = formatDateTime(raw("Time.CurrentLocal"), false);
    dom.latchTime.textContent = latched
      ? formatDateTime(raw("Alarm.LatchTime"))
      : "Never";
    transition("alarm-active", activeAlarm, (next) =>
      next ? "Ground impedance alarm asserted." : "Ground impedance alarm cleared.",
    );
    transition("alarm-latched", latchedAlarm, (next) =>
      next ? "Ground alarm latch retained." : "Ground alarm latch cleared.",
    );
  }

  function pulseItem(label, value, detail = "", quality = "Unknown") {
    const item = element("div", `pulse-item ${statusClass(quality)}`);
    item.append(element("span", "", label), element("strong", "", value));
    if (detail) item.title = detail;
    return item;
  }

  function renderPulse() {
    const memoryUsed = number("OperatingSystem.MemoryUsedBytes");
    const memoryTotal = number("OperatingSystem.MemoryTotalBytes");
    const memoryPercent =
      memoryUsed !== null && memoryTotal ? (memoryUsed / memoryTotal) * 100 : null;
    const ntp = boolean("Time.NtpSynchronized");
    const overlay = boolean("Firmware.OverlayLoaded");
    dom.pulseGrid.replaceChildren(
      pulseItem(
        "Chassis",
        number("Thermal.ChassisTemperatureCelsius") === null
          ? "—"
          : `${formatNumber(number("Thermal.ChassisTemperatureCelsius"), 1)} °C`,
        "",
        raw("Health.Thermal"),
      ),
      pulseItem(
        "CPU",
        number("OperatingSystem.CpuUtilizationPercent") === null
          ? "—"
          : `${formatNumber(number("OperatingSystem.CpuUtilizationPercent"), 1)}%`,
        `Load 1m: ${formatNumber(number("OperatingSystem.Load1Minute"), 2)}`,
        raw("Health.OperatingSystem"),
      ),
      pulseItem(
        "Memory",
        memoryPercent === null ? "—" : `${formatNumber(memoryPercent, 1)}%`,
        `${formatBytes(memoryUsed)} of ${formatBytes(memoryTotal)}`,
        raw("Health.OperatingSystem"),
      ),
      pulseItem(
        "Root disk",
        number("Storage.Filesystems.Root.UsedPercent") === null
          ? "—"
          : `${formatNumber(number("Storage.Filesystems.Root.UsedPercent"), 1)}%`,
        "",
        raw("Health.Storage"),
      ),
      pulseItem(
        "Clock",
        ntp === null ? "—" : ntp ? "NTP synced" : "Unsynced",
        "",
        raw("Health.Time"),
      ),
      pulseItem(
        "FPGA",
        overlay === null ? "—" : overlay ? "Overlay up" : "Not loaded",
        "",
        raw("Health.Firmware"),
      ),
    );
    setChip(dom.overallHealth, raw("Health.Overall") || "Unknown");
  }

  function renderHealth() {
    let good = 0;
    const nodes = HEALTH_PATHS.map(([label, path]) => {
      const value = raw(path) || "Unknown";
      const quality = statusClass(value);
      if (quality === "good") good += 1;
      const item = element("div", `health-item ${quality}`);
      item.append(element("span", "", label), element("small", "", value));
      return item;
    });
    dom.healthGrid.replaceChildren(...nodes);
    dom.healthSummary.textContent = `${good}/${HEALTH_PATHS.length} good`;
    document.body.classList.toggle(
      "has-system-warning",
      good < HEALTH_PATHS.length,
    );
  }

  function renderNetwork() {
    let carriers = 0;
    const interfaces = ["eth0", "eth1"].map((name) => {
      const prefix = `Network.Interfaces.${name}`;
      const carrier = boolean(`${prefix}.Carrier`);
      if (carrier) carriers += 1;
      const box = element("div", "network-interface");
      const top = element("div", "network-topline");
      top.append(
        element("span", "interface-name", name.toUpperCase()),
        element("span", `carrier-state ${carrier ? "up" : ""}`),
      );
      const details = element("dl");
      [
        ["State", raw(`${prefix}.OperationalState`) || "—"],
        ["Address", raw(`${prefix}.Addresses`) || "—"],
        ["MAC", raw(`${prefix}.MacAddress`) || "—"],
        [
          "Link",
          number(`${prefix}.SpeedMegabitPerSecond`) === null
            ? "—"
            : `${formatNumber(number(`${prefix}.SpeedMegabitPerSecond`), 0)} Mbit/s`,
        ],
      ].forEach(([label, value]) => {
        const row = element("div");
        row.append(element("dt", "", label), element("dd", "", value));
        details.append(row);
      });
      box.append(top, details);
      return box;
    });
    dom.networkGrid.replaceChildren(...interfaces);
    dom.networkSummary.textContent = `${carriers}/2 carrier`;
  }

  function renderServices() {
    let activeCount = 0;
    let failedCount = 0;
    const cards = state.serviceUnits.map((unit) => {
      const prefix = `Services.Units.${serviceNodeKey(unit)}`;
      const active = raw(`${prefix}.ActiveState`);
      const sub = raw(`${prefix}.SubState`);
      const result = raw(`${prefix}.Result`);
      const restarts = number(`${prefix}.RestartCount`);
      if (active === "active") activeCount += 1;
      const className =
        active === "active" ? "active" : active === "failed" ? "failed" : "unknown";
      if (active === "failed") failedCount += 1;
      const item = element("div", `service-item ${className}`);
      item.title = `${unit}\nresult: ${result || "—"}\nrestarts: ${restarts ?? "—"}`;
      item.append(
        element("strong", "", displayServiceName(unit)),
        element(
          "span",
          "",
          `${active || "unknown"} · ${sub || "—"} · ${restarts ?? "—"} restart${restarts === 1 ? "" : "s"}`,
        ),
      );
      return item;
    });
    dom.servicesGrid.replaceChildren(...cards);
    const idleCount = state.serviceUnits.length - activeCount - failedCount;
    dom.servicesSummary.textContent = failedCount
      ? `${activeCount} active · ${failedCount} failed`
      : `${activeCount} active · ${idleCount} idle`;
  }

  function buildVariableTable() {
    state.tableRows.clear();
    const fragment = document.createDocumentFragment();
    state.catalog.forEach((spec) => {
      const row = element("tr");
      row.dataset.group = spec.group;
      row.dataset.search = `${spec.label} ${spec.path} ${spec.group}`.toLowerCase();

      const nameCell = element("td", "variable-name");
      if (spec.chartable) {
        const plotButton = element("button", "variable-plot", spec.label);
        plotButton.type = "button";
        plotButton.title = `Plot ${spec.label}`;
        plotButton.addEventListener("click", () => selectCustomView(spec));
        nameCell.append(plotButton);
      } else {
        nameCell.append(element("strong", "", spec.label));
      }
      nameCell.append(element("code", "", spec.path));
      const valueCell = element("td", "value-cell", "—");
      const statusCell = element("td");
      const status = element("span", "status-code unknown", "Waiting");
      statusCell.append(status);
      const updatedCell = element("td", "updated-cell", "never");
      row.append(nameCell, valueCell, statusCell, updatedCell);
      state.tableRows.set(spec.path, {
        row,
        valueCell,
        status,
        updatedCell,
      });
      fragment.append(row);
    });
    dom.variableTable.replaceChildren(fragment);

    const groups = [...new Set(state.catalog.map((spec) => spec.group))].sort();
    groups.forEach((group) => {
      const option = element("option", "", group);
      option.value = group;
      dom.groupSelect.append(option);
    });
    filterVariables();
  }

  function updateVariableTable() {
    state.catalog.forEach((spec) => {
      const cells = state.tableRows.get(spec.path);
      if (!cells) return;
      const payload = valuePayload(spec.path);
      cells.valueCell.textContent = formatValue(spec, payload);
      cells.valueCell.title = cells.valueCell.textContent;
      const quality = statusClass(payload.status);
      cells.status.className = `status-code ${quality}`;
      cells.status.textContent = payload.status || "Unknown";
      const valueTime = payload.source_timestamp || payload.received_at;
      cells.updatedCell.textContent = relativeTime(valueTime);
      cells.updatedCell.title = formatDateTime(valueTime);
    });
  }

  function filterVariables() {
    const query = dom.variableSearch.value.trim().toLowerCase();
    const group = dom.groupSelect.value;
    let visible = 0;
    state.tableRows.forEach(({ row }) => {
      const show =
        (!query || row.dataset.search.includes(query)) &&
        (!group || row.dataset.group === group);
      row.hidden = !show;
      if (show) visible += 1;
    });
    dom.variableCount.textContent = `${visible} of ${state.catalog.length} variables`;
  }

  function buildChartPanels() {
    state.chartPanels.clear();
    const panels = plotViews().map((view) => {
      const article = element("article", "chart-panel");
      article.dataset.view = view.id;
      const titleId = `chart-title-${view.id}`;
      article.setAttribute("aria-labelledby", titleId);

      const heading = element("header", "chart-panel-header");
      const copy = element("div", "chart-panel-copy");
      const title = element("h3", "", view.label);
      title.id = titleId;
      copy.append(
        title,
        element(
          "p",
          "",
          VIEW_COPY[view.id] ||
            state.specs.get(view.paths[0])?.description ||
            `${view.label} telemetry`,
        ),
      );
      const legend = element("div", "chart-legend");
      legend.setAttribute("aria-label", `${view.label} plot legend`);
      heading.append(copy, legend);

      const shell = element("div", "chart-shell");
      const canvas = element("canvas", "trend-canvas");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", `${view.label} time-series plot`);
      const empty = element("div", "chart-empty");
      const wave = element("span", "empty-wave");
      wave.setAttribute("aria-hidden", "true");
      const emptyTitle = element("strong", "", "Building live history");
      const emptyCopy = element(
        "span",
        "",
        "The first points will appear as OPC UA samples arrive.",
      );
      empty.append(wave, emptyTitle, emptyCopy);
      const tooltip = element("div", "chart-tooltip");
      tooltip.hidden = true;
      shell.append(canvas, empty, tooltip);
      article.append(heading, shell);

      const panel = {
        article,
        shell,
        canvas,
        empty,
        emptyTitle,
        emptyCopy,
        tooltip,
        legend,
      };
      state.chartPanels.set(view.id, panel);
      canvas.addEventListener("mousemove", (event) => {
        showTooltip(event, view, panel);
      });
      canvas.addEventListener("mouseleave", hideTooltip);
      return article;
    });
    dom.chartStack.replaceChildren(...panels);
  }

  function selectCustomView(spec) {
    state.customView = {
      id: "custom",
      label: spec.label,
      unit: spec.unit,
      paths: [spec.path],
    };
    buildChartPanels();
    if (state.plotMode === "history") loadHistory();
    else scheduleDraw();
    state.chartPanels
      .get("custom")
      ?.article.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function renderChartHeading() {
    dom.trendTitle.textContent = "Correlated telemetry";
    dom.trendSubtitle.textContent =
      state.plotMode === "history"
        ? "All primary signals · retained SQLite record · shared time range"
        : "Every primary signal on one shared time range";
  }

  function ingestSample(snapshot) {
    if (state.paused || snapshot.sequence === state.lastSampleSequence) return;
    const timestamp = Date.parse(snapshot.generated_at);
    if (!Number.isFinite(timestamp) || timestamp - state.lastSampleAt < 400) return;
    const values = {};
    state.catalog.forEach((spec) => {
      if (!spec.chartable) return;
      const payload = snapshot.values?.[spec.path];
      values[spec.path] = {
        value: numericPlotValue(payload?.value),
        status: payload?.status || "BadWaitingForInitialData",
      };
    });
    state.samples.push({ time: timestamp, values });
    const rangePayload = snapshot.values?.["Measurement.ResistanceRange"];
    state.samples.at(-1).resistanceRange = rangePayload?.value || null;
    state.samples.at(-1).resistanceRangeStatus =
      rangePayload?.status || "BadWaitingForInitialData";
    state.lastSampleAt = timestamp;
    state.lastSampleSequence = snapshot.sequence;
    const cutoff = timestamp - MAX_HISTORY_SECONDS * 1000;
    while (state.samples.length && state.samples[0].time < cutoff) state.samples.shift();
  }

  function updateSnapshot(snapshot, initial = false) {
    state.snapshot = snapshot;
    ingestSample(snapshot);
    updateConnection();
    renderIdentity();
    renderMeasurement();
    renderAlarm();
    renderPulse();
    renderHealth();
    renderNetwork();
    renderServices();
    updateVariableTable();
    scheduleDraw();
    if (initial) addEvent("Initial instrument state loaded.");
  }

  function plotStatusIsUsable(status) {
    return !String(status || "").toLowerCase().startsWith("bad");
  }

  function formatAxis(value, span) {
    if (Math.abs(value) >= 1e9) return `${formatNumber(value / 1e9, 1)}G`;
    if (Math.abs(value) >= 1e6) return `${formatNumber(value / 1e6, 1)}M`;
    if (Math.abs(value) >= 1e3) return `${formatNumber(value / 1e3, 1)}k`;
    const precision = span < 1 ? 2 : span < 10 ? 1 : 0;
    return formatNumber(value, precision);
  }

  function scheduleDraw() {
    if (state.drawPending) return;
    state.drawPending = true;
    requestAnimationFrame(() => {
      state.drawPending = false;
      drawCharts();
    });
  }

  function seriesForView(view, samples) {
    const series = view.paths.map((path, index) => {
      const spec = state.specs.get(path);
      const alarmBoolean = view.id === "alarm" && path === "Alarm.Active";
      return {
        path,
        spec,
        alarmBoolean,
        color:
          alarmBoolean
            ? NORMAL_COLOR
            : view.id === "impedance" && path.includes("Threshold")
              ? THRESHOLD_COLOR
              : COLORS[index % COLORS.length],
        points: samples.map((sample) => ({
          time: sample.time,
          value: numericPlotValue(sample.values[path]?.value),
          status: sample.values[path]?.status,
          aggregate: sample.values[path]?.aggregate,
        })),
      };
    });
    if (
      view.id === "impedance" &&
      samples.some((sample) => sample.resistanceRange === "OutOfRange")
    ) {
      series.splice(1, 0, {
        path: HIGH_Z_SERIES_PATH,
        spec: {
          label: "HIGH Z",
          unit: "Ω",
          precision: 0,
        },
        color: HIGH_Z_COLOR,
        highZ: true,
        lineWidth: 2.6,
        points: samples.map((sample) => ({
          time: sample.time,
          value:
            sample.resistanceRange === "OutOfRange"
              ? state.highZFloorOhm
              : null,
          status: sample.resistanceRangeStatus,
        })),
      });
    }
    return series;
  }

  function formatSeriesValue(item, point) {
    if (
      !point ||
      !Number.isFinite(point.value) ||
      !plotStatusIsUsable(point.status)
    ) {
      return "—";
    }
    if (item.highZ) return `> ${formatNumber(state.highZFloorOhm, 0)} Ω`;
    if (item.alarmBoolean) {
      return alarmPointAsserted(point) ? "ALARM" : "NORMAL";
    }
    return `${formatNumber(point.value, item.spec?.precision ?? 1)}${
      item.spec?.unit ? ` ${item.spec.unit}` : ""
    }`;
  }

  function alarmPointAsserted(point) {
    const maximum = numericPlotValue(point?.aggregate?.maximum);
    return (maximum ?? point?.value ?? 0) > 0;
  }

  function drawAlarmTrace(context, item, x, y, start, gapLimit) {
    let previous = null;
    item.points.forEach((point) => {
      const usable =
        Number.isFinite(point.value) &&
        plotStatusIsUsable(point.status) &&
        point.time >= start;
      if (!usable) {
        previous = null;
        return;
      }
      const pointX = x(point.time);
      const pointY = y(point.value);
      const color = alarmPointAsserted(point) ? ALARM_COLOR : NORMAL_COLOR;
      if (
        previous &&
        point.time - previous.time <= gapLimit
      ) {
        const previousColor = alarmPointAsserted(previous)
          ? ALARM_COLOR
          : NORMAL_COLOR;
        context.strokeStyle = previousColor;
        context.beginPath();
        context.moveTo(x(previous.time), y(previous.value));
        context.lineTo(pointX, y(previous.value));
        context.stroke();
        if (point.value !== previous.value) {
          context.strokeStyle = color;
          context.beginPath();
          context.moveTo(pointX, y(previous.value));
          context.lineTo(pointX, pointY);
          context.stroke();
        }
      } else {
        context.fillStyle = color;
        context.beginPath();
        context.arc(pointX, pointY, 2.2, 0, Math.PI * 2);
        context.fill();
      }
      previous = point;
    });
  }

  function drawCharts() {
    plotViews().forEach((view) => {
      const panel = state.chartPanels.get(view.id);
      if (panel) drawChart(view, panel);
    });
    renderPlotState();
  }

  function drawChart(view, panel) {
    const rect = panel.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    panel.canvas.width = Math.round(rect.width * ratio);
    panel.canvas.height = Math.round(rect.height * ratio);
    const context = panel.canvas.getContext("2d");
    context.scale(ratio, ratio);

    const width = rect.width;
    const height = rect.height;
    const padding = { left: 58, right: 20, top: 22, bottom: 35 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const retained = activeSamples();
    const domain = plotDomain(retained);
    const { start, end, duration } = domain;
    const samples = retained.filter(
      (sample) => sample.time >= start && sample.time <= end,
    );
    const series = seriesForView(view, samples);
    const numeric = series.flatMap((item) =>
      item.points
        .filter(
          (point) =>
            Number.isFinite(point.value) && plotStatusIsUsable(point.status),
        )
        .map((point) => point.value),
    );

    context.clearRect(0, 0, width, height);
    panel.empty.hidden = numeric.length > 0;
    if (!numeric.length) {
      renderLegend(panel, series);
      return;
    }

    let minValue = Math.min(...numeric);
    let maxValue = Math.max(...numeric);
    if (view.id === "alarm") {
      minValue = 0;
      maxValue = 1;
    } else if (view.id === "system") {
      minValue = 0;
      maxValue = Math.max(100, maxValue);
    } else {
      const span = maxValue - minValue;
      const margin = span === 0 ? Math.max(Math.abs(maxValue) * 0.08, 1) : span * 0.1;
      minValue -= margin;
      maxValue += margin;
    }
    if (minValue === maxValue) maxValue = minValue + 1;
    const valueSpan = maxValue - minValue;
    const x = (timestamp) =>
      padding.left + ((timestamp - start) / duration) * plotWidth;
    const y = (value) =>
      padding.top + (1 - (value - minValue) / valueSpan) * plotHeight;

    context.lineWidth = 1;
    context.font =
      "10px 'JetBrains Mono', ui-monospace, SFMono-Regular, Consolas, monospace";
    context.textBaseline = "middle";
    for (let index = 0; index <= 5; index += 1) {
      const gridY = padding.top + (plotHeight / 5) * index;
      const value = maxValue - (valueSpan / 5) * index;
      context.strokeStyle = "#3f464d";
      context.beginPath();
      context.moveTo(padding.left, gridY);
      context.lineTo(width - padding.right, gridY);
      context.stroke();
      context.fillStyle = "#929aa1";
      context.textAlign = "right";
      const label = view.id === "alarm"
        ? index === 0
          ? "ALARM"
          : index === 5
            ? "NORMAL"
            : ""
        : formatAxis(value, valueSpan);
      if (label) context.fillText(label, padding.left - 9, gridY);
    }
    for (let index = 0; index <= 4; index += 1) {
      const gridX = padding.left + (plotWidth / 4) * index;
      const timestamp = start + (duration / 4) * index;
      context.strokeStyle = "#30353a";
      context.beginPath();
      context.moveTo(gridX, padding.top);
      context.lineTo(gridX, height - padding.bottom);
      context.stroke();
      context.fillStyle = "#929aa1";
      context.textAlign = index === 0 ? "left" : index === 4 ? "right" : "center";
      context.fillText(
        new Intl.DateTimeFormat(
          undefined,
          duration >= 86400 * 1000
            ? {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              }
            : {
                hour: "2-digit",
                minute: "2-digit",
                second: duration <= 300 * 1000 ? "2-digit" : undefined,
              },
        ).format(timestamp),
        gridX,
        height - 17,
      );
    }

    series.forEach((item, index) => {
      const rollupMatch = String(state.historyResolution || "").match(
        /^([0-9]+)-minute/,
      );
      const rollupMinutes = state.historyResolution === "one-minute rollup"
        ? 1
        : Number(rollupMatch?.[1] || 0);
      const slowRaw =
        state.plotMode === "history" &&
        state.historyResolution === "raw" &&
        !item.path.startsWith("Measurement.") &&
        !item.path.startsWith("Thermal.") &&
        item.path !== "Time.UptimeSeconds" &&
        item.path !== "SDR.FrameSequence";
      const gapLimit = rollupMinutes
        ? rollupMinutes * 60 * 1000 * 2.5
        : slowRaw
          ? 30000
          : 5000;
      context.strokeStyle = item.color;
      context.lineWidth = item.lineWidth || (index === 0 ? 2.2 : 1.7);
      context.lineJoin = "round";
      context.lineCap = "round";
      context.setLineDash(item.path.includes("Threshold") ? [6, 5] : []);
      if (item.alarmBoolean) {
        drawAlarmTrace(context, item, x, y, start, gapLimit);
        context.setLineDash([]);
        return;
      }
      context.beginPath();
      let drawing = false;
      let previousTime = null;
      item.points.forEach((point) => {
        const usable =
          Number.isFinite(point.value) &&
          plotStatusIsUsable(point.status) &&
          point.time >= start;
        const gap =
          previousTime !== null && point.time - previousTime > gapLimit;
        if (!usable) {
          const absentSlowSample =
            slowRaw && point.value == null && point.status == null;
          if (!absentSlowSample) {
            drawing = false;
            previousTime = null;
          }
          return;
        }
        const pointX = x(point.time);
        const pointY = y(point.value);
        if (!drawing || gap) {
          context.moveTo(pointX, pointY);
          drawing = true;
        } else {
          context.lineTo(pointX, pointY);
        }
        previousTime = point.time;
      });
      context.stroke();
      context.setLineDash([]);
    });

    const highZSeries = series.find((item) => item.highZ);
    if (highZSeries) {
      const highZY = y(state.highZFloorOhm);
      context.fillStyle = HIGH_Z_COLOR;
      context.font =
        "700 10px 'JetBrains Mono', ui-monospace, SFMono-Regular, Consolas, monospace";
      context.textAlign = "right";
      context.fillText(
        formatNumber(state.highZFloorOhm, 0),
        padding.left - 9,
        highZY,
      );
    }

    if (state.hoverTime !== null) {
      const hoverX = x(state.hoverTime);
      if (hoverX >= padding.left && hoverX <= width - padding.right) {
        context.strokeStyle = "#b8c41f";
        context.lineWidth = 1;
        context.setLineDash([3, 3]);
        context.beginPath();
        context.moveTo(hoverX, padding.top);
        context.lineTo(hoverX, height - padding.bottom);
        context.stroke();
        context.setLineDash([]);
      }
    }
    renderLegend(panel, series);
  }

  function renderLegend(panel, series) {
    const nodes = series.map((item) => {
      const container = element("span", "legend-item");
      const swatch = element("i", "legend-swatch");
      const latest = [...item.points]
        .reverse()
        .find(
          (point) =>
            Number.isFinite(point.value) && plotStatusIsUsable(point.status),
        );
      swatch.style.backgroundColor =
        item.alarmBoolean && latest
          ? alarmPointAsserted(latest)
            ? ALARM_COLOR
            : NORMAL_COLOR
          : item.color;
      const value = formatSeriesValue(item, latest);
      container.append(
        swatch,
        element("span", "", item.spec?.label || item.path),
        element("strong", "", value),
      );
      return container;
    });
    panel.legend.replaceChildren(...nodes);
  }

  function renderPlotState() {
    const highZNote = activeSamples().some(
      (sample) => sample.resistanceRange === "OutOfRange",
    )
      ? ` · HIGH Z clipped to ${formatNumber(state.highZFloorOhm, 0)} Ω`
      : "";
    if (state.plotMode === "history") {
      if (state.historyLoading) {
        dom.plotState.textContent = "Historical query in progress…";
      } else if (state.historyResolution) {
        dom.plotState.textContent =
          `History · ${state.historyResolution} · ` +
          `${state.historySamples.length} points${highZNote}`;
      }
      return;
    }
    dom.plotState.textContent = state.paused
      ? `Plot paused · ${state.samples.length} samples retained${highZNote}`
      : `Live · ${state.samples.length} samples retained in this browser${highZNote}`;
  }

  function showTooltip(event, view, panel) {
    const retained = activeSamples();
    if (!retained.length) return;
    const rect = panel.canvas.getBoundingClientRect();
    const padding = { left: 58, right: 20 };
    const plotWidth = rect.width - padding.left - padding.right;
    const fraction = Math.max(
      0,
      Math.min(1, (event.clientX - rect.left - padding.left) / plotWidth),
    );
    const domain = plotDomain(retained);
    const target = domain.start + fraction * domain.duration;
    const candidate = retained.reduce((nearest, sample) =>
      Math.abs(sample.time - target) < Math.abs(nearest.time - target) ? sample : nearest,
    );
    state.hoverTime = candidate.time;
    state.hoverViewId = view.id;
    state.chartPanels.forEach((candidatePanel, viewId) => {
      if (viewId !== view.id) candidatePanel.tooltip.hidden = true;
    });

    const content = document.createDocumentFragment();
    content.append(element("time", "", formatDateTime(candidate.time)));
    const visibleSamples = retained.filter(
      (sample) => sample.time >= domain.start && sample.time <= domain.end,
    );
    seriesForView(view, visibleSamples).forEach((item) => {
      const payload = item.highZ
        ? {
            value:
              candidate.resistanceRange === "OutOfRange"
                ? state.highZFloorOhm
                : null,
            status: candidate.resistanceRangeStatus,
          }
        : candidate.values[item.path];
      const row = element("div", "tooltip-row");
      const label = element("span");
      const dot = element("i");
      dot.style.backgroundColor = item.color;
      label.append(dot, document.createTextNode(item.spec?.label || item.path));
      row.append(label, element("strong", "", formatSeriesValue(item, payload)));
      content.append(row);
    });
    panel.tooltip.replaceChildren(content);
    panel.tooltip.hidden = false;
    const tooltipWidth = 210;
    const localX = event.clientX - rect.left;
    panel.tooltip.style.left = `${Math.min(rect.width - tooltipWidth, Math.max(8, localX + 12))}px`;
    panel.tooltip.style.top = `${Math.max(8, event.clientY - rect.top - 30)}px`;
    scheduleDraw();
  }

  function hideTooltip() {
    state.hoverTime = null;
    state.hoverViewId = null;
    state.chartPanels.forEach((panel) => {
      panel.tooltip.hidden = true;
    });
    scheduleDraw();
  }

  function exportCsv() {
    const paths = plotPaths();
    if (!paths.length) return;
    if (state.plotMode === "history") {
      try {
        const { start, end } = historyBounds();
        const parameters = new URLSearchParams({
          series: paths.join(","),
          from: new Date(start).toISOString(),
          to: new Date(end).toISOString(),
        });
        const link = document.createElement("a");
        link.href = `/api/history/export.csv?${parameters}`;
        link.download =
          `gizmo-history-all-${new Date().toISOString().replaceAll(":", "-")}.csv`;
        link.click();
        addEvent("Requested historical all-series CSV export.");
      } catch (error) {
        addEvent(`Historical export failed: ${error.message}`, "error");
      }
      return;
    }
    const latest = state.samples.at(-1)?.time || Date.now();
    const cutoff = latest - state.windowSeconds * 1000;
    const rows = state.samples
      .filter((sample) => sample.time >= cutoff)
      .map((sample) => [
        new Date(sample.time).toISOString(),
        ...paths.map((path) => sample.values[path]?.value ?? ""),
        sample.resistanceRange ?? "",
        ...paths.map((path) => sample.values[path]?.status ?? ""),
        sample.resistanceRangeStatus ?? "",
      ]);
    const headers = [
      "timestamp_utc",
      ...paths,
      "Measurement.ResistanceRange",
      ...paths.map((path) => `${path}.StatusCode`),
      "Measurement.ResistanceRange.StatusCode",
    ];
    const csv = [headers, ...rows]
      .map((row) =>
        row
          .map((field) => {
            const text = String(field);
            return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
          })
          .join(","),
      )
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `gizmo-all-${new Date().toISOString().replaceAll(":", "-")}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    addEvent(`Exported ${rows.length} all-series samples to CSV.`);
  }

  function togglePause() {
    if (state.plotMode === "history") {
      loadHistory();
      return;
    }
    state.paused = !state.paused;
    dom.pauseButton.textContent = state.paused ? "Resume plot" : "Pause plot";
    dom.pauseButton.classList.toggle("is-active", state.paused);
    addEvent(state.paused ? "Live plot paused." : "Live plot resumed.");
    scheduleDraw();
  }

  function updateClock() {
    dom.headerClock.textContent = new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
    if (state.snapshot) {
      updateConnection();
      state.tableRows.forEach(({ updatedCell }, path) => {
        const payload = valuePayload(path);
        updatedCell.textContent = relativeTime(
          payload.source_timestamp || payload.received_at,
        );
      });
    }
  }

  async function loadCatalog() {
    const response = await fetch("/api/catalog", { cache: "no-store" });
    if (!response.ok) throw new Error(`catalog request returned ${response.status}`);
    const catalog = await response.json();
    state.catalog = catalog.variables;
    state.specs = new Map(state.catalog.map((spec) => [spec.path, spec]));
    state.views = catalog.views;
    state.serviceUnits = catalog.service_units;
    state.historyAvailable = Boolean(catalog.history_available);
    state.highZFloorOhm =
      Number(catalog.resistance_high_z_floor_ohm) || 500;
    const historyOption = dom.modeSelect.querySelector('option[value="history"]');
    if (historyOption) {
      historyOption.textContent = state.historyAvailable
        ? "History"
        : "History unavailable";
    }
    buildVariableTable();
    buildChartPanels();
    renderChartHeading();
  }

  async function loadInitialState() {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`state request returned ${response.status}`);
    updateSnapshot(await response.json(), true);
  }

  function openStream() {
    const stream = new EventSource("/api/stream");
    stream.onopen = () => {
      state.streamOpen = true;
      updateConnection();
    };
    stream.addEventListener("sample", (event) => {
      state.streamOpen = true;
      try {
        updateSnapshot(JSON.parse(event.data));
      } catch (error) {
        addEvent(`Rejected malformed live sample: ${error.message}`, "error");
      }
    });
    stream.onerror = () => {
      state.streamOpen = false;
      updateConnection();
    };
  }

  function bindEvents() {
    document
      .querySelector('.section-nav a[href="#variables"]')
      ?.addEventListener("click", () => {
        dom.variableDetails.open = true;
      });
    dom.modeSelect.addEventListener("change", () => {
      setPlotMode(dom.modeSelect.value);
    });
    dom.rangeSelect.addEventListener("change", () => {
      updateRangeOptions();
      if (state.plotMode === "history") loadHistory();
      else scheduleDraw();
    });
    dom.historyFrom.addEventListener("change", () => {
      if (state.plotMode === "history") loadHistory();
    });
    dom.historyTo.addEventListener("change", () => {
      if (state.plotMode === "history") loadHistory();
    });
    dom.pauseButton.addEventListener("click", togglePause);
    dom.exportButton.addEventListener("click", exportCsv);
    dom.variableSearch.addEventListener("input", filterVariables);
    dom.groupSelect.addEventListener("change", filterVariables);
    dom.clearEvents.addEventListener("click", () => {
      dom.eventList.replaceChildren();
      addEvent("Session log cleared.");
    });
    window.addEventListener("resize", scheduleDraw);
  }

  async function start() {
    initializeHistoryInputs();
    updateRangeOptions();
    bindEvents();
    updateClock();
    setInterval(updateClock, 1000);
    try {
      await loadCatalog();
      await loadInitialState();
      openStream();
    } catch (error) {
      state.streamOpen = false;
      dom.connectionDot.className = "status-orb status-bad";
      dom.connectionLabel.textContent = "Dashboard unavailable";
      dom.connectionDetail.textContent = error.message;
      addEvent(`Dashboard startup failed: ${error.message}`, "error");
    }
  }

  start();
})();
