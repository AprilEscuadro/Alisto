// ALISTO Inbox page: shared by family, BHW, and admin. Talks to the JSON
// API in inbox_routes.py. The page sets window.INBOX before loading this:
//   { meId, role, canAnnounce, showStatusFilter }
// Everything is built with textContent, never innerHTML, so message text
// can't inject HTML.
(function () {
    "use strict";

    const CFG = window.INBOX || {};
    const API = "/inbox/api";
    const THREAD_POLL_MS = 6000; // open conversation
    const UNREAD_POLL_MS = 20000; // cheap check; reloads the list only if needed
    const FULL_RELOAD_MS = 120000; // safety net
    const TZ = "Asia/Manila";

    const TYPE_ICON = {
        support: "headphones",
        report: "triangle-alert",
        care_plan: "credit-card",
        direct: "message-circle",
        announcement: "megaphone",
    };
    const FILTERS = {
        all: () => true,
        support: (r) => r.type === "support" || r.type === "report",
        care_plan: (r) => r.type === "care_plan",
        direct: (r) => r.type === "direct",
        announcement: (r) => r.type === "announcement",
    };

    const state = {
        threads: [],
        announcements: [],
        filter: "all",
        status: "all",
        open: null, // {kind: 'thread'|'announcement', id}
        openStatus: "",
        seen: new Set(),
        lastMs: 0,
        lastDay: "",
        threadTimer: null,
        lastUnread: null,
        busy: false,
        recipients: null,
    };

    const $ = (id) => document.getElementById(id);
    const inboxEl = $("inbox");
    const listEl = $("threadList");
    const paneEl = $("pane");

    // ---------- helpers ----------

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(name) {
        const i = document.createElement("i");
        i.setAttribute("data-lucide", name);
        return i;
    }

    function button(cls, iconName, label) {
        const b = el("button", "ibx-btn " + cls);
        b.type = "button";
        if (iconName) b.appendChild(icon(iconName));
        b.appendChild(document.createTextNode(label));
        return b;
    }

    function drawIcons() {
        if (window.lucide) window.lucide.createIcons();
    }

    async function api(path, options) {
        const opts = Object.assign({ headers: { Accept: "application/json" } }, options || {});
        if (opts.body && typeof opts.body !== "string") {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(opts.body);
        }
        const res = await fetch(API + path, opts);
        if (res.status === 401) {
            window.location.href = "/login";
            throw new Error("Please log in again.");
        }
        let data = {};
        try {
            data = await res.json();
        } catch (e) {
            throw new Error("Something went wrong. Try again.");
        }
        if (!res.ok || data.success === false) throw new Error(data.message || "Something went wrong. Try again.");
        return data;
    }

    let toastTimer = null;
    function toast(message, isError) {
        const t = $("ibxToast");
        if (!t) return;
        t.textContent = message;
        t.classList.toggle("error", !!isError);
        t.classList.add("show");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
    }

    function dayKey(ms) {
        return new Date(ms).toLocaleDateString("en-CA", { timeZone: TZ });
    }

    function clock(ms) {
        return new Date(ms).toLocaleTimeString("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
    }

    function dayLabel(ms) {
        const key = dayKey(ms);
        if (key === dayKey(Date.now())) return "Today";
        if (key === dayKey(Date.now() - 86400000)) return "Yesterday";
        return new Date(ms).toLocaleDateString("en-US", { timeZone: TZ, month: "short", day: "numeric", year: "numeric" });
    }

    function shortTime(ms) {
        if (!ms) return "";
        const label = dayLabel(ms);
        if (label === "Today") return clock(ms);
        if (label === "Yesterday") return label;
        return new Date(ms).toLocaleDateString("en-US", { timeZone: TZ, month: "short", day: "numeric" });
    }

    // ---------- list ----------

    function rows() {
        const anns = state.announcements.map((a) => ({
            kind: "announcement",
            type: "announcement",
            id: a.id,
            subject: a.title,
            with: a.from_name,
            preview: a.body,
            last_at: a.created_at,
            unread: a.is_read ? 0 : 1,
            status: "",
        }));
        return state.threads
            .map((t) => Object.assign({ kind: "thread" }, t))
            .concat(anns)
            .sort((a, b) => b.last_at - a.last_at);
    }

    function statusOk(r) {
        if (state.status === "all" || r.kind !== "thread" || r.type === "direct") return true;
        return r.status === state.status;
    }

    function updateCounts() {
        const all = rows();
        Object.keys(FILTERS).forEach((f) => {
            const n = all.filter(FILTERS[f]).reduce((sum, r) => sum + (r.unread ? 1 : 0), 0);
            const badge = document.querySelector(`[data-count="${f}"]`);
            if (badge) {
                badge.textContent = n;
                badge.hidden = !n;
            }
        });
        const unreadThreads = state.threads.reduce((sum, t) => sum + t.unread, 0);
        state.lastUnread = unreadThreads;
        document.querySelectorAll("[data-inbox-badge]").forEach((b) => {
            b.textContent = unreadThreads > 99 ? "99+" : String(unreadThreads);
            b.hidden = !unreadThreads;
        });
        const base = document.title.replace(/^\(\d+\+?\)\s*/, "");
        document.title = (unreadThreads ? `(${unreadThreads}) ` : "") + base;
    }

    function renderList() {
        const items = rows().filter(FILTERS[state.filter]).filter(statusOk);
        listEl.textContent = "";
        if (!items.length) {
            let msg = "Nothing here yet. Use New message to start a conversation.";
            if (state.filter === "announcement") msg = "No announcements yet.";
            else if (state.status !== "all") msg = `No ${state.status} conversations here.`;
            listEl.appendChild(el("p", "list-empty", msg));
            updateCounts();
            return;
        }
        items.forEach((r) => {
            const btn = el("button", "thread-item");
            btn.type = "button";
            if (r.unread) btn.classList.add("unread");
            if (r.status === "resolved") btn.classList.add("is-resolved");
            if (state.open && state.open.kind === r.kind && state.open.id === r.id) btn.classList.add("active");

            const tile = el("span", `type-tile type-${r.type}`);
            tile.appendChild(icon(TYPE_ICON[r.type] || "message-circle"));

            const text = el("span", "thread-text");
            const top = el("span", "thread-top");
            let who = r.kind === "thread" ? `${r.with} · ${r.type_label}` : r.with;
            if (r.status === "resolved") who += " · Resolved";
            top.appendChild(el("span", "thread-who", who));
            top.appendChild(el("span", "thread-time", shortTime(r.last_at)));
            text.appendChild(top);
            text.appendChild(el("span", "thread-subject", r.subject));
            text.appendChild(el("span", "thread-preview", r.preview));

            btn.appendChild(tile);
            btn.appendChild(text);
            if (r.unread && r.kind === "thread") btn.appendChild(el("span", "unread-pill", r.unread > 99 ? "99+" : r.unread));

            btn.addEventListener("click", () => (r.kind === "thread" ? openThread(r.id) : openAnnouncement(r.id)));
            listEl.appendChild(btn);
        });
        drawIcons();
        updateCounts();
    }

    async function loadThreads() {
        try {
            const data = await api("/threads");
            state.threads = data.threads;
            renderList();
        } catch (e) {
            if (!state.threads.length) {
                listEl.textContent = "";
                listEl.appendChild(el("p", "list-empty", e.message));
            }
        }
    }

    async function loadAnnouncements() {
        try {
            const data = await api("/announcements");
            state.announcements = data.announcements;
            renderList();
        } catch (e) {
            /* announcements are optional; the list still works */
        }
    }

    // Cheap check: only reload the whole list when the unread count moved.
    async function checkUnread() {
        try {
            const data = await api("/unread");
            if (data.unread !== state.lastUnread) loadThreads();
        } catch (e) {
            /* try again next time */
        }
    }

    // ---------- right pane ----------

    function stopThreadPoll() {
        clearTimeout(state.threadTimer);
        state.threadTimer = null;
    }

    function showEmptyPane() {
        paneEl.textContent = "";
        const empty = el("div", "pane-empty");
        empty.appendChild(icon("messages-square"));
        empty.appendChild(el("p", null, "Pick a conversation to read it here."));
        paneEl.appendChild(empty);
        drawIcons();
    }

    function closePane() {
        stopThreadPoll();
        state.open = null;
        inboxEl.classList.remove("show-pane");
        showEmptyPane();
        history.replaceState(null, "", location.pathname);
        renderList();
    }

    function paneHead(title, metaNodes, actionNodes) {
        const head = el("div", "pane-head");
        const back = el("button", "pane-back");
        back.type = "button";
        back.setAttribute("aria-label", "Back to list");
        back.appendChild(icon("arrow-left"));
        back.addEventListener("click", closePane);
        head.appendChild(back);

        const wrap = el("div", "pane-title-wrap");
        wrap.appendChild(el("h2", "pane-title", title));
        const meta = el("div", "pane-meta");
        metaNodes.forEach((n) => meta.appendChild(n));
        wrap.appendChild(meta);
        head.appendChild(wrap);

        const actions = (actionNodes || []).filter(Boolean);
        if (actions.length) {
            const box = el("div", "pane-actions");
            actions.forEach((a) => box.appendChild(a));
            head.appendChild(box);
        }
        return head;
    }

    function threadMeta(t) {
        const nodes = [el("span", `ibx-chip type-${t.type}`, t.type_label)];
        if (t.type !== "direct") {
            nodes.push(el("span", `ibx-chip status-${t.status}`, t.status === "resolved" ? "Resolved" : "Open"));
        }
        nodes.push(el("span", null, `With ${t.with}`));
        if (t.elder_name) nodes.push(el("span", "ibx-chip plain", `About ${t.elder_name}`));
        if (t.related_id && t.related_id !== t.thread_no) nodes.push(el("span", "ibx-chip plain", t.related_id));
        if (t.thread_no) nodes.push(el("span", "ibx-chip plain", t.thread_no));
        return nodes;
    }

    function statusButton(t) {
        if (t.type === "direct") return null;
        const resolved = t.status === "resolved";
        const btn = button("ibx-btn-ghost", resolved ? "rotate-ccw" : "check", resolved ? "Reopen" : "Mark resolved");
        btn.addEventListener("click", async () => {
            btn.disabled = true;
            try {
                await api(`/threads/${encodeURIComponent(t.id)}/status`, {
                    method: "POST",
                    body: { status: resolved ? "open" : "resolved" },
                });
                await openThread(t.id);
                loadThreads();
            } catch (e) {
                toast(e.message, true);
                btn.disabled = false;
            }
        });
        return btn;
    }

    function drawMessage(box, m) {
        if (state.seen.has(m.id)) return false;
        state.seen.add(m.id);
        if (m.created_at) state.lastMs = Math.max(state.lastMs, m.created_at);

        const ms = m.created_at || Date.now();
        const day = dayKey(ms);
        if (day !== state.lastDay) {
            state.lastDay = day;
            box.appendChild(el("div", "day-sep", dayLabel(ms)));
        }

        if (m.is_system) {
            const note = el("div", "msg-system", m.body);
            note.appendChild(el("small", null, clock(ms)));
            box.appendChild(note);
            return true;
        }

        const mine = !!CFG.meId && m.sender_id === CFG.meId;
        const wrap = el("div", `msg ${mine ? "mine" : "theirs"}`);
        if (!mine) {
            const from = el("div", "msg-from");
            from.appendChild(el("b", null, m.sender_name || "Unknown"));
            if (m.role_label) from.appendChild(document.createTextNode(` · ${m.role_label}`));
            wrap.appendChild(from);
        }
        wrap.appendChild(el("div", "msg-bubble", m.body));
        wrap.appendChild(el("div", "msg-time", clock(ms)));
        box.appendChild(wrap);
        return true;
    }

    async function openThread(id) {
        stopThreadPoll();
        let data;
        try {
            data = await api(`/threads/${encodeURIComponent(id)}`);
        } catch (e) {
            toast(e.message, true);
            return;
        }
        const t = data.thread;
        state.open = { kind: "thread", id: t.id };
        state.openStatus = t.status;
        state.seen = new Set();
        state.lastMs = 0;
        state.lastDay = "";

        paneEl.textContent = "";
        paneEl.appendChild(paneHead(t.subject, threadMeta(t), [statusButton(t)]));

        if (t.type === "direct") {
            const notice = el("div", "pane-notice");
            notice.appendChild(icon("siren"));
            notice.appendChild(
                el("span", null, "Messages here don't send SMS alerts. In an emergency, use the ALISTO device or call 911.")
            );
            paneEl.appendChild(notice);
        }

        const box = el("div", "messages");
        box.id = "messageBox";
        data.messages.forEach((m) => drawMessage(box, m));
        paneEl.appendChild(box);
        paneEl.appendChild(composer(t));
        drawIcons();
        box.scrollTop = box.scrollHeight;

        const row = state.threads.find((r) => r.id === t.id);
        if (row) row.unread = 0;
        renderList();
        inboxEl.classList.add("show-pane");
        history.replaceState(null, "", `${location.pathname}?t=${encodeURIComponent(t.id)}`);
        scheduleThreadPoll();
    }

    function composer(t) {
        const wrap = el("div");
        const form = el("form", "composer");
        const input = el("textarea");
        input.rows = 1;
        input.maxLength = 2000;
        input.placeholder = t.status === "resolved" ? "Reply to reopen this request" : "Write a message";
        input.setAttribute("aria-label", "Message");
        const send = el("button", "ibx-btn ibx-btn-primary");
        send.type = "submit";
        send.appendChild(icon("send"));
        send.appendChild(document.createTextNode("Send"));
        form.appendChild(input);
        form.appendChild(send);

        input.addEventListener("input", () => {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 140) + "px";
        });
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
                e.preventDefault();
                form.requestSubmit();
            }
        });
        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            const body = input.value.trim();
            if (!body || state.busy) return;
            state.busy = true;
            send.disabled = true;
            try {
                await api(`/threads/${encodeURIComponent(t.id)}/messages`, { method: "POST", body: { body } });
                input.value = "";
                input.style.height = "auto";
                if (t.status === "resolved") await openThread(t.id); // chip goes back to Open
                else await pollThread();
                loadThreads();
            } catch (err) {
                toast(err.message, true);
            } finally {
                state.busy = false;
                send.disabled = false;
                const again = paneEl.querySelector(".composer textarea");
                if (again) again.focus();
            }
        });

        wrap.appendChild(form);
        wrap.appendChild(el("div", "composer-hint", "Enter to send, Shift+Enter for a new line"));
        return wrap;
    }

    async function pollThread() {
        if (!state.open || state.open.kind !== "thread") return;
        const id = state.open.id;
        const box = $("messageBox");
        if (!box) return;
        try {
            const data = await api(`/threads/${encodeURIComponent(id)}?after=${state.lastMs}`);
            if (!state.open || state.open.id !== id) return; // switched threads meanwhile
            const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
            let added = false;
            data.messages.forEach((m) => {
                if (drawMessage(box, m)) added = true;
            });
            if (stick) box.scrollTop = box.scrollHeight;

            if (data.thread.status !== state.openStatus) {
                openThread(id); // someone else resolved or reopened it
                loadThreads();
            } else if (added) {
                const row = state.threads.find((r) => r.id === id);
                if (row) {
                    row.preview = data.thread.preview;
                    row.last_at = data.thread.last_at;
                    renderList();
                }
            }
        } catch (e) {
            /* try again on the next tick */
        }
    }

    function scheduleThreadPoll() {
        stopThreadPoll();
        state.threadTimer = setTimeout(async () => {
            if (!document.hidden) await pollThread();
            if (state.open && state.open.kind === "thread") scheduleThreadPoll();
        }, THREAD_POLL_MS);
    }

    function openAnnouncement(id) {
        const a = state.announcements.find((x) => x.id === id);
        if (!a) return;
        stopThreadPoll();
        state.open = { kind: "announcement", id };
        paneEl.textContent = "";

        const meta = [el("span", "ibx-chip type-announcement", "Announcement"), el("span", null, `From ${a.from_name}`)];
        if (CFG.canAnnounce) {
            const who = a.audience_roles.map((r) => (r === "bhw" ? "BHWs" : "Families")).join(" and ");
            meta.push(el("span", "ibx-chip plain", `To ${who || "everyone"}`));
            meta.push(el("span", null, `Seen by ${Math.max(0, a.read_count - 1)}`));
        }
        meta.push(el("span", "ibx-chip plain", a.barangay || "All barangays"));
        meta.push(el("span", null, `${dayLabel(a.created_at)}, ${clock(a.created_at)}`));

        let del = null;
        if (CFG.role === "admin" || a.created_by === CFG.meId) {
            del = button("ibx-btn-ghost ibx-btn-danger", "trash-2", "Delete");
            del.addEventListener("click", async () => {
                if (!window.confirm("Delete this announcement? People will no longer see it.")) return;
                del.disabled = true;
                try {
                    await api(`/announcements/${encodeURIComponent(id)}`, { method: "DELETE" });
                    state.announcements = state.announcements.filter((x) => x.id !== id);
                    toast("Announcement deleted.");
                    closePane();
                } catch (e) {
                    toast(e.message, true);
                    del.disabled = false;
                }
            });
        }

        paneEl.appendChild(paneHead(a.title, meta, [del]));
        paneEl.appendChild(el("div", "announcement-body", a.body));
        drawIcons();
        inboxEl.classList.add("show-pane");
        history.replaceState(null, "", location.pathname);

        if (!a.is_read) {
            a.is_read = true;
            api(`/announcements/${encodeURIComponent(id)}/read`, { method: "POST" }).catch(() => { });
        }
        renderList();
    }

    // ---------- modals ----------

    function openModal(modal) {
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("modal-open");
    }

    function closeModal(modal) {
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("modal-open");
    }

    document.querySelectorAll(".ibx-modal").forEach((modal) => {
        modal.addEventListener("click", (e) => {
            if (e.target === modal || e.target.closest("[data-close-modal]")) closeModal(modal);
        });
    });
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        document.querySelectorAll(".ibx-modal.open").forEach(closeModal);
    });

    function showError(box, message) {
        box.textContent = message;
        box.classList.toggle("show", !!message);
    }

    async function loadRecipients() {
        if (state.recipients) return state.recipients;
        const data = await api("/recipients");
        state.recipients = data;

        const toSel = $("composeTo");
        toSel.textContent = "";
        const blank = el("option", null, "Choose a person");
        blank.value = "";
        toSel.appendChild(blank);
        const groups = { admin: "ALISTO Support", bhw: "Barangay Health Workers", family: "Families" };
        Object.keys(groups).forEach((role) => {
            const people = data.recipients.filter((r) => r.role === role);
            if (!people.length) return;
            const group = el("optgroup");
            group.label = groups[role];
            people.forEach((r) => {
                const opt = el("option", null, role === "admin" ? `${r.name} (Admin team)` : r.name);
                opt.value = r.key;
                group.appendChild(opt);
            });
            toSel.appendChild(group);
        });

        const typeSel = $("composeType");
        typeSel.textContent = "";
        if (CFG.role === "admin") {
            const dm = el("option", null, "Message");
            dm.value = "direct";
            typeSel.appendChild(dm);
        }
        data.ticket_types.forEach((t) => {
            const opt = el("option", null, t.label);
            opt.value = t.key;
            typeSel.appendChild(opt);
        });

        const elderSel = $("composeElder");
        data.elders.forEach((e) => {
            const opt = el("option", null, e.name);
            opt.value = e.id;
            elderSel.appendChild(opt);
        });
        $("composeElderField").hidden = !data.elders.length;

        const brgy = $("announceBarangay");
        if (brgy) {
            data.barangays.forEach((b) => {
                const opt = el("option", null, b);
                opt.value = b;
                brgy.appendChild(opt);
            });
        }
        return data;
    }

    // ---- new message ----

    const composeModal = $("composeModal");
    const composeForm = $("composeForm");

    function syncComposeFields() {
        const r = (state.recipients ? state.recipients.recipients : []).find((x) => x.key === $("composeTo").value);
        // Topic: family/BHW pick one when writing to ALISTO Support; admins can
        // also file a ticket for someone. BHW <-> family is always a message.
        $("composeTypeField").hidden = !r || !(r.role === "admin" || CFG.role === "admin");
        $("composeToNote").textContent = r ? r.note : "";
    }

    async function openCompose(prefill) {
        showError($("composeError"), "");
        composeForm.reset();
        openModal(composeModal);
        try {
            await loadRecipients();
        } catch (e) {
            showError($("composeError"), e.message);
            return;
        }
        if (prefill) {
            if (prefill.to) $("composeTo").value = prefill.to;
            if (prefill.type) $("composeType").value = prefill.type;
            if (prefill.subject) $("composeSubject").value = prefill.subject;
        }
        syncComposeFields();
        (prefill && prefill.subject ? $("composeBody") : $("composeTo")).focus();
    }

    $("composeTo").addEventListener("change", syncComposeFields);
    $("newMessageBtn").addEventListener("click", () => openCompose());

    composeForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const errorBox = $("composeError");
        showError(errorBox, "");
        const payload = {
            to: $("composeTo").value,
            type: $("composeTypeField").hidden ? "" : $("composeType").value,
            elder_id: $("composeElder").value,
            subject: $("composeSubject").value.trim(),
            body: $("composeBody").value.trim(),
        };
        if (!payload.to) return showError(errorBox, "Choose who to send this to.");
        if (!payload.subject || !payload.body) return showError(errorBox, "Add a subject and a message.");

        const btn = $("composeSend");
        btn.disabled = true;
        try {
            const data = await api("/threads", { method: "POST", body: payload });
            closeModal(composeModal);
            toast("Message sent.");
            await loadThreads();
            openThread(data.thread_id);
        } catch (err) {
            showError(errorBox, err.message);
        } finally {
            btn.disabled = false;
        }
    });

    // ---- new announcement (admin / BHW) ----

    const announceModal = $("announceModal");
    if (announceModal && CFG.canAnnounce) {
        const form = $("announceForm");
        $("newAnnouncementBtn").addEventListener("click", async () => {
            showError($("announceError"), "");
            form.reset();
            openModal(announceModal);
            try {
                await loadRecipients();
            } catch (e) {
                showError($("announceError"), e.message);
            }
            $("announceTitle").focus();
        });

        form.addEventListener("submit", async (e) => {
            e.preventDefault();
            const errorBox = $("announceError");
            showError(errorBox, "");
            const audience = $("announceAudience");
            const brgy = $("announceBarangay");
            const payload = {
                title: $("announceTitle").value.trim(),
                body: $("announceBody").value.trim(),
                audience: audience ? audience.value : "family",
                barangay: brgy ? brgy.value : "",
            };
            if (!payload.title || !payload.body) return showError(errorBox, "Add a title and a message.");

            const btn = $("announceSend");
            btn.disabled = true;
            try {
                await api("/announcements", { method: "POST", body: payload });
                closeModal(announceModal);
                toast("Announcement posted.");
                await loadAnnouncements();
                const latest = state.announcements[0];
                if (latest) openAnnouncement(latest.id);
            } catch (err) {
                showError(errorBox, err.message);
            } finally {
                btn.disabled = false;
            }
        });
    }

    // ---------- tabs + status filter ----------

    document.querySelectorAll(".inbox-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            state.filter = tab.dataset.filter;
            document.querySelectorAll(".inbox-tab").forEach((t) => {
                const on = t === tab;
                t.classList.toggle("active", on);
                t.setAttribute("aria-selected", String(on));
            });
            const statusBar = $("statusBar");
            if (statusBar) statusBar.hidden = ["direct", "announcement"].includes(state.filter);
            renderList();
        });
    });

    const statusSel = $("statusFilter");
    if (statusSel) {
        statusSel.addEventListener("change", () => {
            state.status = statusSel.value;
            renderList();
        });
        state.status = statusSel.value;
    }

    // ---------- start ----------

    async function start() {
        showEmptyPane();
        await Promise.all([loadThreads(), loadAnnouncements()]);

        const params = new URLSearchParams(location.search);
        if (params.get("t")) openThread(params.get("t"));
        else if (params.get("compose")) {
            openCompose({ to: "admins", type: params.get("compose"), subject: params.get("subject") || "" });
        }

        setInterval(() => {
            if (!document.hidden) checkUnread();
        }, UNREAD_POLL_MS);
        setInterval(() => {
            if (!document.hidden) {
                loadThreads();
                loadAnnouncements();
            }
        }, FULL_RELOAD_MS);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
                checkUnread();
                pollThread();
            }
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();