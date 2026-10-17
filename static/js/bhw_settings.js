/* Settings page wiring. Needs bhw_prefs.js (loaded in <head>). */
(function () {
    var P = window.AlistoPrefs;
    if (!P) return;
    var prefs = P.get();

    // Segmented controls (Theme / Text size / Time format)
    document.querySelectorAll(".seg").forEach(function (seg) {
        var key = seg.dataset.pref;
        function paint() {
            seg.querySelectorAll("button").forEach(function (b) {
                var on = b.dataset.value === String(P.get()[key]);
                b.classList.toggle("on", on);
                b.setAttribute("aria-checked", on ? "true" : "false");
            });
        }
        seg.addEventListener("click", function (e) {
            var b = e.target.closest("button"); if (!b) return;
            var patch = {}; patch[key] = b.dataset.value;
            P.set(patch); paint();
        });
        paint();
    });

    // Alert sound on/off
    var sound = document.getElementById("prefSound");
    sound.checked = !!prefs.sound;
    sound.addEventListener("change", function () { P.set({ sound: sound.checked }); });

    // Test button: plays the real emergency alarm (siren + voice). Click again to stop.
    var testBtn = document.getElementById("prefTestSound");
    var testLabel = document.getElementById("prefTestLabel");
    function setTesting(on) {
        testBtn.classList.toggle("playing", on);
        testLabel.textContent = on ? "Stop" : "Test";
    }
    testBtn.addEventListener("click", async function () {
        var A = window.AlistoAlarm;
        if (!A) return;
        if (A.isPlaying()) { A.stop(); setTesting(false); return; }
        setTesting(true);
        await A.play({ test: true });
        setTesting(false);
    });
    window.addEventListener("pagehide", function () { if (window.AlistoAlarm) window.AlistoAlarm.stop(); });

    // Desktop notifications (browser permission)
    var desk = document.getElementById("prefDesktop");
    desk.checked = !!prefs.desktop && window.Notification && Notification.permission === "granted";
    desk.addEventListener("change", function () {
        if (!desk.checked) return P.set({ desktop: false });
        if (!window.Notification) { desk.checked = false; return alert("This browser doesn't support notifications."); }
        Notification.requestPermission().then(function (r) {
            var ok = r === "granted"; desk.checked = ok; P.set({ desktop: ok });
            if (ok) new Notification("ALISTO", { body: "Desktop notifications are on." });
            else alert("Notifications are blocked. Allow them in your browser's site settings.");
        });
    });
})();