// Shondhan AI police dashboard — plain JS, no build step.
const PERMS = {
  admin: ["case:create", "case:view", "case:close", "alert:review", "scan:run", "audit:view"],
  officer: ["case:view", "case:close", "alert:review", "scan:run"],
  registrar: ["case:create", "case:view"],
};
const CAMERA_TYPES = {
  toll_plaza: "Toll plaza", railway_station: "Railway station", bus_terminal: "Bus terminal",
  launch_ghat: "Launch ghat", land_port: "Land port", highway: "Highway checkpoint",
};
let me = null;
let cameras = [];
let timers = [];
let liveStream = null;

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const can = (p) => me && PERMS[me.role]?.includes(p);
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const pct = (x) => (x == null ? "—" : `${(x * 100).toFixed(1)}%`);
const scoreClass = (s) => (s >= 0.55 ? "hi" : "mid");

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Not logged in"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}

// ------------------------------------------------------------------ auth
function showLogin() { $("#app").hidden = true; $("#login").hidden = false; }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    me = await post("/api/login", { username: f.get("username"), password: f.get("password") });
    $("#login-error").textContent = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await post("/api/logout", {}); me = null; showLogin(); });

async function start() {
  $("#login").hidden = true; $("#app").hidden = false;
  $("#who").textContent = `${me.name} · ${me.role}`;
  document.querySelectorAll("[data-perm]").forEach((a) => (a.hidden = !can(a.dataset.perm)));
  cameras = await api("/api/cameras");
  if (!location.hash) location.hash = "#alerts";
  route();
  refreshBadge();
  setInterval(refreshBadge, 5000);
}

