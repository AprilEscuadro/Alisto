/* ALISTO BHW live alert watcher. Loaded on EVERY BHW page (base.html).
   Polls /bhw/alerts/poll, and for each NEW unanswered emergency in this BHW's
   barangay: plays the alarm (siren + voice), shows a red banner, and sends a
   desktop notification. Honors the Settings toggles (alert sound, desktop).
   Stops when the alert is acknowledged/resolved or the BHW presses Silence. */
(function () {
    // BHW pages: <div id="bhwWatch" data-url="...">.
    // Family pages: window.ALISTO_WATCH = { url, listUrl, scope, badges } (set in family_nav.js).
    var C = window.ALISTO_WATCH || {};
    var cfg = document.getElementById("bhwWatch");
    var P = window.AlistoPrefs, A = window.AlistoAlarm;
    var URL = C.url || (cfg && cfg.dataset.url);
    if (!URL || !P || !A) return;
    var SCOPE = C.scope || "bhw";
    var LIST_URL = C.listUrl || URL.replace("/alerts/poll", "/alerts");
    var BADGES = C.badges || '.admin-bell-badge, .admin-nav-link[href$="/alerts"] .nav-badge';
    var POLL_MS = 15000;     // how often to ask the server
    var REPEAT_MS = 30000;   // re-ring while an alert is still unanswered
    var SEEN = "alisto_" + SCOPE + "_alerted", SILENT = "alisto_" + SCOPE + "_silenced", RING = "alisto_" + SCOPE + "_lastring";
    var baseTitle = document.title;
    var banner = null, last = [];

    function load(k) { try { return JSON.parse(localStorage.getItem(k) || "[]"); } catch (e) { return []; } }
    function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } }
    function lastRing() { return Number(localStorage.getItem(RING) || 0); }
    function stamp() { try { localStorage.setItem(RING, String(Date.now())); } catch (e) { } }
    function icons() { if (window.lucide) window.lucide.createIcons(); }

    // Browsers block sound until the page has had a click/tap/key. Unlock quietly on the first one.
    ["click", "keydown", "touchstart"].forEach(function (ev) {
        window.addEventListener(ev, function () { A.unlock(); }, { once: true, capture: true });
    });

    function ring() {
        if (!P.get().sound || A.isPlaying()) return;
        if (Date.now() - lastRing() < 25000) return;   // another tab just rang
        stamp();
        A.play({ repeat: 2 });
    }

    function notify(fresh) {
        var p = P.get();
        if (!p.desktop || !window.Notification || Notification.permission !== "granted") return;
        fresh.forEach(function (a) {
            var n = new Notification("ALISTO EMERGENCY", {
                body: a.resident + (a.trigger === "button" ? " pressed the ALISTO device" : " called for help") + " (" + a.time + ").",
                tag: "alisto-" + a.id, requireInteraction: true,
            });
            n.onclick = function () { window.focus(); window.location.href = a.url; n.close(); };
        });
    }

    function setCount(n) {
        document.querySelectorAll(BADGES).forEach(function (el) {
            el.textContent = n; el.hidden = n === 0;
        });
        document.title = n ? "\uD83D\uDEA8 (" + n + ") " + baseTitle : baseTitle;
    }

    // BHW only: keeps the bell dropdown (same look as the admin's) in sync.
    function renderPanel(open) {
        var list = document.getElementById("bhwNotifList");
        if (!list) return;                              // admin/family pages: no panel
        var empty = document.getElementById("bhwNotifEmpty");
        var count = document.getElementById("bhwNotifCount");
        list.innerHTML = "";
        open.slice(0, 8).forEach(function (a) {
            var row = document.createElement("a");
            row.href = a.url; row.className = "notif-item"; row.setAttribute("role", "menuitem");
            var ic = document.createElement("span"); ic.className = "notif-icon tint-red";
            var i = document.createElement("i"); i.setAttribute("data-lucide", "siren"); ic.appendChild(i);
            var body = document.createElement("span"); body.className = "notif-body";
            var t = document.createElement("strong"); t.textContent = a.resident;
            var s = document.createElement("small");
            s.textContent = (a.trigger === "button" ? "Pressed the ALISTO device" : "Called for help") + " \u2014 still active";
            var w = document.createElement("small"); w.className = "text-muted"; w.textContent = a.time;
            body.appendChild(t); body.appendChild(s); body.appendChild(w);
            var chev = document.createElement("i"); chev.setAttribute("data-lucide", "chevron-right");
            row.appendChild(ic); row.appendChild(body); row.appendChild(chev);
            list.appendChild(row);
        });
        list.hidden = !open.length;
        if (empty) empty.hidden = open.length > 0;
        if (count) { count.hidden = !open.length; count.textContent = open.length + " active"; }
        icons();
    }

    function render(open, silenced) {
        if (!open.length) { if (banner) { banner.remove(); banner = null; } return; }
        var loud = open.some(function (a) { return silenced.indexOf(a.id) < 0; });
        var blocked = loud && P.get().sound && !A.ready();
        var first = open[0];
        var text = open.length === 1
            ? first.resident + (first.trigger === "button" ? " pressed the ALISTO device" : " called for help") + " (" + first.time + ")"
            : open.length + " emergencies need a response";

        if (!banner) {
            banner = document.createElement("div");
            banner.className = "alert-watch";
            banner.setAttribute("role", "alert");
            document.body.appendChild(banner);
        }
        banner.innerHTML = "";
        var ic = document.createElement("i"); ic.setAttribute("data-lucide", "siren");
        var msg = document.createElement("div"); msg.className = "alert-watch-text";
        var b = document.createElement("strong"); b.textContent = "EMERGENCY";
        var s = document.createElement("span"); s.textContent = text;
        msg.appendChild(b); msg.appendChild(s);
        var actions = document.createElement("div"); actions.className = "alert-watch-actions";

        var view = document.createElement("a");
        view.href = open.length === 1 ? first.url : LIST_URL;
        view.className = "aw-btn aw-btn-solid"; view.textContent = open.length === 1 ? "View alert" : "View alerts";
        actions.appendChild(view);

        if (blocked) {
            var en = document.createElement("button"); en.type = "button"; en.className = "aw-btn";
            en.textContent = "Enable sound";
            en.addEventListener("click", function () { A.unlock(); stamp(); A.play({ repeat: 2 }); render(last, load(SILENT)); });
            actions.appendChild(en);
        } else if (loud) {
            var sil = document.createElement("button"); sil.type = "button"; sil.className = "aw-btn";
            sil.textContent = "Silence";
            sil.addEventListener("click", function () {
                A.stop();
                save(SILENT, load(SILENT).concat(open.map(function (a) { return a.id; })));
                render(last, load(SILENT));
            });
            actions.appendChild(sil);
        }
        banner.appendChild(ic); banner.appendChild(msg); banner.appendChild(actions);
        icons();
    }

    function handle(data) {
        var open = data.alerts || [], ids = open.map(function (a) { return a.id; });
        last = open;
        var seen = load(SEEN).filter(function (id) { return ids.indexOf(id) >= 0; });
        var silenced = load(SILENT).filter(function (id) { return ids.indexOf(id) >= 0; });
        var fresh = open.filter(function (a) { return seen.indexOf(a.id) < 0; });
        if (fresh.length) {
            seen = seen.concat(fresh.map(function (a) { return a.id; }));
            notify(fresh);
            lastRingReset();
        }
        save(SEEN, seen); save(SILENT, silenced);
        setCount(open.length);
        renderPanel(open);
        // Pop the bell open when a NEW emergency arrives (BHW pages only).
        if (fresh.length && document.getElementById("bhwNotifList")) {
            var bell = document.getElementById("adminNotif");
            if (bell) bell.classList.add("open");
        }
        render(open, silenced);

        if (!open.length) { if (A.isPlaying()) A.stop(); return; }
        var loud = open.some(function (a) { return silenced.indexOf(a.id) < 0; });
        if (!loud) { if (A.isPlaying()) A.stop(); return; }
        if (fresh.length || Date.now() - lastRing() >= REPEAT_MS) ring();
    }
    function lastRingReset() { try { localStorage.removeItem(RING); } catch (e) { } }

    async function poll() {
        try {
            var res = await fetch(URL, { headers: { Accept: "application/json" }, credentials: "same-origin" });
            if (!(res.headers.get("content-type") || "").includes("json")) return; // logged out
            var data = await res.json();
            if (data.success) handle(data);
        } catch (e) { /* offline or server restarting: try again next time */ }
    }

    poll();
    setInterval(poll, POLL_MS);
})();