"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.body.dataset.csrfToken;
  const preview = document.body.dataset.preview === "true";
  let records = [];
  let uploadId = null;
  let currentView = "documents";
  let statusPending = false;
  let testPending = false;

  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value == null ? "" : String(value);
    if (className) node.className = className;
    return node;
  };
  function notice(element, message, type = "") {
    element.className = (element.classList.contains("inline-notice") ? "inline-notice" : "notice") + (type ? ` ${type}` : "");
    element.textContent = message;
    element.hidden = !message;
  }
  function setServer(value) {
    if (value) $("server-name").textContent = value;
  }
  function sizeLabel(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
    return `${(n / 1024 ** 3).toFixed(2)} GB`;
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), path === "/api/upload" ? 120000 : 25000);
    try {
      const response = await fetch(path, { credentials: "same-origin", signal: controller.signal, ...options });
      if (response.status === 401) {
        window.location.assign("/login");
        throw new Error("Your session has expired. Please sign in again.");
      }
      let data;
      try { data = await response.json(); } catch { throw new Error("The server returned an unexpected response. Please try again."); }
      if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status}).`);
      setServer(data.server);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Check connectivity and try again.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function downloadIcon() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4");
    svg.append(path);
    return svg;
  }
  function renderRecords() {
    const search = $("record-search").value.trim().toLowerCase();
    const matching = records.filter((row) => [row.filename, row.department, row.sha256, row.id].some((value) => String(value || "").toLowerCase().includes(search)));
    const body = $("records-body");
    body.replaceChildren();
    $("record-count").textContent = search ? `${matching.length} matches` : `${records.length} records`;
    if (!matching.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td"); cell.colSpan = 6;
      const empty = text("div", "", "empty-state");
      empty.append(text("span", "▤", "empty-glyph"), text("strong", search ? "No matching documents" : "Your records start here"), text("span", search ? "Try a different filename, department or checksum." : "Upload your first document to create a shared record."));
      cell.append(empty); row.append(cell); body.append(row); return;
    }
    matching.forEach((record) => {
      const row = document.createElement("tr");
      const fileCell = document.createElement("td");
      const doc = text("div", "", "document-cell");
      const description = text("div", "", "document-description");
      const name = text("span", record.filename, "document-name"); name.title = record.filename || "";
      description.append(name, text("span", String(record.id || "").slice(0, 12), "document-id"));
      doc.append(text("span", "▤", "document-glyph"), description); fileCell.append(doc);
      const department = document.createElement("td");
      const departmentClass = { "Human Resources": "hr", "Cyber Security": "cyber", "Executive General": "executive" }[record.department] || "";
      department.append(text("span", record.department, `department-tag ${departmentClass}`));
      const date = text("td", "", "date-cell");
      const parsed = new Date(record.created_at);
      if (!Number.isNaN(parsed.getTime())) date.append(text("div", parsed.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" })), text("span", parsed.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })));
      else date.textContent = record.created_at || "—";
      const bytes = text("td", sizeLabel(record.size), "size-cell");
      const hash = document.createElement("td");
      const shortHash = text("span", `${String(record.sha256 || "").slice(0, 10)}…`, "checksum"); shortHash.title = record.sha256 || ""; hash.append(shortHash);
      const action = document.createElement("td");
      const link = document.createElement("a"); link.className = "download-link"; link.href = `/download/${encodeURIComponent(record.id)}`;
      link.setAttribute("aria-label", `Download ${record.filename || "document"}`); link.title = "Download document"; link.append(downloadIcon()); action.append(link);
      row.append(fileCell, department, date, bytes, hash, action); body.append(row);
    });
  }
  async function loadRecords() {
    const button = $("refresh-records"); button.disabled = true;
    try {
      const data = await api("/api/records");
      records = Array.isArray(data.records) ? data.records : [];
      const summary = data.summary || {};
      $("stat-count").textContent = Number(summary.count == null ? records.length : summary.count).toLocaleString();
      $("stat-bytes").textContent = sizeLabel(summary.total_bytes == null ? records.reduce((total, row) => total + (Number(row.size) || 0), 0) : summary.total_bytes);
      const departments = summary.departments;
      $("stat-departments").textContent = Array.isArray(departments) ? departments.length : departments == null ? new Set(records.map((row) => row.department)).size : departments;
      $("nav-count").textContent = summary.count == null ? records.length : summary.count;
      renderRecords();
      notice($("global-notice"), "");
    } catch (error) {
      notice($("global-notice"), `Could not load records: ${error.message}`, "error");
      $("record-count").textContent = "Unavailable";
      if (!records.length) {
        const body = $("records-body"); body.replaceChildren();
        const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 6;
        const empty = text("div", "", "empty-state"); empty.append(text("strong", "Records could not be loaded"), text("span", "Check the operator panel, then refresh this page."));
        cell.append(empty); row.append(cell); body.append(row);
      }
    } finally { button.disabled = false; }
  }
  function selectedFile() {
    const file = $("document").files[0];
    uploadId = null;
    $("file-name").textContent = file ? file.name : "Choose a document";
    $("file-hint").textContent = file ? sizeLabel(file.size) : "or drag and drop it here";
    notice($("upload-notice"), file && file.size > 16 * 1024 ** 2 ? "This file is larger than 16 MB. Please choose a smaller document." : "", "error");
  }
  $("document").addEventListener("change", selectedFile);
  ["dragenter", "dragover"].forEach((event) => $("file-drop").addEventListener(event, (e) => { e.preventDefault(); $("file-drop").classList.add("drag-over"); }));
  ["dragleave", "drop"].forEach((event) => $("file-drop").addEventListener(event, (e) => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); }));
  $("file-drop").addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) { $("document").files = event.dataTransfer.files; selectedFile(); }
  });
  $("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("document").files[0];
    if (!file) return notice($("upload-notice"), "Choose a document first.", "error");
    if (file.size > 16 * 1024 ** 2) return notice($("upload-notice"), "This file is larger than 16 MB.", "error");
    if (!uploadId) uploadId = crypto.randomUUID();
    const payload = new FormData(); payload.append("document", file); payload.append("department", $("department").value); payload.append("upload_id", uploadId);
    const button = $("upload-button"); const label = button.querySelector("span"); button.disabled = true; label.textContent = "Processing upload…";
    notice($("upload-notice"), "Uploading the file and saving its record. Keep this page open.");
    try {
      const result = await api("/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: payload });
      $("upload-form").reset(); selectedFile();
      notice($("upload-notice"), result.message || "Document uploaded and its record saved.", "success");
      await loadRecords();
    } catch (error) { notice($("upload-notice"), error.message, "error"); }
    finally { button.disabled = false; label.textContent = "Upload document"; }
  });
  $("record-search").addEventListener("input", renderRecords);
  $("refresh-records").addEventListener("click", loadRecords);

  function badge(element, label, state = "neutral") { element.className = `badge ${state}`; element.textContent = label; }
  function service(kind, data) {
    const known = data && typeof data.ok === "boolean";
    badge($(`${kind}-badge`), known ? data.ok ? "Connected" : "Check needed" : "Unavailable", known ? data.ok ? "success" : "error" : "neutral");
    $(`${kind}-message`).textContent = data && data.message ? data.message : "This check is not available. Confirm the instance permissions and service connectivity.";
  }
  function listStatus(container, rows, emptyMessage, type) {
    container.replaceChildren();
    if (!rows.length) { container.append(text("p", emptyMessage, "empty-copy")); return; }
    rows.forEach((item) => {
      const state = String(item.state || "unknown");
      const tone = type === "target" ? state === "healthy" ? "success" : ["initial", "draining", "unused"].includes(state) ? "warning" : state === "unhealthy" ? "error" : "neutral" : state === "OK" ? "success" : state === "ALARM" ? "error" : "warning";
      const row = text("div", "", "status-row"), top = text("div", "", "status-row-top");
      top.append(text("span", item.name || item.id || "Unknown", "status-name"), text("span", state.replaceAll("_", " "), `badge ${tone}`));
      row.append(top); if (item.reason) row.append(text("p", item.reason)); container.append(row);
    });
  }
  function setDashboard(url) {
    const link = $("dashboard-link");
    try { const parsed = new URL(url); if (parsed.protocol !== "https:") throw new Error("Invalid protocol"); link.href = parsed.href; link.hidden = false; }
    catch { link.hidden = true; }
  }
  setDashboard(document.body.dataset.dashboardUrl);
  async function loadStatus() {
    if (statusPending) return;
    statusPending = true; $("refresh-status").disabled = true;
    try {
      const data = await api("/api/status");
      service("database", data.database); service("storage", data.storage);
      const notifications = data.notifications || {};
      const confirmed = notifications.confirmed;
      badge($("email-badge"), confirmed === true ? "Subscription confirmed" : confirmed === false ? "Confirmation needed" : "Not verified", confirmed === true ? "success" : confirmed === false ? "warning" : "neutral");
      $("email-message").textContent = notifications.message || (confirmed === true ? "SNS reports a confirmed subscription. Trigger an alarm and check the recipient inbox to verify delivery." : "Confirm the Amazon SNS subscription email. Delivery has not been verified by this panel.");
      listStatus($("target-list"), Array.isArray(data.targets) ? data.targets : [], "Target health is unavailable. Check the AWS console and instance permissions.", "target");
      listStatus($("alarm-list"), Array.isArray(data.alarms) ? data.alarms : [], "No alarm state was returned. Check the CloudWatch console and instance permissions.", "alarm");
      notice($("aws-warning"), data.aws_error ? `Some AWS monitoring information is unavailable: ${data.aws_error}` : "", "warning");
      $("ops-updated").textContent = `${preview || data.preview ? "Preview data · " : ""}Last checked ${new Date().toLocaleTimeString()}`;
      if (data.dashboard_url) setDashboard(data.dashboard_url);
      if (data.test && data.test.active) {
        const end = data.test.until ? new Date(Number(data.test.until) * 1000).toLocaleTimeString() : "the configured recovery time";
        notice($("test-notice"), `This server is in the temporary test state. It will recover automatically at ${end}. A different request may be handled by the other server.`, "warning");
      }
    } catch (error) {
      notice($("aws-warning"), `Status could not be checked: ${error.message}`, "warning");
      $("ops-updated").textContent = "The latest status request failed. Previously displayed values may be out of date.";
    } finally { statusPending = false; $("refresh-status").disabled = false; }
  }
  document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => {
    currentView = button.dataset.view;
    document.querySelectorAll(".nav-button").forEach((nav) => { const active = nav === button; nav.classList.toggle("active", active); if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current"); });
    document.querySelectorAll(".page-view").forEach((view) => { view.hidden = view.id !== `view-${currentView}`; });
    $("breadcrumb-page").textContent = currentView === "operations" ? "Operator panel" : "Documents";
    document.title = `Corporate Gateway · ${currentView === "operations" ? "Operations" : "Documents"}`;
    if (currentView === "operations") loadStatus();
  }));
  $("refresh-status").addEventListener("click", loadStatus);
  $("failure-test").addEventListener("click", async () => {
    if (testPending) return;
    const agreed = window.confirm("Start a four-minute availability test on the server handling this request? It will temporarily fail its health check. Requests may fail briefly before the load balancer routes to the other server. It will recover automatically. Only continue after confirming the SNS subscription and saving your healthy baseline evidence.");
    if (!agreed) return;
    testPending = true; $("failure-test").disabled = true;
    try {
      const data = await api("/api/test/failure", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ seconds: 240 }) });
      notice($("test-notice"), `${data.message || "Temporary test started."} Test server: ${data.server || "see the server badge"}. Check CloudWatch and the recipient inbox. Recovery is automatic.`, "warning");
      loadStatus();
    } catch (error) { notice($("test-notice"), error.message, "error"); }
    finally { testPending = false; $("failure-test").disabled = false; }
  });
  setInterval(() => { if (currentView === "operations" && !document.hidden) loadStatus(); }, 15000);
  loadRecords();
})();
