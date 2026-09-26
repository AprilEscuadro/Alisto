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
