/* ALISTO BHW: resident profile extras + edit-in-place (Elderly Users page).
   Works inside the resident's View window (built by admin.js):
   - shows the connected family accounts (name, email, relationship) and the
     emergency contacts under the details,
   - "Edit details" turns Full name, Date of birth, Address and Occupation into
     inputs IN THE SAME TILES; everything else (age, device, load, last update,
     accounts, contacts) is greyed out and locked,
   - saves through /bhw/elderly/<id>/edit (the row's data-edit-url). */
(function () {
    'use strict';

    var $ = function (sel, root) { return (root || document).querySelector(sel); };
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    var esc = function (v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    };
    var icons = function () { if (window.lucide) window.lucide.createIcons(); };

    var EDITABLE = { 'Full Name': 'name', 'Date of Birth': 'dob', 'Address': 'address', 'Occupation': 'occupation' };
    var state = { row: null, data: null, editing: false, saving: false, seq: 0, originals: [] };

    // ---------- styles (colours come from the View window's own variables) ----------
    var style = document.createElement('style');
    style.textContent =
        '#vmExtra{display:grid;gap:18px;padding:0 32px 22px}' +
        '.vm-sec-title{display:flex;align-items:center;gap:8px;margin:0 0 10px;font-size:12px;font-weight:700;' +
        'letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}' +
        '.vm-sec-title svg{width:15px;height:15px}' +
        '.vm-count{padding:1px 8px;border-radius:999px;background:rgba(80,136,254,.14);color:#3b6ff0;font-size:11px;letter-spacing:0}' +
        '.vm-list{display:grid;grid-template-columns:1fr 1fr;gap:10px}' +
        '.vm-item{display:flex;align-items:center;gap:12px;min-width:0;padding:12px 14px;border:1px solid var(--hair);' +
        'border-radius:16px;background:var(--tile)}' +
        '.vm-av{flex:none;width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-size:12px;' +
        'font-weight:700;background:rgba(80,136,254,.14);color:#3b6ff0}' +
        '.vm-item-main{flex:1;min-width:0}' +
        '.vm-item-main strong{display:block;font-size:14px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
        '.vm-item-main small{display:block;margin-top:2px;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
        '.vm-tags{flex:none;display:flex;flex-direction:column;align-items:flex-end;gap:4px}' +
        '.vm-tag{padding:2px 9px;border-radius:999px;background:rgba(80,136,254,.14);color:#3b6ff0;font-size:11px;font-weight:700;white-space:nowrap}' +
        '.vm-tag.role{background:rgba(120,120,128,.16);color:var(--muted)}' +
        '.vm-note{padding:14px 16px;border:1px dashed var(--hair);border-radius:16px;font-size:13px;color:var(--muted)}' +
        '#vmEditFoot{position:sticky;bottom:0;display:flex;align-items:center;justify-content:flex-end;gap:10px;' +
        'padding:14px 32px 20px;background:rgba(255,255,255,.78);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}' +
        'html[data-theme="dark"] #vmEditFoot{background:rgba(22,30,54,.86)}' +
        '.vm-foot-msg{flex:1;min-height:18px;font-size:13px;color:var(--muted)}' +
        '.vm-foot-msg.error{color:#dc2626}' +
        '.vm-btn{display:inline-flex;align-items:center;gap:8px;height:44px;padding:0 22px;border:none;border-radius:14px;' +
        'font:inherit;font-size:14.5px;font-weight:700;cursor:pointer;transition:transform .1s}' +
        '.vm-btn:active{transform:scale(.97)}.vm-btn:disabled{opacity:.5;cursor:not-allowed}' +
        '.vm-btn svg{width:17px;height:17px}' +
        '.vm-btn-primary{background:#5088fe;color:#fff;box-shadow:0 8px 20px rgba(80,136,254,.35)}' +
        '.vm-btn-ghost{background:rgba(120,120,128,.14);color:var(--ink)}' +
        '#userModalDetails > div{position:relative}' +
        '#userModalDetails > div.vm-locked{opacity:.5;cursor:not-allowed;user-select:none;-webkit-user-select:none}' +
        '#userModalDetails > div.vm-locked *{pointer-events:none}' +
        '.vm-lock{position:absolute;top:10px;right:12px;color:var(--muted)}' +
        '.vm-lock svg{width:14px;height:14px}' +
        '#vmExtra.vm-locked{opacity:.5;pointer-events:none;user-select:none}' +
        '.vm-input{display:block;width:100%;box-sizing:border-box;margin:3px 0 0;padding:9px 12px;border:1.5px solid var(--hair);' +
        'border-radius:12px;background:rgba(120,120,128,.1);font:inherit;font-size:15px;font-weight:500;color:var(--ink)}' +
        'textarea.vm-input{resize:none;line-height:1.4}' +
        '.vm-input:focus{outline:none;border-color:#5088fe;background:transparent}' +
        '.vm-hint{display:block;margin-top:5px;font-size:11.5px;font-weight:500;color:var(--muted)}' +
        '@media (max-width:640px){#vmExtra{padding:0 20px 18px}.vm-list{grid-template-columns:1fr}#vmEditFoot{padding:12px 20px 16px}}';
    document.head.appendChild(style);

    // ---------- helpers ----------
    function initials(name) {
        return String(name || '').trim().split(/\s+/).slice(0, 2).map(function (w) { return w[0] || ''; }).join('').toUpperCase();
    }
    function dl() { return $('#userModalDetails'); }
    function tiles() { return $$(':scope > div', dl()); }
    function labelOf(tile) { return ((tile.querySelector('dt') || {}).textContent || '').trim(); }
    function tileFor(label) { return tiles().filter(function (t) { return labelOf(t) === label; })[0] || null; }

    function reset() {
        state.row = null; state.data = null; state.editing = false; state.saving = false; state.originals = [];
        var a = $('#vmExtra'); if (a) a.remove();
        var b = $('#vmEditFoot'); if (b) b.remove();
        var card = $('#userModal .admin-modal-card'); if (card) card.classList.remove('vm-editing');
    }

    // ---------- extra tile + sections ----------
    function addOccupationTile(value) {
        var list = dl();
        if (!list || tileFor('Occupation')) return;
        var tile = document.createElement('div');
        tile.innerHTML = '<span class="vm-ico"><i data-lucide="briefcase"></i></span><dt>Occupation</dt><dd></dd>';
        var dd = tile.querySelector('dd');
        if (value) dd.textContent = value; else dd.innerHTML = '<span style="color:var(--muted)">Not provided</span>';
        var addr = tileFor('Address');
        if (addr) addr.insertAdjacentElement('afterend', tile); else list.appendChild(tile);
    }

    function accountItem(a) {
        var sub = [a.email, a.phone].filter(Boolean).join(' · ') || 'No email';
        return '<div class="vm-item"><span class="vm-av">' + esc(initials(a.name)) + '</span>' +
            '<div class="vm-item-main"><strong>' + esc(a.name) + '</strong><small>' + esc(sub) + '</small></div>' +
            '<div class="vm-tags">' + (a.relationship ? '<span class="vm-tag">' + esc(a.relationship) + '</span>' : '') +
            '<span class="vm-tag role">' + esc(a.role || 'Family') + ' account</span></div></div>';
    }
    function contactItem(c) {
        var type = c.contact_type === 'FAMILY' ? 'Family' : (c.contact_type === 'BHW' ? 'BHW' : 'Other');
        return '<div class="vm-item"><span class="vm-av">' + esc(initials(c.full_name)) + '</span>' +
            '<div class="vm-item-main"><strong>' + esc(c.full_name) + '</strong><small>' + esc(c.contact_num) + '</small></div>' +
            '<div class="vm-tags">' + (c.relationship ? '<span class="vm-tag">' + esc(c.relationship) + '</span>' : '') +
            '<span class="vm-tag role">' + type + '</span></div></div>';
    }
    function section(icon, title, items, render, emptyText) {
        return '<section><h4 class="vm-sec-title"><i data-lucide="' + icon + '"></i>' + esc(title) +
            '<span class="vm-count">' + items.length + '</span></h4>' +
            (items.length ? '<div class="vm-list">' + items.map(render).join('') + '</div>'
                : '<div class="vm-note">' + esc(emptyText) + '</div>') + '</section>';
    }

    function renderExtras(html) {
        var box = $('#vmExtra');
        if (!box) {
            box = document.createElement('div');
            box.id = 'vmExtra';
            dl().insertAdjacentElement('afterend', box);
        }
        box.innerHTML = html;
        icons();
    }

    // ---------- foot (Edit / Cancel + Save) ----------
    function foot() {
        var f = $('#vmEditFoot');
        if (!f) {
            f = document.createElement('div');
            f.id = 'vmEditFoot';
            $('#userModal .admin-modal-card').appendChild(f);
        }
        return f;
    }
    function footView() {
        foot().innerHTML = '<div class="vm-foot-msg"></div>' +
            '<button type="button" class="vm-btn vm-btn-primary" id="vmEditBtn"><i data-lucide="pencil"></i> Edit details</button>';
        icons();
    }
    function footEdit() {
        foot().innerHTML = '<div class="vm-foot-msg" id="vmMsg" role="status"></div>' +
            '<button type="button" class="vm-btn vm-btn-ghost" id="vmCancelBtn">Cancel</button>' +
            '<button type="button" class="vm-btn vm-btn-primary" id="vmSaveBtn"><i data-lucide="check"></i> Save changes</button>';
        icons();
    }
    function setMsg(text, kind) {
        var m = $('#vmMsg');
        if (!m) return;
        m.textContent = text || '';
        m.className = 'vm-foot-msg' + (kind ? ' ' + kind : '');
    }

    // ---------- load when the View window opens ----------
    async function begin(row) {
        reset();
        if (!row || !row.dataset.editUrl || !dl()) return;
        state.row = row;
        var seq = ++state.seq;
        renderExtras('<div class="vm-note">Loading accounts and contacts…</div>');
        try {
            var res = await fetch(row.dataset.editUrl, { headers: { 'Accept': 'application/json' } });
            var data = await res.json();
            if (seq !== state.seq) return;
            if (!data.success) throw new Error(data.message || 'Could not load the resident.');
            state.data = data;
            addOccupationTile(data.resident.occupation);
            renderExtras(
                section('users', 'Connected accounts', data.accounts || [], accountItem, 'No family account is connected yet.') +
                section('phone-call', 'Emergency contacts', data.contacts || [], contactItem, 'No emergency contacts saved yet.'));
            footView();
        } catch (x) {
            if (seq !== state.seq) return;
            renderExtras('<div class="vm-note">Could not load the accounts and contacts. Close this window and try again.</div>');
        }
    }

    // ---------- edit in place ----------
    function enterEdit() {
        if (!state.data || state.editing) return;
        var r = state.data.resident;
        state.editing = true;
        state.originals = [];
        $('#userModal .admin-modal-card').classList.add('vm-editing');

        tiles().forEach(function (tile) {
            var label = labelOf(tile);
            var kind = EDITABLE[label];
            var dd = tile.querySelector('dd');
            if (!kind || !dd) {
                tile.classList.add('vm-locked');
                tile.title = 'This can’t be edited here';
                var lock = document.createElement('span');
                lock.className = 'vm-lock';
                lock.innerHTML = '<i data-lucide="lock"></i>';
                tile.appendChild(lock);
                return;
            }
            state.originals.push({ dd: dd, html: dd.innerHTML });
            if (kind === 'name') dd.innerHTML = '<input class="vm-input" data-f="full_name" maxlength="100" autocomplete="off" value="' + esc(r.full_name) + '" />';
            if (kind === 'dob') dd.innerHTML = '<input class="vm-input" data-f="date_of_birth" type="date" min="1900-01-01" value="' + esc(r.date_of_birth) + '" />';
            if (kind === 'occupation') dd.innerHTML = '<input class="vm-input" data-f="occupation" maxlength="80" autocomplete="off" placeholder="Optional" value="' + esc(r.occupation) + '" />';
            if (kind === 'address') dd.innerHTML = '<textarea class="vm-input" data-f="address" rows="3" maxlength="200">' + esc(r.address) + '</textarea>' +
                '<span class="vm-hint">Must stay in Barangay ' + esc(r.barangay) + '.</span>';
        });
        var extra = $('#vmExtra'); if (extra) extra.classList.add('vm-locked');
        footEdit();
        icons();
        var first = $('[data-f="full_name"]'); if (first) first.focus();
    }

    function exitEdit() {
        state.originals.forEach(function (o) { o.dd.innerHTML = o.html; });
        state.originals = [];
        tiles().forEach(function (t) {
            t.classList.remove('vm-locked');
            t.removeAttribute('title');
            var lock = t.querySelector('.vm-lock'); if (lock) lock.remove();
        });
        var extra = $('#vmExtra'); if (extra) extra.classList.remove('vm-locked');
        $('#userModal .admin-modal-card').classList.remove('vm-editing');
        state.editing = false;
        footView();
    }

    function val(name) { var el = $('[data-f="' + name + '"]'); return el ? el.value.trim() : ''; }

    async function save() {
        if (state.saving || !state.editing || !state.row) return;
        var body = { full_name: val('full_name'), date_of_birth: val('date_of_birth'), occupation: val('occupation'), address: val('address') };
        if (body.full_name.length < 2) return setMsg('Enter the resident’s full name.', 'error');
        if (!body.date_of_birth) return setMsg('Enter the date of birth.', 'error');
        if (!body.address) return setMsg('Enter the address.', 'error');

        state.saving = true;
        var btn = $('#vmSaveBtn'); if (btn) btn.disabled = true;
        setMsg('Saving…');
        try {
            var res = await fetch(state.row.dataset.editUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify(body)
            });
            var data = await res.json().catch(function () {
                return { success: false, message: 'Something went wrong on the server. Please try again.' };
            });
            if (data.success) {
                setMsg('Saved.');
                window.location.reload();   // the page shows the "was updated" message
                return;
            }
            setMsg(data.message || 'Could not save the changes.', 'error');
        } catch (x) {
            setMsg('Could not reach the server. Check your connection and try again.', 'error');
        }
        state.saving = false;
        btn = $('#vmSaveBtn'); if (btn) btn.disabled = false;
    }

    // ---------- wiring ----------
    document.addEventListener('click', function (e) {
        var view = e.target.closest('.js-view-details');
        if (view) {
            var row = view.closest('tr');
            setTimeout(function () { begin(row); }, 0);   // after admin.js has built the window
            return;
        }
        if (e.target.closest('#vmEditBtn')) enterEdit();
        else if (e.target.closest('#vmCancelBtn')) exitEdit();
        else if (e.target.closest('#vmSaveBtn')) save();
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && state.editing && e.target.matches && e.target.matches('input[data-f]')) {
            e.preventDefault();
            save();
        }
    });
})();