async function refreshBadge() {
  if (!me) return;
  try {
    const s = await api("/api/stats");
    const b = $("#pending-badge");
    b.hidden = !s.alerts_pending; b.textContent = s.alerts_pending;
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ router
window.addEventListener("hashchange", route);
function route() {
  timers.forEach(clearInterval); timers = [];
  stopLive();
  const [name, arg] = location.hash.slice(1).split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${name}`));
  const views = { alerts: viewAlerts, cases: viewCases, case: viewCase, "new-case": viewNewCase, scan: viewScan, live: viewLive, audit: viewAudit };
  (views[name] || viewAlerts)(arg);
}

function makeMap(el, center = [23.7, 90.35], zoom = 7) {
  const map = L.map(el).setView(center, zoom);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap", maxZoom: 18 }).addTo(map);
  return map;
}
function cameraMarkers(map) {
  cameras.forEach((c) => L.circleMarker([c.lat, c.lon], { radius: 5, color: "#0b6e4f", weight: 2, fillOpacity: 0.6 })
    .bindTooltip(`${c.name}<br><small>${CAMERA_TYPES[c.type] || c.type}</small>`).addTo(map));
}

// ------------------------------------------------------------------ alerts / dashboard
async function viewAlerts(selectedId) {
  const v = $("#view");
  v.innerHTML = `
    <div class="stats" id="stats"></div>
    <div class="grid2">
      <div class="panel"><h2>Alert queue</h2><p class="muted small">Every possible match needs a trained officer's decision. The AI never acts alone.</p>
        <div class="list" id="alert-list"></div></div>
      <div class="stack"><div id="alert-detail"></div><div class="panel"><h2>Camera network &amp; sightings</h2><div id="map" class="map"></div></div></div>
    </div>`;
  const map = makeMap($("#map"));
  cameraMarkers(map);
  const layer = L.layerGroup().addTo(map);

  async function load() {
    const [stats, alerts] = await Promise.all([api("/api/stats"), api("/api/alerts")]);
    $("#stats").innerHTML = [
      ["Active cases", stats.active_cases], ["Children found", stats.found_cases],
      ["Pending alerts", stats.alerts_pending], ["Confirmed sightings", stats.alerts_confirmed],
      ["False alert rate", pct(stats.false_alert_rate)],
      ["Median time to first alert", stats.median_minutes_to_first_alert == null ? "—" : `${stats.median_minutes_to_first_alert} min`],
      ["Faces scanned", stats.faces_scanned.toLocaleString()], ["Discarded (not stored)", stats.faces_discarded.toLocaleString()],
    ].map(([k, val]) => `<div class="stat"><b>${esc(val)}</b><span>${esc(k)}</span></div>`).join("");

    $("#alert-list").innerHTML = alerts.length ? alerts.map((a) => `
      <div class="item ${a.id == selectedId ? "sel" : ""}" data-id="${a.id}">
        <img src="/api/alerts/${a.id}/face" onerror="this.style.visibility='hidden'" alt="">
        <div class="meta">
          <div class="row" style="gap:6px"><b>${esc(a.case_code)}</b><span class="pill ${a.status}">${a.status}</span></div>
          <div class="small">${esc(a.camera_name)}</div>
          <div class="small muted">${fmtTime(a.seen_at)} · score <span class="score ${scoreClass(a.score)}">${a.score.toFixed(2)}</span> · ${a.hits} frames</div>
        </div>
      </div>`).join("") : `<p class="muted">No alerts yet. Register a case and scan footage.</p>`;
    document.querySelectorAll("#alert-list .item").forEach((el) => el.addEventListener("click", () => { location.hash = `#alerts/${el.dataset.id}`; }));

    layer.clearLayers();
    alerts.filter((a) => a.status !== "rejected").forEach((a) => {
      L.circleMarker([a.lat, a.lon], { radius: 10, color: a.status === "confirmed" ? "#c0392b" : "#d68910", weight: 3, fillOpacity: 0.3 })
        .bindTooltip(`${a.case_code} · ${a.status}<br>${a.camera_name}`).addTo(layer);
    });
  }
  await load();
  timers.push(setInterval(load, 5000));
  if (selectedId) renderAlertDetail(selectedId, map);
  else $("#alert-detail").innerHTML = `<div class="panel muted">Select an alert to review it.</div>`;
}

async function renderAlertDetail(id, map) {
  const box = $("#alert-detail");
  let a;
  try { a = await api(`/api/alerts/${id}`); } catch (e) { box.innerHTML = `<div class="panel error">${esc(e.message)}</div>`; return; }
  map?.setView([a.lat, a.lon], 10);
  const matchedIsPredicted = a.matched_kind === "predicted";
  box.innerHTML = `
    <div class="panel">
      <div class="row" style="justify-content:space-between"><h2>Alert #${a.id} · ${esc(a.case_code)}</h2><span class="pill ${a.status}">${a.status}</span></div>
      ${a.snapshot_blob ? `<img class="snapshot" src="/api/alerts/${a.id}/snapshot" alt="Camera snapshot">` : `<p class="muted">Snapshot deleted.</p>`}
      <h3 style="margin-top:14px">Compare faces</h3>
      <div class="compare">
        <figure><img src="/api/alerts/${a.id}/face" onerror="this.style.visibility='hidden'" alt=""><figcaption>Camera face</figcaption></figure>
        <figure><img src="/api/photos/${a.matched_photo_id}/image" onerror="this.style.visibility='hidden'" alt="">
          <figcaption>${matchedIsPredicted ? '<span class="pill predicted">Predicted</span> ' : ""}Best match: ${esc(a.matched_label || "")}</figcaption></figure>
        <figure><img src="/api/photos/${a.real_photos[0]}/image" onerror="this.style.visibility='hidden'" alt=""><figcaption><span class="pill real">Real</span> Registered photo</figcaption></figure>
      </div>
      <dl class="kv" style="margin-top:14px">
        <dt>Confidence</dt><dd><span class="score ${scoreClass(a.score)}">${a.score.toFixed(3)}</span>
          ${a.real_score > 0 ? `<span class="muted"> (vs real photos: ${a.real_score.toFixed(3)})</span>` : ""}</dd>
        <dt>Seen in</dt><dd>${a.hits} frame(s)</dd>
        <dt>Location</dt><dd>${esc(a.camera_name)}, ${esc(a.district)} <span class="muted">(${CAMERA_TYPES[a.camera_type] || a.camera_type})</span></dd>
        <dt>Time</dt><dd>${fmtTime(a.seen_at)}</dd>
        <dt>Vehicle</dt><dd>${esc(a.vehicle_plate || "—")} ${a.direction ? `· heading ${esc(a.direction)}` : ""}</dd>
        <dt>Child</dt><dd>${esc(a.child_name || "—")} · age ${esc(a.age_at_missing ?? "?")} when missing since ${esc(a.missing_since)} · <a href="#case/${a.case_id}">open case</a></dd>
        ${a.reviewed_by ? `<dt>Reviewed</dt><dd>${esc(a.reviewed_by)} · ${fmtTime(a.reviewed_at)} ${a.review_note ? "· " + esc(a.review_note) : ""}</dd>` : ""}
      </dl>
      ${a.status === "pending" && can("alert:review") ? `
        <div class="notice" style="margin-top:14px">Check the face carefully against the <b>real</b> photos. Predicted looks are AI-generated guesses, not evidence.</div>
        <label style="margin-top:10px">Officer note <input id="review-note" placeholder="Reason for decision (logged)"></label>
        <div class="row" style="margin-top:10px">
          <button class="btn primary" id="btn-confirm">Confirm match &amp; alert police</button>
          <button class="btn" id="btn-reject">Not a match (reject)</button>
        </div>` : ""}
      ${a.notifications.length ? `<h3 style="margin-top:14px">Notifications sent</h3>
        <table><tr><th>Recipient</th><th>Channel</th><th>Message</th></tr>
        ${a.notifications.map((n) => `<tr><td>${esc(n.recipient)}</td><td>${esc(n.channel)}</td><td class="small">${esc(n.message)}</td></tr>`).join("")}</table>` : ""}
    </div>`;
  const review = async (decision) => {
    try {
      const r = await post(`/api/alerts/${id}/review`, { decision, note: $("#review-note").value });
      toast(decision === "confirm" ? `Confirmed. ${r.notified.length} recipients notified.` : "Rejected. Snapshot deleted.");
      renderAlertDetail(id, map); refreshBadge();
    } catch (e) { toast(e.message); }
  };
  $("#btn-confirm")?.addEventListener("click", () => review("confirm"));
  $("#btn-reject")?.addEventListener("click", () => review("reject"));
}

// ------------------------------------------------------------------ cases
async function viewCases() {
  const list = await api("/api/cases");
  $("#view").innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:12px"><h1>Missing-child cases</h1>
      ${can("case:create") ? `<a class="btn primary" href="#new-case">Register case</a>` : ""}</div>
    <div class="cards">${list.map((c) => `
      <div class="card" data-id="${c.id}">
        ${c.cover_photo ? `<img src="/api/photos/${c.cover_photo}/image" alt="">` : `<img alt="">`}
        <div class="body">
          <div class="row" style="justify-content:space-between"><b>${esc(c.case_code)}</b><span class="pill ${c.status}">${c.status}</span></div>
          <div>${esc(c.child_name || "(redacted)")}</div>
          <div class="small muted">Missing since ${esc(c.missing_since)} · ${esc(c.police_station)}</div>
          <div class="small muted">${c.real_photos} real · ${c.predicted_looks} predicted looks · ${c.sightings} sightings</div>
        </div>
      </div>`).join("") || `<p class="muted">No cases yet.</p>`}</div>`;
  document.querySelectorAll(".card").forEach((el) => el.addEventListener("click", () => (location.hash = `#case/${el.dataset.id}`)));
}

async function viewCase(id) {
  const c = await api(`/api/cases/${id}`);
  const real = c.photos.filter((p) => p.kind === "real");
  const pred = c.photos.filter((p) => p.kind === "predicted");
  $("#view").innerHTML = `
    <p><a href="#cases">← All cases</a></p>
    <div class="grid2">
      <div class="stack">
        <div class="panel">
          <div class="row" style="justify-content:space-between"><h1>${esc(c.case_code)}</h1><span class="pill ${c.status}">${c.status}</span></div>
          <dl class="kv">
            <dt>Child</dt><dd>${esc(c.child_name || "(redacted)")} ${c.gender ? "· " + esc(c.gender) : ""}</dd>
            <dt>Age when missing</dt><dd>${esc(c.age_at_missing ?? "—")}</dd>
            <dt>Missing since</dt><dd>${esc(c.missing_since)}</dd>
            <dt>Last seen</dt><dd>${esc(c.last_seen_location || "—")}</dd>
            <dt>GD / case no.</dt><dd>${esc(c.gd_number)} · ${esc(c.police_station)}</dd>
            <dt>Guardian</dt><dd>${esc(c.guardian_name || "—")} ${c.guardian_phone ? "· " + esc(c.guardian_phone) : ""} · consent ✓</dd>
            <dt>Case officer</dt><dd>${esc(c.case_officer || "—")} ${c.case_officer_phone ? "· " + esc(c.case_officer_phone) : ""}</dd>
            <dt>Registered</dt><dd>${fmtTime(c.created_at)} by ${esc(c.created_by)}</dd>
            ${c.closed_at ? `<dt>Closed</dt><dd>${fmtTime(c.closed_at)}</dd>` : ""}
          </dl>
          ${c.age_backend_note ? `<div class="notice" style="margin-top:12px">${esc(c.age_backend_note)}</div>` : ""}
          ${c.status === "active" && can("case:close") ? `
            <div class="privacy" style="margin-top:12px">Closing a case <b>permanently deletes</b> all photos, predicted looks, face signatures and snapshots for this child.</div>
            <div class="row" style="margin-top:10px">
              <button class="btn primary" id="close-found">Child found — close case</button>
              <button class="btn" id="close-withdrawn">Withdraw case</button>
            </div>
            <p id="close-confirm" class="small" hidden></p>` : ""}
        </div>
        <div class="panel"><h2>Sightings</h2>
          ${c.alerts.length ? `<table><tr><th>When</th><th>Camera</th><th>Score</th><th>Status</th></tr>${c.alerts.map((a) => `
            <tr><td><a href="#alerts/${a.id}">${fmtTime(a.seen_at)}</a></td><td>${esc(a.camera_name)}${a.vehicle_plate ? `<br><span class="small muted">${esc(a.vehicle_plate)}</span>` : ""}</td>
            <td class="score ${scoreClass(a.score)}">${a.score.toFixed(2)}</td><td><span class="pill ${a.status}">${a.status}</span></td></tr>`).join("")}</table>`
            : `<p class="muted">No sightings yet.</p>`}
        </div>
      </div>
      <div class="stack">
        <div class="panel"><h2>Route &amp; next checkpoints</h2><div id="route-info" class="small muted"></div><div id="map" class="map" style="height:340px"></div></div>
        <div class="panel"><h2>Real photos (anchors)</h2>
          <div class="gallery">${real.map((p) => `<figure><img src="/api/photos/${p.id}/image" alt=""><span class="pill real tag">Real</span><figcaption>${esc(p.label)}</figcaption></figure>`).join("") || `<p class="muted">Deleted.</p>`}</div>
        </div>
        <div class="panel"><h2>Predicted looks (${pred.length})</h2>
          <p class="small muted">AI-generated, used only inside the matching system. Never publish these as real photos.</p>
          <div class="gallery">${pred.map((p) => `<figure><img loading="lazy" src="/api/photos/${p.id}/image" alt=""><span class="pill predicted tag">Predicted</span><figcaption>${esc(p.label.replace("PREDICTED · ", ""))}</figcaption></figure>`).join("")}</div>
        </div>
      </div>
    </div>`;

  const map = makeMap($("#map"));
  cameraMarkers(map);
  const r = await api(`/api/cases/${id}/route`);
  if (r.sightings.length) {
    const pts = r.sightings.map((s) => [s.lat, s.lon]);
    L.polyline(pts, { color: "#c0392b", weight: 4 }).addTo(map);
    r.sightings.forEach((s, i) => L.circleMarker([s.lat, s.lon], { radius: 9, color: "#c0392b", fillOpacity: 0.8 })
      .bindTooltip(`#${i + 1} ${s.name}<br>${fmtTime(s.seen_at)}${s.vehicle_plate ? "<br>" + esc(s.vehicle_plate) : ""}`).addTo(map));
    r.next_checkpoints.forEach((n) => L.circleMarker([n.lat, n.lon], { radius: 11, color: "#d68910", dashArray: "4", fillOpacity: 0.15 })
      .bindTooltip(`Suggested interception: ${n.name} (${n.distance_km} km)`).addTo(map));
    map.fitBounds(L.latLngBounds([...pts, ...r.next_checkpoints.map((n) => [n.lat, n.lon])]).pad(0.3));
    $("#route-info").innerHTML = `${r.sightings.length} confirmed sighting(s). Suggested next checkpoints (${esc(r.method)}): ` +
      (r.next_checkpoints.map((n) => `<b>${esc(n.name)}</b> ${n.distance_km} km`).join(", ") || "none in range");
  } else {
    $("#route-info").textContent = "Route appears after the first confirmed sighting.";
  }

  const close = (outcome) => {
    const p = $("#close-confirm");
    p.hidden = false;
    p.innerHTML = `Type the case code <b>${esc(c.case_code)}</b> to confirm: <input id="close-code" style="width:140px"> <button class="btn danger small" id="close-go">Delete data &amp; close</button>`;
    $("#close-go").onclick = async () => {
      if ($("#close-code").value.trim() !== c.case_code) return toast("Case code does not match");
      try { await post(`/api/cases/${id}/close`, { outcome }); toast("Case closed. All biometric data deleted."); viewCase(id); }
      catch (e) { toast(e.message); }
    };
  };
  $("#close-found")?.addEventListener("click", () => close("found"));
  $("#close-withdrawn")?.addEventListener("click", () => close("withdrawn"));
}

function viewNewCase() {
  const today = new Date().toISOString().slice(0, 10);
  $("#view").innerHTML = `
    <h1>Register a missing child</h1>
    <form id="case-form" class="panel">
      <fieldset><legend>Legal authorisation</legend>
        <div class="form-grid">
          <label>GD / police case number * <input name="gd_number" required placeholder="e.g. GD-1234/2026"></label>
          <label>Police station * <input name="police_station" required></label>
          <label>Case officer <input name="case_officer"></label>
          <label>Case officer phone <input name="case_officer_phone"></label>
        </div>
      </fieldset>
      <fieldset><legend>Child</legend>
        <div class="form-grid">
          <label>Name <input name="child_name"></label>
          <label>Gender <select name="gender"><option value="">—</option><option>Female</option><option>Male</option><option>Other</option></select></label>
          <label>Age when missing (years) <input name="age_at_missing" type="number" step="0.5" min="0" max="18"></label>
          <label>Missing since * <input name="missing_since" type="date" required value="${today}"></label>
          <label style="grid-column:1/-1">Last seen location <input name="last_seen_location"></label>
          <label style="grid-column:1/-1">Notes <textarea name="notes"></textarea></label>
        </div>
      </fieldset>
      <fieldset><legend>Guardian</legend>
        <div class="form-grid">
          <label>Guardian name <input name="guardian_name"></label>
          <label>Guardian phone <input name="guardian_phone"></label>
        </div>
        <label class="check" style="margin-top:10px"><input type="checkbox" name="guardian_consent" value="true" required>
          <span>The guardian consents to the child's photos being used for face matching until the case is closed, after which all data is deleted.</span></label>
      </fieldset>
      <fieldset><legend>Photos</legend>
        <p class="small muted" style="margin-top:0">Add several clear photos: front, left and right, and from different ages (most recent is most useful). Enter the child's age in each photo if known.</p>
        <input type="file" id="photo-input" accept="image/*" multiple>
        <div id="photo-list" class="gallery" style="margin-top:10px"></div>
      </fieldset>
      <div class="row"><button class="btn primary" id="submit-case" type="submit">Register case &amp; generate looks</button><span id="case-status" class="muted"></span></div>
    </form>`;
  const files = [];
  $("#photo-input").addEventListener("change", (e) => {
    for (const f of e.target.files) files.push({ file: f, url: URL.createObjectURL(f) });
    e.target.value = "";
    renderPhotos();
  });
  function renderPhotos() {
    $("#photo-list").innerHTML = files.map((f, i) => `
      <figure><img src="${f.url}" alt=""><figcaption><input data-i="${i}" class="age" type="number" step="0.5" placeholder="age in photo" style="width:100%;padding:4px"></figcaption>
      <button type="button" class="btn small" data-rm="${i}" style="margin-top:4px">Remove</button></figure>`).join("");
    document.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => { files.splice(+b.dataset.rm, 1); renderPhotos(); }));
  }
  $("#case-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!files.length) return toast("Add at least one photo");
    const fd = new FormData(e.target);
    if (!fd.get("age_at_missing")) fd.delete("age_at_missing");
    const ages = [...document.querySelectorAll("#photo-list .age")].map((i) => i.value);
    files.forEach((f, i) => { fd.append("photos", f.file); fd.append("photo_ages", ages[i] || ""); });
    $("#submit-case").disabled = true;
    $("#case-status").innerHTML = `<span class="spinner"></span> Detecting faces, generating up to 50 predicted looks and creating face signatures…`;
    try {
      const r = await api("/api/cases", { method: "POST", body: fd });
      toast(`${r.case_code} registered: ${r.real_photos} real + ${r.generated_looks} predicted looks.` +
        (r.rejected_photos.length ? ` ${r.rejected_photos.length} photo(s) had no clear face.` : ""), 6000);
      location.hash = `#case/${r.id}`;
    } catch (err) {
      $("#case-status").textContent = err.message; $("#submit-case").disabled = false;
    }
  });
}

