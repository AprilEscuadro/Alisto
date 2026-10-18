/* ALISTO BHW display prefs. Loaded in <head> of bhw/base.html so the theme
   is applied BEFORE the page paints (no white flash in dark mode). */
(function () {
    // Family pages set window.ALISTO_PREFS_KEY first so their choices are kept apart.
    var KEY = window.ALISTO_PREFS_KEY || "alisto_bhw_prefs";
    var DEFAULTS = Object.assign({ theme: "system", textSize: "default", timeFormat: "12", sound: true, desktop: false },
        window.ALISTO_PREFS_DEFAULTS || {});

    function read() {
        try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || "{}")); }
        catch (e) { return Object.assign({}, DEFAULTS); }
    }
    function write(p) { try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) { } }

    var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    function apply(p) {
        var dark = p.theme === "dark" || (p.theme === "system" && mq && mq.matches);
        var root = document.documentElement;
        root.setAttribute("data-theme", dark ? "dark" : "light");
        root.setAttribute("data-text", p.textSize);
        root.setAttribute("data-time", p.timeFormat);
    }

    // Follow the OS live while "System" is selected.
    if (mq && mq.addEventListener) {
        mq.addEventListener("change", function () { var p = read(); if (p.theme === "system") apply(p); });
    }

    window.AlistoPrefs = {
        get: read,
        set: function (patch) { var p = Object.assign(read(), patch); write(p); apply(p); return p; },
    };

    // ---------- Emergency alarm: siren + spoken message ----------
    // Used by Settings > "Test" and (later) by the Alerts page for real alerts.
    var Alarm = (function () {
        var ctx = null, osc = null, gain = null, token = 0, playing = false;
        var LINE = "Alisto emergency! Emergency device pressed!";

        function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

        function siren(seconds) {
            return new Promise(function (resolve) {
                var Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) return resolve();
                if (!ctx) ctx = new Ctx();
                if (ctx.state === "suspended") ctx.resume();
                osc = ctx.createOscillator(); gain = ctx.createGain();
                osc.type = "sawtooth"; osc.connect(gain); gain.connect(ctx.destination);
                var t0 = ctx.currentTime, sweep = 0.7, n = Math.ceil(seconds / sweep);
                osc.frequency.setValueAtTime(650, t0);
                for (var i = 0; i < n; i++) {
                    osc.frequency.linearRampToValueAtTime(1000, t0 + sweep * i + sweep / 2);
                    osc.frequency.linearRampToValueAtTime(650, t0 + sweep * (i + 1));
                }
                gain.gain.setValueAtTime(0.0001, t0);
                gain.gain.linearRampToValueAtTime(0.22, t0 + 0.05);
                gain.gain.setValueAtTime(0.22, t0 + seconds - 0.1);
                gain.gain.linearRampToValueAtTime(0.0001, t0 + seconds);
                osc.onended = function () { resolve(); };
                osc.start(t0); osc.stop(t0 + seconds);
            });
        }

        function speak(text) {
            return new Promise(function (resolve) {
                if (!("speechSynthesis" in window)) return resolve();
                var u = new SpeechSynthesisUtterance(text);
                u.lang = "en-US"; u.rate = 0.95; u.pitch = 1; u.volume = 1;
                u.onend = u.onerror = function () { resolve(); };
                window.speechSynthesis.speak(u);
            });
        }

        function stop() {
            token++; playing = false;
            try { if (osc) osc.stop(); } catch (e) { }
            if ("speechSynthesis" in window) window.speechSynthesis.cancel();
        }

        // opts: { test: true } adds "This is only a test." and plays the message twice.
        // opts: { repeat: n } for real alerts (n times, default 2).
        async function play(opts) {
            opts = opts || {};
            stop();
            var my = token, times = opts.repeat || 2;
            playing = true;
            for (var i = 0; i < times; i++) {
                if (my !== token) return;
                await siren(i === 0 ? 2.1 : 1.0);
                if (my !== token) return;
                await speak(LINE);
                await sleep(250);
            }
            if (opts.test && my === token) await speak("This is only a test.");
            if (my === token) playing = false;
        }

        // Browsers only allow sound after a click/tap/key. Call this from one (done
        // automatically by bhw_alert_watch.js) so real alerts can ring later.
        function unlock() {
            try {
                var Ctx = window.AudioContext || window.webkitAudioContext;
                if (Ctx) { if (!ctx) ctx = new Ctx(); if (ctx.state === "suspended") ctx.resume(); }
                if ("speechSynthesis" in window) {
                    var u = new SpeechSynthesisUtterance(" "); u.volume = 0;
                    window.speechSynthesis.speak(u);
                }
            } catch (e) { }
        }
        function ready() { return !!ctx && ctx.state === "running"; }

        return { play: play, stop: stop, unlock: unlock, ready: ready, isPlaying: function () { return playing; } };
    })();
    window.AlistoAlarm = Alarm;

    apply(read());
})();