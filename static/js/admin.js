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
      } else if (form.classList.contains('js-confirm-bulk')) {
        const n = Number($('#bulkCount').textContent) || 0;
        const what = `${n} alert${n === 1 ? '' : 's'}`;
        const copy = {
          'acknowledge': {
            title: `Mark ${what} as Responding?`,
            message: 'This confirms that you are responding to these residents.',
            okLabel: 'Yes, Responding', danger: false,
          },
          'resolve': {
            title: `Mark ${what} as Resolved?`,
            message: 'They will be closed as resolved. This cannot be undone.',
            okLabel: 'Yes, Resolved', danger: false,
          },
          'false-alarm': {
            title: `Mark ${what} as False Alarm?`,
            message: 'They will be closed as false alarms and stop showing as active. This cannot be undone.',
            okLabel: 'Yes, False Alarm', danger: true,
          },
        };
        request = copy[$('#bulkAction').value] || null;
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
      filters: { type: '#typeFilter', barangay: '#barangayFilter', status: '#statusFilter', plan: '#subscriptionFilter' }, exportButton: '#exportUsers',
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
      filters: {
        status: '#deviceStatusFilter', barangay: '#deviceBarangayFilter',
        assigned: '#deviceAssignedFilter', load: '#deviceLoadFilter', sim: '#deviceSimFilter',
      },
      clearButton: '#deviceClearFilters', exportButton: '#exportDevices',
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
    ['', 'active', 'responding', 'resolved', 'medication', 'false_alarm'].forEach((value) => {
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
        if (e.target.closest('a, button, form, input, label, .col-select')) return;
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

  // ---------- Alerts: tick rows, then Responding / Resolved / False Alarm ----------
  function setupBulkAlerts() {
    const table = $('#alertsTable');
    const bar = $('#bulkForm');
    const all = $('#alertSelectAll');
    if (!table || !bar || !all) return;

    const boxes = () => $$('tbody .alert-check', table);
    const visibleBoxes = () => boxes().filter((b) => !b.closest('tr').hidden);

    function sync() {
      const checked = boxes().filter((b) => b.checked);
      bar.hidden = checked.length === 0;
      $('#bulkCount').textContent = checked.length;
      const vis = visibleBoxes();
      all.checked = vis.length > 0 && vis.every((b) => b.checked);
      all.indeterminate = !all.checked && vis.some((b) => b.checked);
      boxes().forEach((b) => b.closest('tr').classList.toggle('row-checked', b.checked));
    }

    // Select-all only ticks rows you can see (current filter + page).
    all.addEventListener('change', () => {
      visibleBoxes().forEach((b) => { b.checked = all.checked; });
      sync();
    });
    table.addEventListener('change', (e) => {
      if (e.target.classList.contains('alert-check')) sync();
    });

    // When a filter, search or page change hides rows, un-tick them so a
    // hidden alert can never be updated by accident.
    new MutationObserver(() => {
      boxes().forEach((b) => { if (b.closest('tr').hidden) b.checked = false; });
      sync();
    }).observe(table.tBodies[0], { attributes: true, attributeFilter: ['hidden'], subtree: true });

    bar.addEventListener('click', (e) => {
      if (e.target.closest('#bulkClear')) {
        boxes().forEach((b) => { b.checked = false; });
        sync();
        return;
      }
      const btn = e.target.closest('[data-bulk]');
      if (!btn) return;
      $('#bulkAction').value = btn.dataset.bulk;
      bar.requestSubmit();
    });

    sync();
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

  // ---------- Add User (Family or BHW) window ----------
  // Same fields as the public registration page (templates/register.html).
  // The server (admin.add_user) reuses the registration rules, so the checks
  // are identical and a BHW added here is approved right away.
  function setupAddUser() {
    const modal = $('#addUserModal');
    const open = $('#openAddUser');
    const form = $('#addUserForm');
    const errorBox = $('#addUserError');
    if (!modal || !open || !form) return;

    const roleSel = $('#au_role');
    const pick = $('#auPick');
    const famBox = $('#auFamily');
    const bhwBox = $('#auBhw');
    const bhwBrgyBox = $('#auBhwBarangay');
    const addressBox = $('#auAddress');
    const pw = $('#au_password');
    const pw2 = $('#au_confirm');
    const deviceIn = $('#au_device_id');
    const deviceMsg = $('#auDeviceMsg');
    const joinNotice = $('#auJoinNotice');
    const joinMode = $('#au_join_mode');
    const existingElder = $('#au_existing_elder_id');
    const seniorName = $('#au_senior_name');
    const seniorDob = $('#au_dob');
    const relSelect = $('#au_rel_select');
    const relOtherWrap = $('#auRelOtherWrap');
    const relOther = $('#au_rel_other');
    const relHidden = $('#au_relationship');
    const bhwBarangay = $('#au_barangay_assigned');
    const healthCenter = $('#au_health_center');
    const validId = $('#au_valid_id');
    const DEFAULT_HINT = 'The Device ID is on the ALISTO device or its package.';

    let healthCentersByBarangay = {};
    let barangaysLoaded = false;
    let checkedDevice = '';      // the Device ID that passed /check_device
    let joining = false;

    function show(box, on) {
      box.hidden = !on;
      box.disabled = !on;        // a disabled fieldset is not validated or sent
    }

    function fillSelect(select, placeholder, values) {
      select.replaceChildren();
      const first = document.createElement('option');
      first.value = '';
      first.textContent = placeholder;
      select.appendChild(first);
      values.forEach((v) => {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v;
        select.appendChild(o);
      });
    }

    // ----- barangays + health centers come from the same /barangays the registration page uses
    async function loadBarangays() {
      if (barangaysLoaded) return;
      try {
        const res = await fetch('/barangays');
        const data = await res.json();
        if (!data.success || !data.barangays.length) throw new Error('empty');
        healthCentersByBarangay = data.health_centers || {};
        fillSelect($('#au_family_barangay'), 'Select barangay', data.barangays);
        fillSelect($('#au_barangay'), 'Select barangay', data.barangays);
        fillSelect(bhwBarangay, 'Select assigned barangay', data.barangays);
        barangaysLoaded = true;
      } catch (err) {
        errorBox.textContent = 'Could not load the barangay list. Close this window and try again.';
        errorBox.hidden = false;
      }
    }

    function updateHealthCenters() {
      const centers = healthCentersByBarangay[bhwBarangay.value] || [];
      fillSelect(healthCenter, bhwBarangay.value ? 'Select your health center' : 'Select your barangay first', centers);
      if (centers.length === 1) healthCenter.value = centers[0];
    }
    bhwBarangay.addEventListener('change', updateHealthCenters);

    // ----- role switches the right-hand side
    function applyRole() {
      const role = roleSel.value;
      pick.hidden = !!role;
      show(famBox, role === 'family');
      show(bhwBox, role === 'bhw');
      show(bhwBrgyBox, role === 'bhw');
      $('#auLengthText').textContent = role === 'bhw' ? 'Be at least 8 characters' : 'Be at least 6 characters';
      updateRules();
    }
    roleSel.addEventListener('change', applyRole);

    // ----- live password checklist
    function updateRules() {
      const min = roleSel.value === 'bhw' ? 8 : 6;
      const p = pw.value;
      const tests = {
        length: p.length >= min,
        upper: /[A-Z]/.test(p),
        lower: /[a-z]/.test(p),
        number: /\d/.test(p),
        special: /[^A-Za-z0-9]/.test(p),
        match: !!p && p === pw2.value,
      };
      $$('#auRules [data-rule]').forEach((li) => li.classList.toggle('met', tests[li.dataset.rule]));
    }
    pw.addEventListener('input', updateRules);
    pw2.addEventListener('input', updateRules);

    // ----- relationship: same stored text as the registration page
    function updateRelationship() {
      const isOther = relSelect.value === 'other';
      relOtherWrap.hidden = !isOther;
      relOther.required = isOther;
      if (isOther) {
        const specify = relOther.value.trim();
        relHidden.value = specify ? `Other (${specify})` : 'Other';
      } else {
        const opt = relSelect.options[relSelect.selectedIndex];
        relHidden.value = opt && opt.value ? opt.text : '';
      }
    }
    relSelect.addEventListener('change', updateRelationship);
    relOther.addEventListener('input', updateRelationship);

    // ----- device check (same /check_device as registration)
    function lock(el, on) {
      el.readOnly = on;
      el.classList.toggle('is-locked', on);
    }

    function clearJoin() {
      joining = false;
      joinMode.value = 'claim';
      existingElder.value = '';
      lock(seniorName, false);
      lock(seniorDob, false);
      joinNotice.hidden = true;
      show(addressBox, true);
    }

    function setDeviceMsg(text, bad) {
      deviceMsg.textContent = text;
      deviceMsg.classList.toggle('is-bad', !!bad);
    }

    async function checkDevice() {
      const id = deviceIn.value.trim();
      if (!id) { setDeviceMsg('Enter a Device ID.', true); return false; }
      const button = $('#auCheckDevice');
      button.disabled = true;
      button.textContent = 'Checking...';
      try {
        const res = await fetch('/check_device', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: id, role: 'family' }),
        });
        const data = await res.json();
        if (!data.valid) {
          setDeviceMsg(data.message || 'Invalid Device ID.', true);
          return false;
        }
        if (data.already_registered && !data.can_join) {
          setDeviceMsg(data.message || 'This device has no free family slots left.', true);
          return false;
        }
        if (data.already_registered) {
          // Another family member already set it up: join that loved one.
          joining = true;
          joinMode.value = 'join';
          existingElder.value = data.elder_id || '';
          seniorName.value = data.elder_full_name || '';
          seniorDob.value = data.elder_dob || '';
          lock(seniorName, true);
          lock(seniorDob, true);
          joinNotice.textContent = `This device is already registered to ${data.elder_full_name || 'this loved one'}. ` +
            `Their details are filled in. Just pick how this person is related. ` +
            `(${data.family_count} of ${data.family_limit} family slots used.)`;
          joinNotice.hidden = false;
          show(addressBox, false);   // the address is already on file
        } else {
          clearJoin();
        }
        checkedDevice = id;
        setDeviceMsg('Device ID verified.', false);
        return true;
      } catch (err) {
        setDeviceMsg('Could not check the Device ID. Try again.', true);
        return false;
      } finally {
        button.disabled = false;
        button.textContent = 'Check';
      }
    }
    $('#auCheckDevice').addEventListener('click', checkDevice);
    deviceIn.addEventListener('input', () => {
      if (deviceIn.value.trim() !== checkedDevice) {
        checkedDevice = '';
        clearJoin();
        setDeviceMsg(DEFAULT_HINT, false);
      }
    });

    // ----- open / close
    open.addEventListener('click', () => {
      modal.hidden = false;
      loadBarangays();
      $('#au_full_name').focus();
    });
    modal.addEventListener('click', (e) => {
      if (e.target.closest('[data-close-modal]')) modal.hidden = true;
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') modal.hidden = true;
    });

    // ----- submit
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errorBox.hidden = true;
      const role = roleSel.value;

      if (validId && validId.files[0] && validId.files[0].size > 700 * 1024) {
        errorBox.textContent = 'Valid ID must be 700 KB or smaller.';
        errorBox.hidden = false;
        return;
      }

      const button = $('button[type="submit"]', form);
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = 'Adding...';

      // A family account needs a verified Device ID (this also fills join mode).
      if (role === 'family' && deviceIn.value.trim() !== checkedDevice) {
        const ok = await checkDevice();
        if (!ok) {
          errorBox.textContent = deviceMsg.textContent;
          errorBox.hidden = false;
          button.disabled = false;
          button.textContent = originalText;
          return;
        }
        // join mode just locked the loved one's name/date, re-check what is required
        if (!form.reportValidity()) {
          button.disabled = false;
          button.textContent = originalText;
          return;
        }
      }
      updateRelationship();

      try {
        const res = await fetch(form.action, { method: 'POST', body: new FormData(form) });
        const data = await res.json().catch(() => ({ success: false, message: 'Something went wrong on the server. Please try again.' }));
        if (data.success) {
          window.location.reload();
          return; // leave the button disabled, the page is about to reload
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

    applyRole();
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
    setupBulkAlerts();
    FILTER_TABLES.forEach(setupFilterTable);
    setupAddDevices();
    setupAddUser();
    setupProfileEdit();
    setupPasswordToggles();
    setupPasswordChecklist();
    icons();
  });
})();