// ------------------------------------------------------------------ scanning
const cameraOptions = () => cameras.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");

async function viewScan() {
  $("#view").innerHTML = `
    <h1>Scan camera footage</h1>
    <div class="grid2">
      <form id="scan-form" class="panel stack">
        <div class="privacy">Only faces of registered missing children are searched. Every other face is discarded immediately and the uploaded footage is deleted after scanning.</div>
        <label>Camera location <select name="camera_id">${cameraOptions()}</select></label>
        <label>Recorded footage <input type="file" name="video" accept="video/*"></label>
        <label>…or live stream URL (RTSP/HTTP) <input name="stream_url" placeholder="rtsp://camera-ip/stream"></label>
        <label>Recording start time <input name="recorded_at" type="datetime-local"></label>
        <fieldset style="margin:0"><legend>Vehicle data from toll/ANPR system (optional)</legend>
          <div class="form-grid">
            <label>Number plate <input name="vehicle_plate" placeholder="DHAKA METRO-GA 11-2233"></label>
            <label>Direction <select name="direction"><option value="">—</option><option>north</option><option>south</option><option>east</option><option>west</option><option>towards Dhaka</option><option>away from Dhaka</option></select></label>
          </div>
        </fieldset>
        <button class="btn primary" type="submit" id="scan-btn">Start scan</button>
      </form>
      <div class="panel"><h2>Scan jobs</h2><div id="jobs"></div></div>
    </div>`;
  $("#scan-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (!fd.get("video")?.name) fd.delete("video");
    const ra = fd.get("recorded_at");
    fd.set("recorded_at", ra ? new Date(ra).toISOString() : "");
    $("#scan-btn").disabled = true;
    $("#scan-btn").innerHTML = `<span class="spinner"></span> Uploading…`;
    try { await api("/api/scans", { method: "POST", body: fd }); toast("Scan started"); e.target.reset(); }
    catch (err) { toast(err.message); }
    $("#scan-btn").disabled = false; $("#scan-btn").textContent = "Start scan";
    loadJobs();
  });
  async function loadJobs() {
    const jobs = await api("/api/scans");
    $("#jobs").innerHTML = jobs.length ? `<table><tr><th>#</th><th>Camera</th><th>Status</th><th>Faces seen</th><th>Discarded</th><th>Alerts</th><th></th></tr>
      ${jobs.map((j) => `<tr><td>${j.id}</td><td>${esc(j.camera_name)}<br><span class="small muted">${j.kind}${j.vehicle_plate ? " · " + esc(j.vehicle_plate) : ""}</span></td>
        <td><span class="pill ${j.status}">${j.status}</span>${j.status === "running" && j.kind === "video" ? `<div class="progress" style="margin-top:4px"><div style="width:${(j.progress * 100).toFixed(0)}%"></div></div>` : ""}
          ${j.error ? `<div class="small error">${esc(j.error)}</div>` : ""}</td>
        <td>${j.faces_seen}</td><td>${j.faces_discarded}</td>
        <td>${j.alerts_created ? `<a href="#alerts"><b>${j.alerts_created}</b></a>` : 0}</td>
        <td>${j.status === "running" ? `<button class="btn small" data-stop="${j.id}">Stop</button>` : ""}</td></tr>`).join("")}</table>`
      : `<p class="muted">No scans yet.</p>`;
    document.querySelectorAll("[data-stop]").forEach((b) => b.addEventListener("click", () => post(`/api/scans/${b.dataset.stop}/stop`, {}).then(loadJobs)));
  }
  await loadJobs();
  timers.push(setInterval(loadJobs, 2000));
}

