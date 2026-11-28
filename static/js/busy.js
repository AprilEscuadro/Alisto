/* ============================================================
   Busy indicator + double-submit guard, added to every page by app.py.

   HCI choices:
   - Instant feedback: the button that was pressed shows a spinner and
     "Please wait..." right away, and a thin progress bar runs along the top.
   - One indicator at a time: if the pressed button shows progress (ours or
     the page's own, like "Signing In..."), that is the indicator -- no
     second spinner and no overlay. The "Please wait" overlay is only used
     when there is no button to show it on, and only after ~0.6 s so quick
     actions don't flash a screen.
   - Prevents spamming: while an action is running, the same form/button
     can't be sent again (extra clicks are ignored).
   - Accessible: the message is announced to screen readers (role=status),
     buttons get aria-busy, and motion is reduced for users who ask for it.
   - Never stuck: everything resets when the request ends, when the page
     comes back from the browser cache, or after 25 s at the latest.

   Opt out on any form or button with data-no-busy.
   ============================================================ */
(function () {
    if (window.__alistoBusy) return;
    window.__alistoBusy = true;

    var OVERLAY_DELAY = 600;   // ms before the full-screen overlay shows
    var SAFETY_RESET = 25000;  // ms: never leave the page blocked longer
    var pending = 0;
    var overlayTimer = null;
    var safetyTimer = null;
    var lastClicked = null;
    var lastClickedAt = 0;

    // ---------- styles ----------
    var css = [
        '#busyBar{position:fixed;top:0;left:0;height:3px;width:0;z-index:2147483646;',
        'background:linear-gradient(90deg,#5088FE,#7aa6ff);box-shadow:0 0 6px rgba(80,136,254,.6);',
        'transition:width .4s ease,opacity .3s ease;opacity:0;pointer-events:none}',
        '#busyBar.on{opacity:1;width:80%;transition:width 8s cubic-bezier(.1,.6,.2,1),opacity .2s}',
        '#busyBar.done{opacity:0;width:100%;transition:width .2s,opacity .4s .2s}',
        '#busyOverlay{position:fixed;inset:0;z-index:2147483645;display:flex;align-items:center;',
        'justify-content:center;background:rgba(15,23,42,.35);opacity:0;visibility:hidden;',
        'transition:opacity .2s;padding:16px}',
        '#busyOverlay.on{opacity:1;visibility:visible}',
        '#busyOverlay .busy-card{background:#fff;border-radius:16px;padding:22px 26px;',
        'display:flex;align-items:center;gap:14px;box-shadow:0 12px 40px rgba(0,0,0,.18);',
        'font:600 15px Inter,system-ui,sans-serif;color:#1e293b;max-width:340px}',
        '.busy-spin{width:26px;height:26px;flex:0 0 26px;border-radius:50%;',
        'border:3px solid rgba(80,136,254,.25);border-top-color:#5088FE;animation:busyspin .8s linear infinite}',
        '.busy-btn{position:relative;pointer-events:none;opacity:.75;cursor:progress}',
        '.busy-btn .busy-inline{display:inline-block;width:14px;height:14px;margin-right:8px;',
        'vertical-align:-2px;border-radius:50%;border:2px solid currentColor;',
        'border-right-color:transparent;animation:busyspin .7s linear infinite}',
        '@keyframes busyspin{to{transform:rotate(360deg)}}',
        '@media (prefers-reduced-motion:reduce){.busy-spin,.busy-btn .busy-inline{animation-duration:2s}',
        '#busyBar.on{transition:none;width:100%}}'
    ].join('');
    var style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    var bar, overlayEl, overlayText;
    function ensureDom() {
        if (bar) return;
        bar = document.createElement('div');
        bar.id = 'busyBar';
        bar.setAttribute('aria-hidden', 'true');
        overlayEl = document.createElement('div');
        overlayEl.id = 'busyOverlay';
        overlayEl.innerHTML = '<div class="busy-card" role="status" aria-live="assertive">' +
            '<span class="busy-spin" aria-hidden="true"></span><span class="busy-text">Please wait...</span></div>';
        overlayText = overlayEl.querySelector('.busy-text');
        document.body.appendChild(bar);
        document.body.appendChild(overlayEl);
    }

    // ---------- show / hide ----------
    // overlay=false: a button already shows the progress, so only the thin
    // top bar runs (never two spinners at once).
    function start(message, overlay) {
        ensureDom();
        pending++;
        bar.classList.remove('done');
        // restart the bar animation
        void bar.offsetWidth;
        bar.classList.add('on');
        overlayText.textContent = message || 'Please wait...';
        clearTimeout(overlayTimer);
        if (overlay !== false) {
            overlayTimer = setTimeout(function () { overlayEl.classList.add('on'); }, OVERLAY_DELAY);
        }
        clearTimeout(safetyTimer);
        safetyTimer = setTimeout(resetAll, SAFETY_RESET);
    }

    function stop() {
        pending = Math.max(0, pending - 1);
        if (pending > 0) return;
        clearTimeout(overlayTimer);
        clearTimeout(safetyTimer);
        if (!bar) return;
        bar.classList.remove('on');
        bar.classList.add('done');
        overlayEl.classList.remove('on');
    }

    // Does the page already show its own progress on this button
    // (e.g. login's "Signing In..." spinner, register's "Submitting...")?
    function hasOwnState(btn) {
        if (!btn || btn.classList.contains('busy-btn')) return false;
        return btn.disabled || btn.classList.contains('is-loading') ||
            btn.getAttribute('aria-busy') === 'true' ||
            !!btn.querySelector('.spinner, .loading, [class*="spin"]') ||
            /\.\.\.|…/.test(btn.textContent || btn.value || '');
    }

    function markButton(btn, label) {
        if (!btn || btn.classList.contains('busy-btn')) return;
        btn.classList.add('busy-btn');
        btn.setAttribute('aria-busy', 'true');
        btn.setAttribute('aria-disabled', 'true');
        if (btn.tagName === 'BUTTON' && !btn.querySelector('.busy-inline')) {
            btn.dataset.busyHtml = btn.innerHTML;
            // Keep icon-only buttons icon-sized; label the rest.
            var text = (btn.textContent || '').trim();
            btn.innerHTML = '<span class="busy-inline" aria-hidden="true"></span>' +
                (text ? (label || 'Please wait...') : '');
        }
    }

    function unmarkButton(btn) {
        if (!btn || !btn.classList.contains('busy-btn')) return;
        btn.classList.remove('busy-btn');
        btn.removeAttribute('aria-busy');
        btn.removeAttribute('aria-disabled');
        if (btn.dataset.busyHtml !== undefined) {
            btn.innerHTML = btn.dataset.busyHtml;
            delete btn.dataset.busyHtml;
        }
    }

    function resetAll() {
        pending = 0;
        stop();
        document.querySelectorAll('.busy-btn').forEach(unmarkButton);
        document.querySelectorAll('form[data-busy-sent]').forEach(function (f) {
            f.removeAttribute('data-busy-sent');
        });
    }
    window.AlistoBusy = { start: start, stop: stop, reset: resetAll };

    // ---------- which forms count ----------
    function skipForm(form) {
        if (!form || form.hasAttribute('data-no-busy')) return true;
        if (form.target && form.target !== '_self') return true;
        // Downloads (CSV/PDF/backup exports) never leave the page, so an
        // overlay would just sit there.
        var action = (form.getAttribute('action') || '').toLowerCase();
        return /export|download|\.csv|\.pdf|\.xlsx|\.docx|backup/.test(action);
    }

    function messageFor(form) {
        var m = (form.getAttribute('method') || 'get').toLowerCase();
        return m === 'get' ? 'Loading...' : 'Please wait...';
    }

    // Remember what was clicked, and swallow repeat clicks on busy buttons.
    document.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('button, input[type=submit], a.btn, .btn-submit');
        if (!btn) return;
        if (btn.classList.contains('busy-btn')) {
            e.preventDefault();
            e.stopImmediatePropagation();
            return;
        }
        lastClicked = btn;
        lastClickedAt = Date.now();
    }, true);

    // Native form submits. Runs after the page's own handlers (bubble phase,
    // next tick) so forms handled with fetch() aren't double-counted.
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (skipForm(form)) return;
        if (form.hasAttribute('data-busy-sent')) {   // already on its way
            e.preventDefault();
            return;
        }
        var submitter = e.submitter || (lastClicked && form.contains(lastClicked) ? lastClicked : null);
        setTimeout(function () {
            if (e.defaultPrevented) return;          // page JS took over (fetch)
            form.setAttribute('data-busy-sent', '1');
            if (submitter && hasOwnState(submitter)) {
                start(null, false);                  // page shows its own spinner
            } else if (submitter) {
                // Not `disabled`: that would drop the button's name/value
                // (e.g. action=approve) from what's sent.
                markButton(submitter, (form.getAttribute('method') || 'get').toLowerCase() === 'get' ? 'Loading...' : 'Please wait...');
                start(null, false);
            } else {
                start(messageFor(form));             // no button to show it on
            }
        }, 0);
    });

    // form.submit() from JS (e.g. after a confirm dialog) skips the submit event.
    var nativeSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () {
        if (!skipForm(this)) {
            if (this.hasAttribute('data-busy-sent')) return;
            this.setAttribute('data-busy-sent', '1');
            start(messageFor(this));
        }
        return nativeSubmit.apply(this, arguments);
    };

    // fetch(): POST/PUT/PATCH/DELETE are user actions -> show progress, and
    // lock the button that started them. GETs (polling, lookups) stay silent.
    if (window.fetch) {
        var nativeFetch = window.fetch;
        window.fetch = function (input, init) {
            var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
            var silent = init && init.headers && (
                (init.headers['X-No-Busy']) ||
                (typeof init.headers.get === 'function' && init.headers.get('X-No-Busy')));
            if (method === 'GET' || method === 'HEAD' || silent) {
                return nativeFetch.apply(this, arguments);
            }
            var btn = (Date.now() - lastClickedAt < 400) ? lastClicked : null;
            // No click just before it = background work (polling, marking a
            // notification read). Don't block the page for that.
            if (!btn || btn.hasAttribute('data-no-busy')) {
                return nativeFetch.apply(this, arguments);
            }
            // A busy button's page handler may already show its own text
            // ("Submitting...") -- only add ours if it didn't.
            var ownState = hasOwnState(btn);
            if (!ownState) markButton(btn);
            start(null, false);                      // the button is the indicator
            return nativeFetch.apply(this, arguments).finally(function () {
                if (!ownState) unmarkButton(btn);
                stop();
            });
        };
    }

    // Back/forward cache: a page restored after navigating away must not
    // come back still "busy".
    window.addEventListener('pageshow', function (e) { if (e.persisted) resetAll(); });
})();
