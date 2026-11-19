/* Shared navigation and real server-state indicator. No simulated state transitions. */
(function () {
    'use strict';
    function stateFor(status) {
        if (status === 'Online') return 'online';
        if (['Starting', 'Restarting', 'Shutting Down', 'Stopping'].includes(status)) return 'transition';
        if (status === 'Offline') return 'offline';
        return 'unknown';
    }
    function renderStatus(status) {
        const state = stateFor(status);
        document.querySelectorAll('[data-server-status]').forEach(el => {
            el.dataset.state = state;
            const label = el.querySelector('[data-status-label]');
            if (label) label.textContent = status || 'Status unavailable';
        });
        document.querySelectorAll('[data-server-dot]').forEach(el => {
            el.dataset.state = state;
            el.setAttribute('aria-label', 'Server ' + (status || 'status unavailable').toLowerCase());
        });
    }
    function init() {
        const sidebar = document.getElementById('panel-sidebar');
        if (!sidebar) return; // Login and first-run pages never mount the authenticated shell.
        const toggle = document.getElementById('menu-toggle');
        const close = document.getElementById('sidebar-close');
        const overlay = document.getElementById('sidebar-overlay');
        const mobile = window.matchMedia('(max-width: 760px)');
        function setOpen(open) {
            document.body.classList.toggle('nav-open', open);
            if (toggle) toggle.setAttribute('aria-expanded', String(open));
            if (overlay) overlay.hidden = !open;
            sidebar.inert = mobile.matches && !open;
            if (open && close) close.focus();
        }
        if (toggle) toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
        if (close) close.addEventListener('click', () => { setOpen(false); if (toggle) toggle.focus(); });
        if (overlay) overlay.addEventListener('click', () => { setOpen(false); if (toggle) toggle.focus(); });
        sidebar.querySelector('[data-byte-guard-link]')?.addEventListener('click', () => setOpen(false));
        document.addEventListener('keydown', event => {
            if (!document.body.classList.contains('nav-open')) return;
            if (event.key === 'Escape') { setOpen(false); if (toggle) toggle.focus(); }
            if (event.key === 'Tab') {
                const focusable = Array.from(sidebar.querySelectorAll('a[href], button:not([disabled])')).filter(el => el.getClientRects().length);
                const first = focusable[0], last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
        });
        mobile.addEventListener('change', () => setOpen(false));
        setOpen(false);
        if (document.body.dataset.restricted === 'true') { renderStatus('Status unavailable'); return; }
        let stopped = false, timer;
        async function poll() {
            if (stopped) return;
            try {
                const response = await fetch('/api/stats', {cache:'no-store'});
                if (!response.ok) throw new Error('Status unavailable');
                const data = await response.json();
                renderStatus(data.status);
                document.dispatchEvent(new CustomEvent('panel:stats', {detail:data}));
            } catch (_) {
                renderStatus('Status unavailable');
                document.dispatchEvent(new CustomEvent('panel:stats-unavailable'));
            }
            if (!stopped) timer = setTimeout(poll, 4000);
        }
        window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); });
        window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; poll(); } });
        poll();
    }
    window.BytePanelShell = {stateFor:stateFor, renderStatus:renderStatus};
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