function stopLive() {
  if (liveStream) { liveStream.getTracks().forEach((t) => t.stop()); liveStream = null; }
}

function viewLive() {
  $("#view").innerHTML = `
    <h1>Live camera</h1>
    <p class="muted">Simulates an edge device at a checkpoint: frames from this computer's webcam are matched against the watchlist in real time.</p>
    <div class="row" style="margin-bottom:12px">
      <label>Camera location <select id="live-cam">${cameraOptions()}</select></label>
      <label>Plate (optional) <input id="live-plate"></label>
      <button class="btn primary" id="live-start">Start camera</button>
      <button class="btn" id="live-stop" disabled>Stop</button>
      <span id="live-info" class="muted small"></span>
    </div>
    <div class="privacy" style="margin-bottom:12px">Green = face checked and discarded (not stored). Red = possible match, sent to the alert queue for human review.</div>
    <div class="live-wrap"><video id="live-video" autoplay muted playsinline></video><canvas id="live-canvas"></canvas></div>`;
  const video = $("#live-video"), canvas = $("#live-canvas"), grab = document.createElement("canvas");
  let running = false;

  $("#live-start").onclick = async () => {
    try { liveStream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } }); }
    catch (e) { return toast("Camera not available: " + e.message); }
    video.srcObject = liveStream; running = true;
    $("#live-start").disabled = true; $("#live-stop").disabled = false;
    loop();
  };
  $("#live-stop").onclick = () => { running = false; stopLive(); $("#live-start").disabled = false; $("#live-stop").disabled = true; };

  async function loop() {
    while (running && liveStream) {
      if (!video.videoWidth) { await new Promise((r) => setTimeout(r, 200)); continue; }
      grab.width = video.videoWidth; grab.height = video.videoHeight;
      grab.getContext("2d").drawImage(video, 0, 0);
      const blob = await new Promise((r) => grab.toBlob(r, "image/jpeg", 0.85));
      const fd = new FormData();
      fd.append("camera_id", $("#live-cam").value);
      fd.append("vehicle_plate", $("#live-plate").value);
      fd.append("frame", blob, "frame.jpg");
      const t0 = performance.now();
      try {
        const r = await api("/api/live/frame", { method: "POST", body: fd });
        draw(r);
        $("#live-info").textContent = `${r.detections.length} face(s) · ${(performance.now() - t0).toFixed(0)} ms/frame`;
      } catch (e) { $("#live-info").textContent = e.message; }
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  function draw(r) {
    canvas.width = r.width; canvas.height = r.height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, r.width, r.height);
    ctx.lineWidth = 3; ctx.font = "bold 20px sans-serif";
    for (const d of r.detections) {
      const [x1, y1, x2, y2] = d.box;
      const color = d.status === "possible_match" ? "#e74c3c" : d.status === "no_match" ? "#2ecc71" : "#95a5a6";
      ctx.strokeStyle = color; ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      if (d.status === "possible_match") {
        ctx.fillStyle = color; ctx.fillText(`${d.case_code} ${d.score.toFixed(2)}`, x1, y1 - 8);
      }
    }
  }
}

// ------------------------------------------------------------------ audit
async function viewAudit() {
  const rows = await api("/api/audit");
  $("#view").innerHTML = `<h1>Audit log</h1><p class="muted">Every login, case, scan, review and closure is recorded so misuse can be detected.</p>
    <div class="panel" style="overflow-x:auto"><table><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Details</th></tr>
    ${rows.map((r) => `<tr><td class="small">${fmtTime(r.ts)}</td><td>${esc(r.user)}</td><td>${esc(r.action)}</td><td>${esc(r.target || "")}</td><td class="small muted">${esc(r.details || "")}</td></tr>`).join("")}
    </table></div>`;
}

// ------------------------------------------------------------------ boot
api("/api/me").then((u) => { me = u; start(); }).catch(showLogin);
