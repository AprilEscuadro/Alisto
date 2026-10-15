// Toggles the mobile sidebar drawer (see .sidebar.open in dashboard.css)
// -- shared across every family page, since each one carries its own
// copy of the same header/sidebar markup (no shared base template for
// this section).
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.querySelector('.menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  if (!toggle || !sidebar) return;

  toggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
  });

  // Tapping anywhere outside the open drawer closes it again.
  document.addEventListener('click', (e) => {
    if (sidebar.classList.contains('open') &&
      !sidebar.contains(e.target) &&
      !toggle.contains(e.target)) {
      sidebar.classList.remove('open');
    }
  });
});

// Inbox badge in the sidebar: refreshed every 30 seconds while the tab is
// visible, so new replies show up without reloading the page.
(function () {
  const badges = () => document.querySelectorAll('[data-inbox-badge]');
  if (!badges().length) return;

  window.setInboxBadge = function (count) {
    badges().forEach((b) => {
      b.textContent = count > 99 ? '99+' : String(count);
      b.hidden = !count;
    });
  };

  async function refresh() {
    if (document.hidden) return;
    try {
      const res = await fetch('/inbox/api/unread', { headers: { Accept: 'application/json' } });
      if (!res.ok) return;
      const data = await res.json();
      if (data.success) window.setInboxBadge(data.unread);
    } catch (e) { /* offline: keep the last count */ }
  }

  setInterval(refresh, 30000);
  document.addEventListener('visibilitychange', refresh);
})();