/* ============================================================
   ALISTO LIVE MAP
   Plots every loved one who has a registered device, using
   whatever /api/loved-ones/map currently returns.

   Deliberately has no fallback coordinate: an elder whose device
   has never reported is counted and named, but NOT pinned. A pin
   in the wrong place is worse than no pin at all.

   Zoom is never hardcoded — one elder centres, several fit to
   bounds, so the same code works for 1 device or 50.
   ============================================================ */

const AlistoMap = (function () {
  const ENDPOINT = "/api/loved-ones/map";
  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const SINGLE_ZOOM = 16; // used only when there is exactly one pin
  const MAX_FIT_ZOOM = 17; // stops two close pins zooming to street level

  // Where to point the camera when there is nothing to plot yet. This is a
  // viewport default, NOT a pretend position: no marker is ever drawn here,
  // so the map never claims anyone is at this spot.
  const SERVICE_AREA = [10.3778, 123.7986]; // Barangay Sudlon II, Cebu City
  const SERVICE_AREA_ZOOM = 13;

  const STATUS_LABEL = {
    online: "Safe / Online",
    offline: "Offline",
    emergency: "Emergency",
    responded: "Responded",
  };

  let map = null;
  let markerLayer = null;

  function el(id) {
    return document.getElementById(id);
  }

  function showState(which, title, detail) {
    ["alistoMapLoading", "alistoMapEmpty", "alistoMapError"].forEach((id) => {
      const node = el(id);
      if (node) node.classList.toggle("show", id === which);
    });
    if (which === "alistoMapEmpty" || which === "alistoMapError") {
      const node = el(which);
      if (node && title) node.querySelector("strong").textContent = title;
      if (node && detail) node.querySelector("span").textContent = detail;
    }
  }

  function hideStates() {
    showState(null);
  }

  function showNotice(title, detail) {
    const node = el("alistoMapNotice");
    if (!node) return;
    node.querySelector("strong").textContent = title;
    node.querySelector("span").textContent = detail;
    node.classList.add("show");
  }

  function hideNotice() {
    const node = el("alistoMapNotice");
    if (node) node.classList.remove("show");
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function buildIcon(elder) {
    const inner = elder.photo_url
      ? `<img src="${escapeHtml(elder.photo_url)}" alt="" />`
      : escapeHtml(elder.initials || "?");

    return L.divIcon({
      html: `<div class="alisto-pin ${escapeHtml(elder.status)}">${inner}</div>`,
      className: "",
      iconSize: [38, 38],
      iconAnchor: [19, 19],
      popupAnchor: [0, -22],
    });
  }

  function buildPopup(elder) {
    const status = escapeHtml(elder.status);
    const label = STATUS_LABEL[elder.status] || "Unknown";

    const alertRow = elder.alert_title
      ? `<div class="alisto-popup-row">
           <span>Alert</span><strong>${escapeHtml(elder.alert_title)}</strong>
         </div>`
      : "";

    return `
      <div class="alisto-popup">
        <h4>${escapeHtml(elder.name)}</h4>
        <p class="alisto-popup-rel">${escapeHtml(elder.relationship)}</p>
        <span class="alisto-popup-status ${status}">${escapeHtml(label)}</span>
        <div class="alisto-popup-rows">
          ${alertRow}
          <div class="alisto-popup-row">
            <span>Device</span><strong>${escapeHtml(elder.device_id)}</strong>
          </div>
          <div class="alisto-popup-row">
            <span>Last report</span><strong>${escapeHtml(elder.last_seen)}</strong>
          </div>
          <div class="alisto-popup-row">
            <span>Location</span><strong>${escapeHtml(elder.address)}</strong>
          </div>
        </div>
      </div>`;
  }

  function ensureMap() {
    if (map) return map;
    map = L.map("alistoMapCanvas", { zoomControl: true });
    L.tileLayer(TILE_URL, {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
    return map;
  }

  function render(data) {
    const plotted = data.elders.filter((e) => e.has_location);

    hideStates();
    ensureMap();
    markerLayer.clearLayers();

    // Nothing to pin yet. Still show the map — an empty map reads as "no
    // reports", a blank panel reads as "broken" — and say why in a banner.
    if (!plotted.length) {
      const detail = data.total
        ? `${data.total} loved one${data.total === 1 ? "" : "s"} ${
            data.total === 1 ? "is" : "are"
          } linked, but no device has reported a location yet. Pins appear here as soon as one does.`
        : "Link a loved one's ALISTO device to see them on the map.";
      showNotice("No locations reported yet", detail);
      map.setView(SERVICE_AREA, SERVICE_AREA_ZOOM);
      setTimeout(() => map.invalidateSize(), 80);
      updateCount(data);
      return;
    }

    hideNotice();

    const points = [];
    plotted.forEach((elder) => {
      const point = [elder.latitude, elder.longitude];
      points.push(point);
      L.marker(point, { icon: buildIcon(elder) })
        .bindPopup(buildPopup(elder))
        .addTo(markerLayer);
    });

    // One pin centres; several fit to bounds so zooming out is not needed.
    if (points.length === 1) {
      map.setView(points[0], SINGLE_ZOOM);
    } else {
      map.fitBounds(L.latLngBounds(points), {
        padding: [48, 48],
        maxZoom: MAX_FIT_ZOOM,
      });
    }

    // Leaflet mis-measures a container that was display:none when created.
    setTimeout(() => map.invalidateSize(), 80);
    updateCount(data);
  }

  function updateCount(data) {
    const node = el("alistoMapCount");
    if (!node) return;

    if (!data.total) {
      node.textContent = "No devices linked yet";
      return;
    }

    let text = `${data.plotted} of ${data.total} shown`;
    if (data.waiting) {
      text += ` — ${data.waiting} waiting for a first location report`;
    }
    node.textContent = text;
  }

  async function load() {
    showState("alistoMapLoading");
    try {
      const res = await fetch(ENDPOINT);
      const data = await res.json();

      if (!data.success) {
        hideNotice();
        showState(
          "alistoMapError",
          "Could not load the map",
          data.message || "Please try again in a moment.",
        );
        return;
      }
      render(data);
    } catch (err) {
      console.error(err);
      hideNotice();
      showState(
        "alistoMapError",
        "Cannot reach the server",
        "Check your connection and try again.",
      );
    }
  }

  function open() {
    const overlay = el("alistoMapOverlay");
    if (!overlay) return;
    overlay.classList.add("open");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    load();
  }

  function close() {
    const overlay = el("alistoMapOverlay");
    if (!overlay) return;
    overlay.classList.remove("open");
    overlay.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
  }

  document.addEventListener("DOMContentLoaded", function () {
    const overlay = el("alistoMapOverlay");
    if (!overlay) return;

    const closeBtn = el("alistoMapClose");
    if (closeBtn) closeBtn.addEventListener("click", close);

    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && overlay.classList.contains("open")) close();
    });

    // Any element with data-alisto-map opens the map.
    document.querySelectorAll("[data-alisto-map]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        open();
      });
    });
  });

  return { open, close, reload: load };
})();