/* Admin-only, explicitly approved Modrinth updates; no automatic browser checks. */
(function () {
    'use strict';
    const byId = id => document.getElementById(id);
    const root = byId('addon-update-settings');
    if (!root) return;
    const enable = byId('addon-updates-enabled');
    const rows = byId('addon-updates-rows');
    const notice = byId('addon-updates-message');
    const save = byId('addon-updates-save');
    const check = byId('addon-updates-check');
    const apply = byId('addon-updates-apply');
    let saved = null, projects = new Set(), pending = false, timer = null, pageActive = true;
    const active = () => saved && (saved.busy || ['checking', 'applying'].includes(saved.status));
    const dirty = () => saved && (enable.checked !== saved.enabled ||
        JSON.stringify(Array.from(projects).sort()) !== JSON.stringify(saved.projects.slice().sort()));
    function message(text, error) {
        notice.hidden = false;
        notice.textContent = text;
        notice.classList.toggle('error', !!error);
        notice.setAttribute('role', error ? 'alert' : 'status');
    }
    function controls() {
        const blocked = pending || !saved || active();
        enable.disabled = blocked;
        rows.querySelectorAll('input').forEach(input => { input.disabled = blocked; });
        save.disabled = blocked || !dirty();
        check.disabled = blocked || !!dirty();
        apply.disabled = blocked || !!dirty() || !saved.enabled || !saved.projects.length;
        byId('addon-updates-unsaved').hidden = !dirty();
    }
    function checkbox(project, label) {
        const wrapper = document.createElement('label');
        wrapper.className = 'addon-update-approval';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = projects.has(project);
        input.dataset.project = project;
        input.setAttribute('aria-label', 'Automatically update ' + label);
        input.addEventListener('change', () => {
            if (input.checked) projects.add(project); else projects.delete(project);
            rows.querySelectorAll('input').forEach(other => {
                if (other.dataset.project === project) other.checked = input.checked;
            });
            controls();
        });
        wrapper.append(input, document.createTextNode(' Approve automatic updates'));
        return wrapper;
    }
    function renderBackupCleanup(cleanup) {
        const status = byId('addon-updates-backup-cleanup');
        const warnings = byId('addon-updates-backup-warnings');
        // Older API responses may not contain cleanup metadata yet.
        if (!status || !warnings) return;
        status.hidden = !cleanup;
        warnings.hidden = true;
        status.textContent = '';
        warnings.replaceChildren();
        if (!cleanup) return;
        const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
        const date = new Date(cleanup.last_run);
        const lastRun = Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
        status.textContent = cleanup.last_run
            ? 'Rollback backup cleanup: ' + lastRun + ' \u00b7 ' + count(cleanup.removed) +
                ' removed, ' + count(cleanup.retained) + ' retained, ' + count(cleanup.protected) +
                ' protected for manual review.'
            : 'Rollback backup cleanup has not run yet.';
        const notices = Array.isArray(cleanup.warnings)
            ? cleanup.warnings.filter(warning => typeof warning === 'string' && warning) : [];
        notices.forEach(warning => {
            const paragraph = document.createElement('p');
            paragraph.textContent = warning;
            warnings.appendChild(paragraph);
        });
        warnings.hidden = !notices.length;
    }
    function render(data) {
        saved = data;
        enable.checked = data.enabled;
        projects = new Set(data.projects);
        rows.replaceChildren();
        const present = new Set();
        data.addons.forEach(addon => {
            const item = document.createElement('div');
            item.className = 'addon-update-item';
            const title = document.createElement('strong');
            title.textContent = addon.filename;
            item.appendChild(title);
            const version = document.createElement('p');
            version.className = 'field-help';
            version.textContent = 'Installed: ' + (addon.installed_version || 'Unidentified') +
                (addon.available_version ? ' \u2192 Available: ' + addon.available_version : '') +
                (addon.project_id ? ' \u00b7 Modrinth project ' + addon.project_id : '');
            const reason = document.createElement('p');
            reason.className = 'field-help';
            reason.textContent = addon.reason || addon.status;
            item.append(version, reason);
            if (addon.project_id) {
                present.add(addon.project_id);
                item.appendChild(checkbox(addon.project_id, addon.filename));
            }
            rows.appendChild(item);
        });
        data.projects.filter(project => !present.has(project)).forEach(project => {
            const item = document.createElement('div');
            item.className = 'addon-update-item';
            const description = document.createElement('p');
            description.textContent = 'Previously approved Modrinth project: ' + project +
                '. Check installed add-ons to refresh its identity and status.';
            item.append(description, checkbox(project, project));
            rows.appendChild(item);
        });
        if (!data.addons.length && !data.projects.length) {
            const empty = document.createElement('p');
            empty.className = 'field-help';
            empty.textContent = 'Use Check updates to identify installed JARs. Unidentified files are never automatically updated.';
            rows.appendChild(empty);
        }
        const date = value => value ? new Date(value).toLocaleString() : 'Never';
        byId('addon-updates-checked').textContent = 'Last check: ' + date(data.last_checked) +
            ' \u00b7 Last installation: ' + date(data.last_applied);
        renderBackupCleanup(data.backup_cleanup);
        message(data.message, data.status === 'error');
        controls();
        clearTimeout(timer);
        if (pageActive && active()) timer = setTimeout(refresh, 1500);
    }
    async function api(url, options) {
        const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
        if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) {
            throw new Error('Unexpected panel response. Refresh and sign in again if needed.');
        }
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || data.message || 'Update request failed.');
        return data;
    }
    async function refresh() {
        if (!pageActive || pending || dirty()) return;
        try { render(await api('/api/addon-updates')); }
        catch (error) {
            message(error.message, true);
            if (pageActive && active()) timer = setTimeout(refresh, 5000);
        }
    }
    async function action(path, options) {
        if (!pageActive || pending) return;
        pending = true;
        clearTimeout(timer);
        controls();
        try { render(await api(path, options)); }
        catch (error) {
            message(error.message, true);
            if (pageActive && active()) timer = setTimeout(refresh, 1500);
        }
        finally { pending = false; controls(); }
    }
    enable.addEventListener('change', controls);
    save.addEventListener('click', () => action('/api/addon-updates', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enable.checked, projects: Array.from(projects)})
    }));
    check.addEventListener('click', () => action('/api/addon-updates/check', {method: 'POST'}));
    apply.addEventListener('click', () => action('/api/addon-updates/apply', {method: 'POST'}));
    window.addEventListener('beforeunload', event => {
        if (dirty()) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('pagehide', () => { pageActive = false; clearTimeout(timer); });
    window.addEventListener('pageshow', event => {
        pageActive = true;
        if (event.persisted) refresh();
    });
    controls();
    refresh();
}());
