/* BHW Help & Support modals: User Guide, Report a Problem, About, Contact.
   Reports and messages go to the same endpoints as the family site and land
   in the admin Inbox. Replies come back to the BHW Inbox. */
(function () {
    var root = document.getElementById("hsRoot");
    if (!root) return;
    var REPORT_URL = root.dataset.reportUrl;
    var CONTACT_URL = root.dataset.contactUrl;
    var INBOX_URL = root.dataset.inboxUrl;

    function icons() { if (window.lucide) window.lucide.createIcons(); }
    function $(id) { return document.getElementById(id); }

    // ---------- open / close ----------
    function openModal(id) {
        $(id).classList.add("open");
        document.body.classList.add("hs-lock");
    }
    function closeModal(id) {
        $(id).classList.remove("open");
        if (!document.querySelector(".hs-overlay.open")) document.body.classList.remove("hs-lock");
    }
    document.querySelectorAll(".hs-overlay").forEach(function (ov) {
        ov.addEventListener("click", function (e) {
            if (e.target === ov || e.target.closest("[data-hs-close]")) closeModal(ov.id);
        });
    });
    document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        var open = document.querySelector(".hs-overlay.open");
        if (open) closeModal(open.id);
    });

    $("hsGuideBtn").addEventListener("click", function () { openModal("hsGuideModal"); });
    $("hsAboutBtn").addEventListener("click", function () { openModal("hsAboutModal"); });
    $("hsContactBtn").addEventListener("click", function () { openModal("hsContactModal"); });

    // ---------- toast ----------
    function toast(message, type, withInbox) {
        var t = document.createElement("div");
        t.className = "hs-toast " + (type || "success");
        var ic = document.createElement("i");
        ic.setAttribute("data-lucide", type === "error" ? "alert-circle" : "check-circle");
        var sp = document.createElement("span");
        sp.textContent = message;
        t.appendChild(ic); t.appendChild(sp);
        if (withInbox) {
            var a = document.createElement("a");
            a.href = INBOX_URL; a.textContent = "Open Inbox";
            t.appendChild(a);
        }
        document.body.appendChild(t);
        icons();
        setTimeout(function () { t.remove(); }, 6000);
    }
    function errMsg(err) {
        return err && err.message && err.message !== "Failed to fetch"
            ? err.message : "Cannot reach the server. Please try again.";
    }

    // ---------- Report a Problem (values MUST match REPORT_CATEGORIES_BHW in app.py) ----------
    var CATEGORIES = [
        {
            value: "app", label: "App Issue", subs: [
                ["crash", "App crashes or freezes"], ["buttons", "Buttons or links not working"],
                ["slow", "App is slow"], ["other_app", "Other app issue"]]
        },
        {
            value: "alert", label: "Alert Issue", subs: [
                ["not_received", "I didn't receive an alert"], ["late", "Alert arrived late"],
                ["cant_respond", "Can't acknowledge or resolve an alert"],
                ["wrong_location", "Location was incorrect"], ["other_alert", "Other alert issue"]]
        },
        {
            value: "resident", label: "Resident or Device Issue", subs: [
                ["missing", "A resident or device is missing from my list"],
                ["wrong_info", "Resident information is wrong"],
                ["offline", "Device shows offline but it is on"], ["other_resident", "Other resident issue"]]
        },
        {
            value: "map", label: "Map Issue", subs: [
                ["not_loading", "Map isn't loading"], ["pin_wrong", "A pin is in the wrong place"],
                ["status_wrong", "Device status is wrong"], ["other_map", "Other map issue"]]
        },
        {
            value: "account", label: "Account Issue", subs: [
                ["login", "Can't log in"], ["reset_password", "Can't reset password"],
                ["profile_info", "Profile info is wrong"], ["barangay", "My assigned barangay is wrong"],
                ["other_account", "Other account issue"]]
        },
        { value: "other", label: "Other", subs: null },
    ];

    var modalId = "hsReportModal";
    var steps = document.querySelectorAll("#hsReportModal .hs-rstep");
    var backBtn = $("hsReportBack"), titleEl = $("hsReportTitle");
    var foot = $("hsReportFoot"), nextBtn = $("hsReportNext");
    var step = 1, sel = {};

    function row(label, onClick) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "hs-row";
        var s = document.createElement("span"); s.textContent = label;
        var i = document.createElement("i"); i.setAttribute("data-lucide", "chevron-right");
        b.appendChild(s); b.appendChild(i);
        b.addEventListener("click", onClick);
        return b;
    }
    function show(n, title) {
        steps.forEach(function (s) { s.hidden = Number(s.dataset.step) !== n; });
        step = n; titleEl.textContent = title;
        backBtn.style.visibility = n === 1 ? "hidden" : "visible";
        foot.style.display = n >= 3 ? "flex" : "none";
        nextBtn.textContent = n === 4 ? "Submit" : "Continue";
    }
    function buildCategories() {
        var list = $("hsCategoryList"); list.innerHTML = "";
        CATEGORIES.forEach(function (c) {
            list.appendChild(row(c.label, function () {
                sel = { cat: c, sub: null };
                if (!c.subs) { show(3, "Describe the Problem"); return; }
                buildSubs(c); show(2, "Which best describes the problem?");
            }));
        });
        icons();
    }
    function buildSubs(c) {
        var list = $("hsSubList"); list.innerHTML = "";
        c.subs.forEach(function (s) {
            list.appendChild(row(s[1], function () {
                sel.sub = { value: s[0], label: s[1] };
                show(3, "Describe the Problem");
            }));
        });
        icons();
    }
    function reset() {
        sel = {}; $("hsProblemText").value = "";
        buildCategories(); show(1, "Report a Problem");
    }
    backBtn.addEventListener("click", function () {
        if (step === 4) show(3, "Describe the Problem");
        else if (step === 3) { if (sel.cat && sel.cat.subs) { buildSubs(sel.cat); show(2, "Which best describes the problem?"); } else show(1, "Report a Problem"); }
        else if (step === 2) show(1, "Report a Problem");
    });
    nextBtn.addEventListener("click", async function () {
        var text = $("hsProblemText");
        if (step === 3) {
            if (!text.value.trim()) { text.focus(); return; }
            $("hsRevCat").textContent = sel.cat.label;
            $("hsRevSub").textContent = sel.sub ? sel.sub.label : "Not specified";
            $("hsRevDesc").textContent = text.value.trim();
            show(4, "Submit Report"); return;
        }
        if (step !== 4) return;
        nextBtn.disabled = true; nextBtn.textContent = "Submitting...";
        try {
            var res = await fetch(REPORT_URL, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    category: sel.cat.value, subcategory: sel.sub ? sel.sub.value : null,
                    description: text.value, email: $("hsProblemEmail").value.trim(),
                }),
            });
            var data = await res.json();
            if (!data.success) throw new Error(data.message || "Could not send your report.");
            closeModal(modalId); toast(data.message, "success", true); reset();
        } catch (err) { toast(errMsg(err), "error"); }
        finally { nextBtn.disabled = false; nextBtn.textContent = step === 4 ? "Submit" : "Continue"; }
    });
    $("hsReportBtn").addEventListener("click", function () { reset(); openModal(modalId); });

    // ---------- Contact Support ----------
    $("hsContactForm").addEventListener("submit", async function (e) {
        e.preventDefault();
        var btn = $("hsContactSend");
        btn.disabled = true; btn.textContent = "Sending...";
        try {
            var res = await fetch(CONTACT_URL, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: $("hsName").value.trim(), email: $("hsEmail").value.trim(),
                    subject: $("hsSubject").value.trim(), message: $("hsMessage").value.trim(),
                }),
            });
            var data = await res.json();
            if (!data.success) throw new Error(data.message || "Could not send your message.");
            $("hsSubject").value = ""; $("hsMessage").value = "";
            closeModal("hsContactModal"); toast(data.message, "success", true);
        } catch (err) { toast(errMsg(err), "error"); }
        finally {
            btn.disabled = false;
            btn.innerHTML = '<i data-lucide="send"></i> Send'; icons();
        }
    });

    icons();
})();