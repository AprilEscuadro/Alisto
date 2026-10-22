/* ALISTO admin panel behavior */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function icons() {
    if (window.lucide) window.lucide.createIcons();
  }

  // ---------- Top bar dropdowns (profile + notifications) ----------
  function setupDropdown(wrapperId, buttonId, closeOthers = []) {
    const wrapper = $(wrapperId);
    const button = $(buttonId);
    if (!wrapper || !button) return;

    button.addEventListener('click', (e) => {
      e.stopPropagation();
      closeOthers.forEach((id) => { const el = $(id); if (el) el.classList.remove('open'); });
      const open = wrapper.classList.toggle('open');
      button.setAttribute('aria-expanded', String(open));
    });
  }

  function setupProfileMenu() {
    setupDropdown('#adminProfile', '#adminProfileBtn', ['#adminNotif']);
    setupDropdown('#adminNotif', '#adminBellBtn', ['#adminProfile']);
  }

  // ---------- Sidebar on small screens ----------
  function setupSidebar() {
    const sidebar = $('#adminSidebar');
    const backdrop = $('#adminSidebarBackdrop');
    const button = $('#adminMenuBtn');
    if (!sidebar || !button) return;

    const toggle = (open) => {
      sidebar.classList.toggle('open', open);
      backdrop.classList.toggle('open', open);
    };
    button.addEventListener('click', () => toggle(!sidebar.classList.contains('open')));
    backdrop.addEventListener('click', () => toggle(false));
  }

  // ---------- Row action menus (⋮) ----------
  function closeRowMenus(except) {
    $$('.row-menu.open, .export-menu.open').forEach((menu) => {
      if (menu !== except) {
        menu.classList.remove('open');
        $('.row-menu-btn, .export-menu-btn', menu).setAttribute('aria-expanded', 'false');
      }
    });
  }

  // The table scrolls sideways on small screens, which would cut the menu off,
  // so the menu is placed relative to the window instead of the table.
  function placeMenu(button, list) {
    const r = button.getBoundingClientRect();
    const height = list.offsetHeight;
    const openUp = r.bottom + height + 8 > window.innerHeight;
    list.style.position = 'fixed';
    list.style.top = `${openUp ? r.top - height - 4 : r.bottom + 4}px`;
    list.style.left = `${Math.max(8, r.right - list.offsetWidth)}px`;
    list.style.right = 'auto';
  }

  function setupRowMenus() {
    window.addEventListener('scroll', () => closeRowMenus(), true);
    window.addEventListener('resize', () => closeRowMenus());

    document.addEventListener('click', (e) => {
      const button = e.target.closest('.row-menu-btn, .export-menu-btn');
      if (button) {
        const menu = button.closest('.row-menu, .export-menu');
        closeRowMenus(menu);
        const open = menu.classList.toggle('open');
        button.setAttribute('aria-expanded', String(open));
        if (open) placeMenu(button, $('.row-menu-list, .export-menu-list', menu));
        return;
      }
      if (!e.target.closest('.row-menu-list, .export-menu-list')) closeRowMenus();

      const profile = $('#adminProfile');
      if (profile && !e.target.closest('#adminProfile')) profile.classList.remove('open');
      const notif = $('#adminNotif');
      if (notif && !e.target.closest('#adminNotif')) notif.classList.remove('open');
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      closeRowMenus();
      ['#adminProfile', '#adminNotif'].forEach((id) => {
        const el = $(id);
        if (el) el.classList.remove('open');
      });
      closeModal();
    });

    // Ask before rejecting a BHW, removing/adding a serial number, marking
    // a false alarm, or unregistering a device -- via the floating
    // confirmDialog() below instead of the browser's native confirm(),
    // which looks jarring next to the rest of this UI (and the family
    // side already has its own floating version of the same thing, see
    // dashboard.css's .confirm-overlay). confirmDialog() is async, so
    // this always prevents the FIRST submit and, if confirmed, resubmits
    // the form directly with form.submit() -- which (unlike a "submit"
    // click) never re-fires this same listener, so there's no loop.
    document.addEventListener('submit', (e) => {
      const form = e.target;
      let request = null;

      if (form.classList.contains('js-confirm-reject')) {
        request = {
          title: 'Reject this BHW?',
          message: 'They will not be able to sign in.',
          okLabel: 'Reject', danger: true,
        };
      } else if (form.classList.contains('js-confirm-delete-device')) {
        const serial = form.closest('tr').dataset.serial;
        request = {
          title: `Remove ${serial}?`,
          message: 'It will no longer work for registration.',
          okLabel: 'Remove', danger: true,
        };
      } else if (form.classList.contains('js-confirm-false-alarm')) {
        request = {
          title: 'Mark as false alarm?',
          message: 'It will stop showing as active.',
          okLabel: 'Mark False Alarm', danger: true,
        };
      } else if (form.classList.contains('js-confirm-add-devices')) {
        const lines = $('#serial_numbers', form).value.split('\n').map((l) => l.trim()).filter(Boolean);
        const n = lines.length;
        const preview = lines.slice(0, 5).join(', ') + (n > 5 ? `, +${n - 5} more` : '');
        request = {
          title: `Register ${n} serial number${n === 1 ? '' : 's'}?`,
          message: preview, okLabel: 'Register', danger: false,
        };
      } else if (form.classList.contains('js-confirm-unregister')) {
        const serial = form.closest('tr').dataset.serial;
        const elder = form.dataset.elder;
        const who = elder ? `${elder} will no longer have a device linked` : 'the linked elder will no longer have a device';
        request = {
          title: `Unregister ${serial}?`,
          message: `${who}, and the serial becomes available for someone else.`,
          okLabel: 'Unregister', danger: true,
        };
      } else if (form.classList.contains('js-confirm-delete-user')) {
        const kind = form.dataset.type;
        const what = {
          family: 'Their account and profile will be permanently deleted from the website and from Firebase, and they will no longer be able to sign in. Their loved ones and devices are not deleted.',
          bhw: 'Their account, profile and uploaded valid ID will be permanently deleted from the website and from Firebase, and they will no longer be able to sign in.',
          elderly: 'Their profile, emergency contacts, medicine reminders and family links will be permanently deleted, and their device will be unregistered.',
        }[kind] || 'They will be permanently deleted.';
        request = {
          title: `Delete ${form.dataset.name}?`,
          message: `${what} Past alerts and payment records are kept. This cannot be undone.`,
          okLabel: 'Delete permanently', danger: true,
        };
      } else if (form.classList.contains('js-confirm-archive')) {
        request = {
          title: `Archive ${form.dataset.name}?`,
          message: 'They’ll be hidden from the default Users list, but nothing is deleted -- their alerts, subscriptions, and links stay intact, and you can restore them anytime.',
          okLabel: 'Archive', danger: true,
        };
      }

      if (!request) return;
      e.preventDefault();
      confirmDialog(request).then((ok) => { if (ok) form.submit(); });
    });

    // Same floating confirm, for a plain link instead of a form -- e.g.
    // Logout, which just navigates rather than submitting anything.
    document.addEventListener('click', (e) => {
      const link = e.target.closest('.js-confirm-link');
      if (!link) return;
      e.preventDefault();
      confirmDialog({
        title: link.dataset.confirmTitle || 'Are you sure?',
        message: link.dataset.confirmMessage || '',
        okLabel: link.dataset.confirmOk || 'Confirm',
        danger: link.dataset.confirmDanger === 'true',
      }).then((ok) => { if (ok) window.location.href = link.href; });
    });
  }

  // ---------- Floating confirm dialog (replaces window.confirm()) ----------
  function confirmDialog({ title, message, okLabel = 'Confirm', danger = false }) {
    const overlay = $('#adminConfirm');
    if (!overlay) return Promise.resolve(true); // page has no dialog markup -- fail open rather than block the action

    $('#adminConfirmTitle', overlay).textContent = title;
    $('#adminConfirmText', overlay).textContent = message;
    $('#adminConfirmIcon', overlay).className = `confirm-icon${danger ? ' confirm-icon-danger' : ''}`;

    const okBtn = $('#adminConfirmOk', overlay);
    const cancelBtn = $('#adminConfirmCancel', overlay);
    okBtn.textContent = okLabel;
    okBtn.className = `btn ${danger ? 'btn-danger-solid' : 'btn-primary'}`;

    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');

    return new Promise((resolve) => {
      function done(result) {
        overlay.classList.remove('open');
        overlay.setAttribute('aria-hidden', 'true');
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        overlay.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }
      function onOk() { done(true); }
      function onCancel() { done(false); }
      function onBackdrop(e) { if (e.target === overlay) done(false); }
      function onKey(e) { if (e.key === 'Escape') done(false); }

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      overlay.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
    });
  }

  // ---------- User details modal ----------
  const modal = () => $('#userModal');

  function openModal(row) {
    const box = modal();
    if (!box) return;

    // Some tables (the emergency log) have no avatar in the row, so fall back
    // to initials worked out from the name.
    const avatar = $('.admin-avatar-lg', row);
    $('#userModalAvatar').textContent = avatar
      ? avatar.textContent.trim()
      : (row.dataset.name || '?').split(/\s+/).slice(0, 2).map((w) => w[0] || '').join('').toUpperCase();
    $('#userModalAvatar').className = `admin-avatar-lg avatar-${row.dataset.type}`;
    $('#userModalTitle').textContent = row.dataset.name;
    $('#userModalSub').textContent =
      row.dataset.subtitle || `${row.dataset.typeLabel} · ID: ${row.dataset.displayId}`;

    const list = $('#userModalDetails');
    list.innerHTML = '';
    const details = JSON.parse(row.dataset.details || '[]');
    if (!row.dataset.skipMeta) {
      details.push(['Status', row.dataset.status.charAt(0).toUpperCase() + row.dataset.status.slice(1)]);
      details.push(['Joined', row.dataset.joined]);
    }

    details.forEach(([label, value]) => addDetail(list, label, String(value)));

    if (row.dataset.type === 'bhw') {
      const dd = addDetail(list, 'Valid ID', row.dataset.validIdUrl ? '' : 'None uploaded');
      if (row.dataset.validIdUrl) {
        const link = document.createElement('a');
        link.href = row.dataset.validIdUrl;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = row.dataset.validIdName;
        dd.appendChild(link);
      }
    }

    const approval = $('#userModalApproval');
    const pending = row.dataset.status === 'pending' && row.dataset.approvalUrl;
    approval.hidden = !pending;
    if (pending) {
      $('#userModalApprove').action = row.dataset.approvalUrl;
      $('#userModalReject').action = row.dataset.approvalUrl;
    }

    box.hidden = false;
    $('.admin-modal-close', box).focus();
  }

  function addDetail(list, label, value) {
    const wrap = document.createElement('div');
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = label;
    dd.textContent = value;
    wrap.append(dt, dd);
    list.appendChild(wrap);
    return dd;
  }

  function closeModal() {
    const box = modal();
    if (box) box.hidden = true;
  }

  function setupModal() {
    if (!modal()) return;
    document.addEventListener('click', (e) => {
      const view = e.target.closest('.js-view-details');
      if (view) {
        closeRowMenus();
        openModal(view.closest('tr'));
        return;
      }
      if (e.target.closest('[data-close-modal]')) closeModal();
    });
  }

  // ---------- Filterable tables: search, filters, pagination ----------
  // Used by the admin Users table and the BHW Elderly Users table.
  const FILTER_TABLES = [
    {
      table: '#usersTable', search: '#userSearch', noun: 'users',
      filters: { type: '#typeFilter', status: '#statusFilter' }, exportButton: '#exportUsers',
    },
    {
      table: '#elderlyTable', search: '#elderlySearch', noun: 'residents',
      filters: { status: '#deviceStatusFilter' }, clearButton: '#elderlyClearFilters',
    },
    {
      table: '#alertsTable', search: '#alertSearch', noun: 'alerts',
      filters: { group: '#alertGroupFilter' },
    },
    {
      table: '#logsTable', search: '#logSearch', noun: 'alerts',
      filters: { status: '#logStatusFilter' },
      clearButton: '#logClearFilters', exportButton: '#exportLogs',
    },
    {
      table: '#subsTable', search: '#subSearch', noun: 'subscriptions',
      filters: { status: '#subStatusFilter', plan: '#subPlanFilter' },
      clearButton: '#subClearFilters',
    },
    {
      table: '#devicesTable', search: '#deviceSearch', noun: 'serial numbers',
      filters: { status: '#deviceStatusFilter' }, exportButton: '#exportDevices',
      columns: ['serial', 'batch', 'assignedTo', 'status', 'added'],
    },
  ];

  function setupFilterTable(config) {
    const table = $(config.table);
    if (!table) return;

    const PAGE_SIZE = 10;
    const card = table.closest('.admin-table-card');
    const rows = $$('tbody tr:not(.table-empty)', table);
    const empty = $('.table-empty', table);
    const showing = $('.table-footer > span', card);
    const pager = $('.pagination', card);
    const search = $(config.search);
    const filters = Object.entries(config.filters)
      .map(([key, sel]) => [key, $(sel)])
      .filter(([, el]) => el);
    let page = 1;
    let matches = rows;

    // Pre-select filters from the URL, e.g. ?type=bhw&status=pending
    const params = new URLSearchParams(window.location.search);
    filters.forEach(([key, el]) => {
      if (params.get(key)) el.value = params.get(key);
    });

    function applyFilters() {
      const q = search ? search.value.trim().toLowerCase() : '';
      matches = rows.filter((row) =>
        (!q || row.textContent.toLowerCase().includes(q)) &&
        filters.every(([key, el]) => !el.value || row.dataset[key] === el.value)
      );
      page = 1;
      render();
    }

    function render() {
      const total = matches.length;
      const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      page = Math.min(page, pages);
      const start = (page - 1) * PAGE_SIZE;
      const visible = new Set(matches.slice(start, start + PAGE_SIZE));

      rows.forEach((row) => { row.hidden = !visible.has(row); });
      if (empty) empty.hidden = total > 0;
      if (showing) {
        showing.textContent = total
          ? `Showing ${start + 1} to ${Math.min(start + PAGE_SIZE, total)} of ${total} ${config.noun}`
          : `Showing 0 ${config.noun}`;
      }
      if (pager) renderPager(pages);
    }

    function pageList(pages) {
      if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
      const list = [1];
      const from = Math.max(2, page - 1);
      const to = Math.min(pages - 1, page + 1);
      if (from > 2) list.push('…');
      for (let i = from; i <= to; i++) list.push(i);
      if (to < pages - 1) list.push('…');
      list.push(pages);
      return list;
    }

    function pagerButton(label, target, { active = false, disabled = false, aria } = {}) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.innerHTML = label;
      btn.disabled = disabled;
      if (active) {
        btn.classList.add('active');
        btn.setAttribute('aria-current', 'page');
      }
      if (aria) btn.setAttribute('aria-label', aria);
      btn.addEventListener('click', () => {
        page = target;
        render();
      });
      return btn;
    }

    function renderPager(pages) {
      pager.innerHTML = '';
      pager.appendChild(pagerButton('<i data-lucide="chevron-left"></i>', page - 1, { disabled: page === 1, aria: 'Previous page' }));
      pageList(pages).forEach((p) => {
        if (p === '…') {
          const span = document.createElement('span');
          span.textContent = '…';
          pager.appendChild(span);
        } else {
          pager.appendChild(pagerButton(String(p), p, { active: p === page }));
        }
      });
      pager.appendChild(pagerButton('<i data-lucide="chevron-right"></i>', page + 1, { disabled: page === pages, aria: 'Next page' }));
      icons();
    }

    // Reads the table as shown (filtered, but not paginated), so any table
    // can be exported regardless of which columns/format it needs.
    function tableData() {
      const header = $$('thead th', table).map((th) => th.textContent.trim()).filter((t) => t !== 'Actions');
      const rows = matches.map((row) =>
        $$('td', row)
          .filter((td) => !td.classList.contains('col-actions'))
          .map((td) => td.textContent.replace(/\s+/g, ' ').trim())
      );
      return { header, rows };
    }

    function downloadBlob(blob, ext) {
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `alisto-${config.noun}-${new Date().toISOString().slice(0, 10)}.${ext}`;
      link.click();
      URL.revokeObjectURL(link.href);
    }

    function escapeHtml(v) {
      return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function exportCsv() {
      const { header, rows } = tableData();
      const quote = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
      const lines = [header, ...rows].map((r) => r.map(quote).join(','));
      downloadBlob(new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }), 'csv');
    }

    function exportExcel() {
      // An .xls file that's really just an HTML table -- Excel opens this
      // natively, so no spreadsheet library is needed for this to work.
      const { header, rows } = tableData();
      const thead = `<tr>${header.map((h) => `<th>${escapeHtml(h)}</th>`).join('')}</tr>`;
      const tbody = rows.map((r) => `<tr>${r.map((c) => `<td>${escapeHtml(c)}</td>`).join('')}</tr>`).join('');
      const html = `<html><head><meta charset="UTF-8"></head><body><table border="1">${thead}${tbody}</table></body></html>`;
      downloadBlob(new Blob(['\ufeff' + html], { type: 'application/vnd.ms-excel' }), 'xls');
    }

    function exportJson() {
      const { header, rows } = tableData();
      const records = rows.map((r) => Object.fromEntries(header.map((h, i) => [h, r[i]])));
      downloadBlob(new Blob([JSON.stringify(records, null, 2)], { type: 'application/json' }), 'json');
    }

    function exportPdf() {
      // No PDF library on this page either -- open a print-formatted table
      // in a new tab and let the browser's own "Save as PDF" print
      // destination produce the file.
      const { header, rows } = tableData();
      const thead = `<tr>${header.map((h) => `<th>${escapeHtml(h)}</th>`).join('')}</tr>`;
      const tbody = rows.map((r) => `<tr>${r.map((c) => `<td>${escapeHtml(c)}</td>`).join('')}</tr>`).join('');
      const win = window.open('', '_blank');
      if (!win) return;
      win.document.write(`<html><head><title>Alisto ${escapeHtml(config.noun)}</title><style>
        body { font-family: Arial, sans-serif; padding: 24px; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th, td { border: 1px solid #ccc; padding: 6px 8px; text-align: left; }
        th { background: #f1f5f9; }
        h1 { font-size: 16px; margin-bottom: 4px; }
        p { font-size: 12px; color: #666; margin: 0 0 16px; }
      </style></head><body>
        <h1>Alisto &mdash; ${escapeHtml(config.noun)}</h1>
        <p>Exported ${escapeHtml(new Date().toLocaleString())}</p>
        <table>${thead}${tbody}</table>
      </body></html>`);
      win.document.close();
      win.focus();
      win.print();
    }

    const EXPORTERS = { csv: exportCsv, excel: exportExcel, json: exportJson, pdf: exportPdf };

    if (search) search.addEventListener('input', applyFilters);
    filters.forEach(([, el]) => el.addEventListener('change', applyFilters));
    if (config.exportButton && $(config.exportButton)) {
      $$('[data-format]', $(config.exportButton)).forEach((btn) => {
        btn.addEventListener('click', () => {
          const run = EXPORTERS[btn.dataset.format];
          if (run) run();
          $(config.exportButton).classList.remove('open');
        });
      });
    }
    if (config.clearButton && $(config.clearButton)) {
      $(config.clearButton).addEventListener('click', () => {
        if (search) search.value = '';
        filters.forEach(([, el]) => { el.value = ''; });
        applyFilters();
      });
    }

    applyFilters();
  }

  // ---------- Alert filter pills ----------
  // The pills and the "View all active" links both set one hidden <select>,
  // so the shared table code above does the actual filtering.
  function setupAlertFilters() {
    const table = $('#alertsTable');
    if (!table) return;

    const select = document.createElement('select');
    select.id = 'alertGroupFilter';
    select.hidden = true;
    ['', 'active', 'resolved', 'medication', 'false_alarm'].forEach((value) => {
      select.add(new Option(value || 'All', value));
    });
    table.parentElement.appendChild(select);

    function apply(value) {
      select.value = value;
      select.dispatchEvent(new Event('change'));
      $$('#alertPills .pill').forEach((pill) => {
        pill.classList.toggle('active', (pill.dataset.filter || '') === value);
      });
    }

    $$('#alertPills .pill').forEach((pill) => {
      pill.addEventListener('click', () => apply(pill.dataset.filter || ''));
    });
    // Clicking anywhere on a row opens that alert, not just the button.
    $$('tbody tr[data-alert-id]', table).forEach((row) => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('a, button, form')) return;
        const link = $('a[href]', row);
        if (link) window.location = link.href;
      });
    });

    $$('.js-alert-filter').forEach((link) => {
      link.addEventListener('click', () => {
        apply(link.dataset.filter);
        table.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });

    // Open straight to a filter from the URL, e.g. /bhw/alerts?group=active
    const startGroup = new URLSearchParams(window.location.search).get('group');
    if (startGroup) apply(startGroup);
  }

  // ---------- Register serial numbers window ----------
  function setupAddDevices() {
    const modal = $('#addDevicesModal');
    const open = $('#openAddDevices');
    if (!modal || !open) return;

    open.addEventListener('click', () => {
      modal.hidden = false;
      $('#serial_numbers').focus();
    });
    modal.addEventListener('click', (e) => {
      if (e.target.closest('[data-close-modal]')) modal.hidden = true;
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') modal.hidden = true;
    });

    const box = $('#serial_numbers');
    const count = $('#serialCount');
    box.addEventListener('input', () => {
      const n = box.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean).length;
      count.textContent = n ? `${n} serial number${n === 1 ? '' : 's'} typed.` : '';
    });
  }

  // ---------- Add User (BHW) window ----------
  function setupAddUser() {
    const modal = $('#addUserModal');
    const open = $('#openAddUser');
    const form = $('#addUserForm');
    const errorBox = $('#addUserError');
    if (!modal || !open || !form) return;

    open.addEventListener('click', () => {
      modal.hidden = false;
      $('input[name="full_name"]', modal).focus();
    });
    modal.addEventListener('click', (e) => {
      if (e.target.closest('[data-close-modal]')) modal.hidden = true;
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') modal.hidden = true;
    });

    // Submitted via fetch() instead of a plain POST so a validation error
    // (e.g. duplicate email) shows up right inside the still-open modal --
    // a plain form submit would redirect to a fresh page with the error
    // only as a flash banner at the very top, easy to miss while scrolled
    // down filling a long form (real testing: looked like the button just
    // did nothing).
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errorBox.hidden = true;
      const button = $('button[type="submit"]', form);
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = 'Adding...';

      try {
        const res = await fetch(form.action, { method: 'POST', body: new FormData(form) });
        const data = await res.json().catch(() => ({ success: false, message: 'Something went wrong on the server. Please try again.' }));
        if (data.success) {
          window.location.reload();
          return; // leave the button disabled -- the page is about to reload anyway
        }
        errorBox.textContent = data.message || 'Could not add this user. Please check the form.';
        errorBox.hidden = false;
        errorBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } catch (err) {
        errorBox.textContent = 'Could not reach the server. Check your connection and try again.';
        errorBox.hidden = false;
      }
      button.disabled = false;
      button.textContent = originalText;
    });
  }

  // ---------- Edit profile (swap the details list for a form) ----------
  function setupProfileEdit() {
    const edit = $('#editProfileBtn');
    const form = $('#profileForm');
    const view = $('#profileView');
    const cancel = $('#cancelEditBtn');
    if (!edit || !form || !view) return;

    const toggle = (editing) => {
      form.hidden = !editing;
      view.hidden = editing;
      edit.hidden = editing;
      if (editing) form.querySelector('input').focus();
    };

    edit.addEventListener('click', () => toggle(true));
    if (cancel) cancel.addEventListener('click', () => toggle(false));
  }

  // ---------- Show/hide password buttons ----------
  function setupPasswordToggles() {
    document.addEventListener('click', (e) => {
      const button = e.target.closest('[data-toggle-password]');
      if (!button) return;
      const input = document.getElementById(button.dataset.togglePassword);
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      button.innerHTML = `<i data-lucide="${show ? 'eye-off' : 'eye'}"></i>`;
      icons();
    });
  }

  // ---------- Live password requirement checklist ----------
  function setupPasswordChecklist() {
    const input = $('#new_password');
    const list = $('#passwordRules');
    if (!input || !list) return;
    const tests = {
      length: (p) => p.length >= 8,
      case: (p) => /[a-z]/.test(p) && /[A-Z]/.test(p),
      number: (p) => /\d/.test(p),
      special: (p) => /[^A-Za-z0-9]/.test(p),
    };
    input.addEventListener('input', () => {
      $$('[data-rule]', list).forEach((li) => {
        li.classList.toggle('met', tests[li.dataset.rule](input.value));
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupProfileMenu();
    setupSidebar();
    setupRowMenus();
    setupModal();
    setupAlertFilters();   // creates the hidden select the alerts table filters on
    FILTER_TABLES.forEach(setupFilterTable);
    setupAddDevices();
    setupAddUser();
    setupProfileEdit();
    setupPasswordToggles();
    setupPasswordChecklist();
    icons();
  });
})();