/* Admin Help & Support: opens/closes the Admin Guide, Troubleshooting and About modals. */
(function () {
    function $(id) { return document.getElementById(id); }
    function openModal(id) {
        $(id).classList.add("open");
        document.body.classList.add("hs-lock");
    }
    function closeModal(id) {
        $(id).classList.remove("open");
        if (!document.querySelector(".hs-overlay.open")) document.body.classList.remove("hs-lock");
    }
    [["hsGuideBtn", "hsGuideModal"], ["hsTroubleBtn", "hsTroubleModal"], ["hsAboutBtn", "hsAboutModal"]]
        .forEach(function (p) {
            var b = $(p[0]);
            if (b) b.addEventListener("click", function () { openModal(p[1]); });
        });
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
})();