/* ===== ALISTO admin: user profile modal (click a row on Users page) ===== */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const modal = $('#upModal');
  if (!modal) return;

  const card = $('.up-card', modal);
  const body = $('#upBody');
  const foot = $('#upFoot');
  let row = null;

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icons = () => { if (window.lucide) window.lucide.createIcons(); };
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : '');
  const has = (v) => v && v !== '—' && v !== 'No device';
  const lines = (v) => (has(v) ? String(v).split('\n').filter(Boolean) : []);
  const options = JSON.parse(($('#upOptions') || { textContent: '{}' }).textContent);

  const TYPE_LABEL = { elderly: 'Elderly', family: 'Family member', bhw: 'Barangay Health Worker' };
  const TYPE_ICON = { elderly: 'heart-pulse', family: 'users', bhw: 'shield-check' };
  const STATUS_ICON = { active: 'circle-check', pending: 'clock', rejected: 'circle-x', archived: 'archive' };

  // ---------- small builders ----------
  function infoRow(icon, label, value, opts = {}) {
    const val = has(value)
      ? `<span class="up-row-value${opts.mono ? ' up-mono' : ''}">${esc(value)}</span>`
      : '<span class="up-row-value up-empty">Not provided</span>';
    return `<div class="up-row${opts.stack ? ' stack' : ''}"><span class="up-row-label"><i data-lucide="${icon}"></i>${esc(label)}</span>${val}</div>`;
  }

  function section(icon, title, inner, extra = '') {
    return `<section class="up-section ${extra}">
      <h3 class="up-section-title"><i data-lucide="${icon}"></i>${esc(title)}</h3>${inner}</section>`;
  }

  function person(name, sub, tone) {
    const initials = name.split(/\s+/).slice(0, 2).map((w) => w[0] || '').join('').toUpperCase();
    return `<div class="up-person">
      <span class="up-person-avatar tone-${tone}">${esc(initials)}</span>
      <div><strong>${esc(name)}</strong>${sub ? `<small>${esc(sub)}</small>` : ''}</div></div>`;
  }

  function planBadge(ds) {
    if (!ds.plan || ds.plan === 'na') return '';
    const until = ds.planUntil ? `<small class="up-plan-until">${esc(ds.planUntil)}</small>` : '';
    return `<div class="up-plan"><span class="badge badge-${esc(ds.planBadge || 'archived')}">${esc(ds.planLabel)}</span>${until}</div>`;
  }

  // ---------- VIEW MODE ----------
  function renderHero() {
    const ds = row.dataset;
    const av = $('#upAvatar');
    av.textContent = $('.admin-avatar-lg', row).textContent.trim();
    av.className = `up-avatar avatar-${ds.type}`;
    $('#upName').textContent = ds.name;
    card.dataset.type = ds.type;
    $('#upBadges').innerHTML =
      `<span class="up-pill up-pill-type"><i data-lucide="${TYPE_ICON[ds.type]}"></i>${esc(TYPE_LABEL[ds.type] || ds.typeLabel)}</span>
       <span class="up-pill up-pill-${esc(ds.status)}"><i data-lucide="${STATUS_ICON[ds.status] || 'circle'}"></i>${esc(cap(ds.status))}</span>
       <span class="up-pill up-pill-id">ID · ${esc(ds.displayId)}</span>`;
  }

  function renderView() {
    const ds = row.dataset;
    const d = Object.fromEntries(JSON.parse(ds.details || '[]'));
    renderHero();
    card.classList.remove('is-editing');

    let personal = '';
    if ('Date of Birth' in d) personal += infoRow('calendar', 'Date of birth', d['Date of Birth']);
    if ('Email Address' in d) personal += infoRow('mail', 'Email', d['Email Address']);
    if ('Phone Number' in d) personal += infoRow('phone', 'Phone', d['Phone Number']);
    if ('Address' in d) personal += infoRow('map-pin', 'Address', d['Address'], { stack: true });
    personal += infoRow('calendar-plus', 'Member since', ds.joined);
    const personalSec = section('user-round', 'Personal information', `<div class="up-rows">${personal}</div>`);

    const plan = planBadge(ds);
    const planSec = plan ? section('credit-card', 'Care plan', plan) : '';
    const empty = (icon, text) => `<div class="up-note"><i data-lucide="${icon}"></i>${esc(text)}</div>`;

    let left = personalSec;
    let right = '';

    if (ds.type === 'elderly') {
      const serials = has(d['Device Serial']) ? d['Device Serial'].split(',').map((s) => s.trim()) : [];
      left += section('cpu', 'ALISTO device', serials.length
        ? `<div class="up-rows">${serials.map((s) => infoRow('radio', 'Device ID', s, { mono: true })).join('')}</div>`
        : empty('plug-zap', 'No device registered yet.'));
      const fam = lines(d['Linked Family']);
      right += section('users', 'Linked family', fam.length
        ? `<div class="up-stack">${fam.map((n) => person(n, 'Family member', 'blue')).join('')}</div>`
        : empty('user-x', 'No family linked.'));
      right += planSec;
    } else if (ds.type === 'family') {
      left += planSec;
      const elders = lines(d['Linked Elder']);
      const serials = (d['Device Serial'] || '').split('\n');
      right += section('heart-pulse', 'Loved ones', elders.length
        ? `<div class="up-stack">${elders.map((n, i) => person(n, has(serials[i]) ? `Device · ${serials[i]}` : 'No device', 'blue')).join('')}</div>`
        : empty('user-x', 'No loved ones linked.'));
    } else {
      right += section('map-pinned', 'Assignment', `<div class="up-rows">
        ${infoRow('map-pin', 'Barangay', d['Barangay'])}
        ${infoRow('hospital', 'Health center', d['Health Center'])}
        ${infoRow('award', 'Years of service', d['Years of Service'])}
        ${infoRow('id-card', 'BHW ID number', d['BHW ID Number'], { mono: true })}
      </div>`);
      left += section('badge-check', 'Verification', ds.validIdUrl
        ? `<a class="up-file" href="${esc(ds.validIdUrl)}" target="_blank" rel="noopener"><i data-lucide="file-check-2"></i><span>${esc(ds.validIdName)}</span><i data-lucide="external-link"></i></a>`
        : empty('file-x-2', 'No valid ID uploaded.'));
    }

    body.innerHTML = `<div class="up-col">${left}</div><div class="up-col">${right}</div>`;
    renderFooterView();
    icons();
  }

  function renderFooterView() {
    const ds = row.dataset;
    const del = `<form method="POST" action="${esc(ds.deleteUrl)}" class="js-confirm-delete-user" data-name="${esc(ds.name)}" data-type="${esc(ds.type)}">
        <input type="hidden" name="type" value="${esc(ds.type)}" />
        <button type="submit" class="up-btn up-btn-danger"><i data-lucide="trash-2"></i>Delete</button></form>`;
    const restore = ds.status === 'archived'
      ? `<form method="POST" action="${esc(ds.archiveUrl)}"><input type="hidden" name="type" value="${esc(ds.type)}" />
         <button type="submit" class="up-btn up-btn-ghost"><i data-lucide="rotate-ccw"></i>Restore</button></form>` : '';
    const pending = ds.status === 'pending' && ds.approvalUrl
      ? `<form method="POST" action="${esc(ds.approvalUrl)}" class="js-confirm-reject"><input type="hidden" name="action" value="reject" />
           <button type="submit" class="up-btn up-btn-outline-danger"><i data-lucide="x"></i>Reject</button></form>
         <form method="POST" action="${esc(ds.approvalUrl)}"><input type="hidden" name="action" value="approve" />
           <button type="submit" class="up-btn up-btn-approve"><i data-lucide="check"></i>Approve</button></form>` : '';
    foot.innerHTML = `<div class="up-foot-left">${del}${restore}</div>
      <div class="up-foot-right">${pending}
        <button type="button" class="up-btn up-btn-dark" id="upEditBtn"><i data-lucide="pencil"></i>Edit</button></div>`;
  }

  // ---------- EDIT MODE ----------
  function field(name, label, value, opts = {}) {
    const type = opts.type || 'text';
    const cls = `up-field${opts.full ? ' full' : ''}`;
    if (opts.options) {
      const list = opts.options.map((o) =>
        `<option value="${esc(o)}"${o === value ? ' selected' : ''}>${esc(o)}</option>`).join('');
      return `<label class="${cls}"><span>${esc(label)}</span>
        <select name="${name}" required><option value="" disabled${value ? '' : ' selected'}>Select…</option>${list}</select></label>`;
    }
    return `<label class="${cls}"><span>${esc(label)}</span>
      <input type="${type}" name="${name}" value="${esc(value)}"${opts.required ? ' required' : ''}${opts.disabled ? ' disabled' : ''}${opts.placeholder ? ` placeholder="${esc(opts.placeholder)}"` : ''} />
      ${opts.hint ? `<em>${esc(opts.hint)}</em>` : ''}</label>`;
  }

  function renderEdit() {
    const ds = row.dataset;
    const e = JSON.parse(ds.edit || '{}');
    card.classList.add('is-editing');
    let personal = field('full_name', 'Full name', e.full_name, { required: true, full: true });
    if (ds.type === 'elderly') {
      personal += field('date_of_birth', 'Date of birth', e.date_of_birth, { type: 'date' });
      if (e.address !== null && e.address !== undefined) personal += field('address', 'Address', e.address, { full: true });
    } else {
      personal += field('email_ro', 'Email', ds.email, { disabled: true, hint: 'Email is the login, so it can’t be changed here.' });
      personal += field('contact_number', 'Phone number', e.contact_number, { type: 'tel', placeholder: '09XX XXX XXXX' });
      if (ds.type === 'bhw') personal += field('date_of_birth', 'Date of birth', e.date_of_birth, { type: 'date' });
    }
    let html = `<form id="upForm" class="up-form" autocomplete="off">
      <input type="hidden" name="type" value="${esc(ds.type)}" />
      <div class="up-alert" id="upError" hidden></div>
      ${section('user-round', 'Personal information', `<div class="up-grid">${personal}</div>`)}`;
    if (ds.type === 'bhw') {
      html += section('map-pinned', 'Assignment', `<div class="up-grid">
        ${field('barangay_assigned', 'Barangay assigned', e.barangay_assigned, { options: options.barangays || [] })}
        ${field('years_of_service', 'Years of service', e.years_of_service, { options: options.years || [] })}
        ${field('bhw_id_number', 'BHW ID number', e.bhw_id_number, { required: true, full: true })}
      </div>`);
    }
    html += '</form>';
    body.innerHTML = html;
    foot.innerHTML = `<div class="up-foot-left"></div><div class="up-foot-right">
      <button type="button" class="up-btn up-btn-ghost" id="upCancelBtn">Cancel</button>
      <button type="submit" form="upForm" class="up-btn up-btn-dark" id="upSaveBtn"><i data-lucide="check"></i>Save changes</button></div>`;
    icons();
    const first = $('input[name="full_name"]', body);
    if (first) first.focus();
  }

  async function save(e) {
    e.preventDefault();
    const form = e.target;
    const err = $('#upError');
    const btn = $('#upSaveBtn');
    err.hidden = true;
    btn.disabled = true;
    btn.lastChild.textContent = 'Saving…';
    try {
      const res = await fetch(row.dataset.editUrl, { method: 'POST', body: new FormData(form) });
      const data = await res.json().catch(() => ({ success: false, message: 'Something went wrong on the server. Please try again.' }));
      if (data.success) { window.location.reload(); return; }
      err.textContent = data.message || 'Could not save the changes.';
    } catch (x) {
      err.textContent = 'Could not reach the server. Check your connection and try again.';
    }
    err.hidden = false;
    btn.disabled = false;
    btn.lastChild.textContent = 'Save changes';
  }

  // ---------- open / close ----------
  function open(tr) {
    row = tr;
    renderView();
    modal.hidden = false;
    document.body.classList.add('up-lock');
    $('.up-close', modal).focus();
  }
  function close() {
    modal.hidden = true;
    document.body.classList.remove('up-lock');
    row = null;
  }

  document.addEventListener('click', (e) => {
    // Menu item "View details" and a click anywhere on the row both open it.
    const viewBtn = e.target.closest('.js-open-profile');
    if (viewBtn) {
      const menu = viewBtn.closest('.row-menu');
      if (menu) menu.classList.remove('open');
      open(viewBtn.closest('tr'));
      return;
    }
    const tr = e.target.closest('#usersTable tbody tr[data-details]');
    if (tr && !e.target.closest('.col-actions, a, button, form, input, select, label')) { open(tr); return; }

    if (modal.hidden) return;
    if (e.target.closest('[data-up-close]')) close();
    else if (e.target.closest('#upEditBtn')) renderEdit();
    else if (e.target.closest('#upCancelBtn')) renderView();
  });
  document.addEventListener('submit', (e) => { if (e.target.id === 'upForm') save(e); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hidden) close();
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('#usersTable tbody tr[data-details]')) {
      e.preventDefault(); open(e.target);
    }
  });
  // make rows keyboard-reachable
  $$('#usersTable tbody tr[data-details]').forEach((tr) => { tr.tabIndex = 0; });
})();