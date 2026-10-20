// My Profile (admin + BHW): show/hide password, live checklist, and the
// profile photo (resized to 512px and saved as base64, same as the family
// page and the mobile app).
(function () {
    // ---- show / hide password ----
    document.querySelectorAll(".acct-eye").forEach((btn) => {
        btn.addEventListener("click", () => {
            const input = document.getElementById(btn.dataset.toggle);
            if (!input) return;
            const showing = input.type === "text";
            input.type = showing ? "password" : "text";
            btn.innerHTML = '<i data-lucide="' + (showing ? "eye" : "eye-off") + '"></i>';
            if (window.lucide) window.lucide.createIcons();
        });
    });

    // ---- live checklist ----
    const RULES = [
        { key: "length", test: (p) => p.length >= 8 },
        { key: "upper", test: (p) => /[A-Z]/.test(p) },
        { key: "lower", test: (p) => /[a-z]/.test(p) },
        { key: "number", test: (p) => /\d/.test(p) },
        { key: "special", test: (p) => /[^A-Za-z0-9]/.test(p) },
    ];
    const list = document.getElementById("acctPasswordRules");
    const pw = document.getElementById("new_password");
    const confirm = document.getElementById("confirm_password");
    const form = document.getElementById("acctPasswordForm");
    if (list && pw && confirm && form) {
        const update = () => {
            RULES.forEach((rule) => {
                const li = list.querySelector('[data-rule="' + rule.key + '"]');
                li.classList.remove("ok", "bad");
                if (pw.value) li.classList.add(rule.test(pw.value) ? "ok" : "bad");
            });
            const match = list.querySelector('[data-rule="match"]');
            match.classList.remove("ok", "bad");
            if (confirm.value) match.classList.add(pw.value === confirm.value ? "ok" : "bad");
        };
        pw.addEventListener("input", update);
        confirm.addEventListener("input", update);
        form.addEventListener("submit", (e) => {
            if (!RULES.every((r) => r.test(pw.value))) {
                e.preventDefault();
                update();
                pw.focus();
            } else if (pw.value !== confirm.value) {
                e.preventDefault();
                update();
                confirm.focus();
            }
        });
    }

    // ---- profile photo ----
    const trigger = document.getElementById("photoTrigger");
    const input = document.getElementById("profilePhoto");
    const photoForm = document.getElementById("photoForm");
    const field = document.getElementById("photoData");
    if (!trigger || !input || !photoForm || !field) return;
    const MAX_SIDE = 512, QUALITY = 0.7, MAX_BYTES = 700 * 1024;
    trigger.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
        const file = input.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            const img = new Image();
            img.onload = () => {
                let { width, height } = img;
                if (width > MAX_SIDE || height > MAX_SIDE) {
                    const scale = Math.min(MAX_SIDE / width, MAX_SIDE / height);
                    width = Math.round(width * scale);
                    height = Math.round(height * scale);
                }
                const canvas = document.createElement("canvas");
                canvas.width = width;
                canvas.height = height;
                const ctx = canvas.getContext("2d");
                ctx.fillStyle = "#ffffff";
                ctx.fillRect(0, 0, width, height);
                ctx.drawImage(img, 0, 0, width, height);
                const encoded = canvas.toDataURL("image/jpeg", QUALITY).split(",")[1];
                if (encoded.length > MAX_BYTES) {
                    alert("That photo is too large even after resizing. Please choose a different one.");
                    input.value = "";
                    return;
                }
                field.value = encoded;
                photoForm.submit();
            };
            img.onerror = () => { alert("That file could not be read as an image."); input.value = ""; };
            img.src = reader.result;
        };
        reader.readAsDataURL(file);
    });
})();