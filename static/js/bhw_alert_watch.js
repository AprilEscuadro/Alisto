/* ALISTO live alert watcher. Loaded on every Family, BHW, and Admin page.
   Polls for unanswered emergencies. While even ONE is still open, the alarm
   (siren + voice) plays nonstop and the red banner stays up, on every page,
   including the Alerts page itself. There is no Silence button: the sound
   and banner only stop once ALL open emergencies have been acted on
   (acknowledged/responded, resolved, or marked a false alarm). */
(function () {
    // BHW pages:    <div id="bhwWatch" data-url="..." data-list="...">
    // Family/Admin: window.ALISTO_WATCH = { url, listUrl, scope, badges }
    var C = window.ALISTO_WATCH || {};
    var cfg = document.getElementById("bhwWatch");
    var P = window.AlistoPrefs, A = window.AlistoAlarm;
    var URL = C.url || (cfg && cfg.dataset.url);
    if (!URL || !P || !A) return;
    var SCOPE = C.scope || "bhw";
    var LIST_URL = C.listUrl || (cfg && cfg.dataset.list) || URL.replace("/alerts/poll", "/alerts");
    var BADGES = C.badges || '.admin-bell-badge, .admin-nav-link[href$="/alerts"] .nav-badge';
    var POLL_MS = 15000;     // how often to ask the server

    // Admin only: Settings > "Emergency alerts" and "Alert sound" can turn
    // the banner and the siren off. Family and BHW always get both.
    function alertsOn() { return SCOPE !== "admin" || P.get().alerts !== false; }
    function soundOn() { return alertsOn() && (SCOPE !== "admin" || P.get().sound !== false); }
    var SEEN = "alisto_" + SCOPE + "_alerted", RING = "alisto_" + SCOPE + "_ringer";
    var TAB = Math.random().toString(36).slice(2);   // this browser tab
    var baseTitle = document.title;
    var banner = null, last = [], looping = false;

    function load(k) { try { return JSON.parse(localStorage.getItem(k) || "[]"); } catch (e) { return []; } }
    function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } }
    function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function icons() { if (window.lucide) window.lucide.createIcons(); }

    // ---------- Only ONE tab plays the sound ----------
    // With several ALISTO tabs open, they'd all ring on top of each other.
    // The tab that rings keeps a fresh timestamp in localStorage; the others
    // stay quiet unless that timestamp goes stale (tab closed or navigated away).
    function claim() {
        var r = {};
        try { r = JSON.parse(localStorage.getItem(RING) || "{}") || {}; } catch (e) { }
        if (r.id && r.id !== TAB && Date.now() - r.t < 6000) return false;
        try { localStorage.setItem(RING, JSON.stringify({ id: TAB, t: Date.now() })); } catch (e) { }
        return true;
    }
    function release() {
        try {
            var r = JSON.parse(localStorage.getItem(RING) || "{}") || {};
            if (r.id === TAB) localStorage.removeItem(RING);
        } catch (e) { }
    }

    // ---------- Continuous alarm ----------
    // Plays the siren + voice back to back for as long as any emergency is open.
    async function alarmLoop() {
        if (looping) return;
        looping = true;
        while (last.length && soundOn()) {
            // Sound is blocked until the browser allows it. Keep asking: after a
            // click on the previous ALISTO page most browsers allow it right away,
            // otherwise it starts on the first click/tap/key on this page.
            if (!A.ready()) A.unlock();
            if (A.ready() && claim()) {
                var beat = setInterval(claim, 2000);
                await A.play({ repeat: 1 });
                clearInterval(beat);
            } else {
                await sleep(1000);
            }
        }
        release();
        looping = false;
    }

    // Browsers block sound until the page gets a click/tap/key. On the first
    // one, unlock audio; the loop above starts ringing right after.
    ["click", "keydown", "touchstart"].forEach(function (ev) {
        window.addEventListener(ev, function () { A.unlock(); alarmLoop(); }, { once: true, capture: true });
    });
    // Leaving the page: hand the alarm over to the next page/tab right away.
    window.addEventListener("pagehide", function () { A.stop(); release(); });

    function notify(fresh) {
        var p = P.get();
        if (!p.desktop || !window.Notification || Notification.permission !== "granted") return;
        fresh.forEach(function (a) {
            var n = new Notification("ALISTO EMERGENCY", {
                body: a.resident + (a.trigger === "button" ? " pressed the ALISTO device" : " called for help") + " (" + a.time + ").",
                tag: "alisto-" + a.id, requireInteraction: true,
            });
            n.onclick = function () { window.focus(); window.location.href = LIST_URL; n.close(); };
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

    function render(open) {
        if (!open.length) { if (banner) { banner.remove(); banner = null; } return; }
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

        // Always opens the Alerts list already filtered to Active.
        var view = document.createElement("a");
        view.href = LIST_URL;
        view.className = "aw-btn aw-btn-solid"; view.textContent = "View alerts";
        actions.appendChild(view);

        banner.appendChild(ic); banner.appendChild(msg); banner.appendChild(actions);
        icons();
    }

    function handle(data) {
        var open = data.alerts || [], ids = open.map(function (a) { return a.id; });
        last = open;
        var seen = load(SEEN).filter(function (id) { return ids.indexOf(id) >= 0; });
        var fresh = open.filter(function (a) { return seen.indexOf(a.id) < 0; });
        if (fresh.length) {
            seen = seen.concat(fresh.map(function (a) { return a.id; }));
            if (alertsOn()) notify(fresh);
        }
        save(SEEN, seen);
        setCount(open.length);
        renderPanel(open);
        // Pop the bell open when a NEW emergency arrives (BHW pages only).
        if (fresh.length && document.getElementById("bhwNotifList")) {
            var bell = document.getElementById("adminNotif");
            if (bell) bell.classList.add("open");
        }
        render(alertsOn() ? open : []);

        // All emergencies acted on (or alerts/sound turned off): stop the sound.
        if (!open.length || !soundOn()) { A.stop(); return; }
        alarmLoop();
    }

    // Settings changed (this tab or another one): apply it right away.
    window.AlistoWatchRefresh = function () { handle({ alerts: last }); };
    window.addEventListener("storage", function (e) {
        if (e.key && e.key.indexOf("_prefs") > 0) window.AlistoWatchRefresh();
    });

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