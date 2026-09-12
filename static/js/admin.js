/* ALISTO admin panel behavior */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function icons() {
    if (window.lucide) window.lucide.createIcons();
  }

  // ---------- Profile dropdown ----------
  function setupProfileMenu() {
    const profile = $('#adminProfile');
    const button = $('#adminProfileBtn');
    if (!profile || !button) return;

    button.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = profile.classList.toggle('open');
      button.setAttribute('aria-expanded', String(open));
    });
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
    $$('.row-menu.open').forEach((menu) => {
      if (menu !== except) {
        menu.classList.remove('open');
        $('.row-menu-btn', menu).setAttribute('aria-expanded', 'false');
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
      const button = e.target.closest('.row-menu-btn');
      if (button) {
        const menu = button.closest('.row-menu');
        closeRowMenus(menu);
        const open = menu.classList.toggle('open');
        button.setAttribute('aria-expanded', String(open));
        if (open) placeMenu(button, $('.row-menu-list', menu));
        return;
      }
      if (!e.target.closest('.row-menu-list')) closeRowMenus();

      const profile = $('#adminProfile');
      if (profile && !e.target.closest('#adminProfile')) profile.classList.remove('open');
    });

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      closeRowMenus();
      const profile = $('#adminProfile');
      if (profile) profile.classList.remove('open');
      closeModal();
    });

    // Ask before rejecting a BHW or removing a serial number
    document.addEventListener('submit', (e) => {
      if (e.target.classList.contains('js-confirm-reject') &&
          !confirm('Reject this BHW registration? They will not be able to sign in.')) {
        e.preventDefault();
      }
      if (e.target.classList.contains('js-confirm-delete-device')) {
        const serial = e.target.closest('tr').dataset.serial;
        if (!confirm(`Remove serial number ${serial}? It will no longer work for registration.`)) {
          e.preventDefault();
        }
      }
      if (e.target.classList.contains('js-confirm-unregister')) {
        const serial = e.target.closest('tr').dataset.serial;
        const elder = e.target.dataset.elder;
        const who = elder ? `${elder} will no longer have a device linked` : 'the linked elder will no longer have a device';
        if (!confirm(`Unregister ${serial}? ${who}, and the serial becomes available for someone else.`)) {
          e.preventDefault();
        }
      }
    });
  }

  // ---------- User details modal ----------
  const modal = () => $('#userModal');

  function openModal(row) {
    const box = modal();
    if (!box) return;

    $('#userModalAvatar').textContent = $('.admin-avatar-lg', row).textContent;
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

    function exportCsv() {
      // Read the table as shown, so any table can be exported.
      const header = $$('thead th', table).map((th) => th.textContent.trim()).filter((t) => t !== 'Actions');
      const quote = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
      const lines = [header.map(quote).join(',')];
      matches.forEach((row) => {
        const cells = $$('td', row)
          .filter((td) => !td.classList.contains('col-actions'))
          .map((td) => td.textContent.replace(/\s+/g, ' ').trim());
        lines.push(cells.map(quote).join(','));
      });
      const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `alisto-${config.noun}-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(link.href);
    }

    if (search) search.addEventListener('input', applyFilters);
    filters.forEach(([, el]) => el.addEventListener('change', applyFilters));
    if (config.exportButton && $(config.exportButton)) $(config.exportButton).addEventListener('click', exportCsv);
    if (config.clearButton && $(config.clearButton)) {
      $(config.clearButton).addEventListener('click', () => {
        if (search) search.value = '';
        filters.forEach(([, el]) => { el.value = ''; });
        applyFilters();
      });
    }

    applyFilters();
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
    FILTER_TABLES.forEach(setupFilterTable);
    setupAddDevices();
    setupPasswordToggles();
    setupPasswordChecklist();
    icons();
  });
})();