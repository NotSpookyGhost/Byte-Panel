/* Private panel connection-security UI. All server-supplied text stays text. */
(function () {
    'use strict';
    const el = id => document.getElementById(id);
    const settings = el('settings-security');
    const players = el('cg-player-security');
    if (!settings && !players) return;
    const can = permission => typeof window.panelCan === 'function' && window.panelCan(permission);
    const BASE = '/api/connection-security';
    let pageActive = true, pending = false, timer = null;
    let saved = null, auth = null, preview = null, nextCursor = null, currentCursor = '';
    let filter = {identity: '', status: ''};
    let settingsDirty = false;
    const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    const date = value => {
        if (!value) return 'Never';
        const parsed = new Date(typeof value === 'number' && value < 1000000000000 ? value * 1000 : value);
        return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
    };
    const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
    const locationText = location => {
        const value = location && typeof location === 'object' ? location : {};
        return [text(value.city), text(value.country_name) || text(value.country)].filter(Boolean).join(', ') ||
            'Location unknown';
    };
    const label = value => text(value).replace(/_/g, ' ');
    const visible = () => pageActive && !document.hidden && (!settings || !settings.hidden);
    function node(tag, content, className) {
        const result = document.createElement(tag);
        if (content !== undefined) result.textContent = text(content);
        if (className) result.className = className;
        return result;
    }
    function message(content, error = false) {
        const target = el(settings ? 'cg-settings-message' : 'cg-events-message');
        target.hidden = false;
        target.textContent = text(content) || (error ? 'The request could not be completed. Refresh and try again.' : 'Ready.');
        target.classList.toggle('error', error);
        target.setAttribute('role', error ? 'alert' : 'status');
    }
    async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
        // panel_access.js injects the same-origin CSRF header for every mutation.
        const options = {method, credentials: 'same-origin', cache: 'no-store'};
        if (body !== undefined) {
            options.headers = {'Content-Type': 'application/json'};
            options.body = JSON.stringify(body);
        }
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        const timeout = controller ? setTimeout(() => controller.abort(),
            path === '/geoip/update' || (path === '/settings' && method === 'PUT') ||
                /^\/auth\/(install|repair)$/.test(path) ? 180000 : 20000) : null;
        if (controller) options.signal = controller.signal;
        try {
            const response = await fetch(path.startsWith('/api/') ? path : BASE + path, options);
            if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) {
                throw new Error('Unexpected panel response. Refresh and sign in again if needed.');
            }
            let data;
            try { data = await response.json(); }
            catch (error) {
                if (error.name === 'AbortError') throw error;
                throw new Error('The panel returned an unreadable response. Refresh and try again.');
            }
            if (!data || typeof data !== 'object' || Array.isArray(data)) {
                throw new Error('The panel returned an invalid response. Refresh and try again.');
            }
            if (!response.ok || data.status === 'error') {
                throw new Error(text(data.error) || text(data.message) || 'Byte Guard request failed (HTTP ' + response.status + ').');
            }
            return data;
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('The request timed out. Refresh health before retrying; the server may still be completing the operation.');
            throw error;
        }
        finally { if (timeout !== null) clearTimeout(timeout); }
    }
    function list(target, lines, warnings = []) {
        target.replaceChildren();
        lines.filter(Boolean).forEach(line => target.append(node('p', line)));
        if (Array.isArray(warnings)) warnings.filter(value => typeof value === 'string' && value)
            .forEach(warning => target.append(node('p', warning, 'cg-warning')));
    }
    function authRender(data) {
        auth = data && typeof data === 'object' ? data : {};
        list(el('cg-auth-status'), [
            'Authentication: ' + (text(auth.status) || 'Not checked'),
            text(auth.message),
            auth.installed ? 'Installed provider: ' + (text(auth.name) || 'AuthMe') +
                (auth.version ? ' ' + text(auth.version) : '') : '',
            auth.compatible === false ? 'A compatible, ready authentication provider is required for offline-mode protection.' : ''
        ], auth.issues || auth.warnings);
    }
    function settingsRender(data, hydrate = true) {
        saved = data;
        if (hydrate) {
            el('cg-mode').value = ['off', 'monitor', 'enforce'].includes(data.mode) ? data.mode : 'off';
            el('cg-radius').value = Number.isFinite(data.radius_km) ? String(data.radius_km) : '250';
            el('cg-account-id').value = text(data.account_id);
            el('cg-license-key').value = '';
            el('cg-remove-key').checked = false;
            settingsDirty = !!data.effective_identity_mode && data.effective_identity_mode !== data.identity_mode;
        }
        const hasKey = data.license_key_set === true || data.has_license_key === true;
        el('cg-license-help').textContent = hasKey
            ? 'A license key is saved. Leave blank to keep it. The saved key is never returned to this page.'
            : 'No license key is saved. Enter your own MaxMind license key before downloading the City database.';
        el('cg-identity-mode').textContent = 'Server identity mode: ' + (text(data.identity_mode) || 'Unknown') +
            (data.effective_identity_mode && data.effective_identity_mode !== data.identity_mode
                ? ' (server.properties now requests ' + text(data.effective_identity_mode) + ')' : '') +
            '. Authentication mode changes require a new protection setup; identities are never silently converted.';
        const health = data.health || data.bridge || {};
        list(el('cg-health'), [
            'Mode: ' + text(data.mode || 'off'),
            'Integration: ' + (text(health.status) || text(health.state) ||
                (health.healthy === true ? 'Connected and current' : 'Not ready / not connected')),
            text(health.message),
            (health.last_seen || health.received_at) ? 'Last plugin heartbeat: ' + date(health.last_seen || health.received_at) : '',
            health.pending_revision ? 'The plugin has not acknowledged the latest policy yet.' : ''
        ], health.issues || health.warnings);
        const geo = data.geoip || data.geoiphealth || {};
        list(el('cg-geoip'), [
            'Database: ' + (text(geo.status) || (geo.ready ? 'Ready' : geo.available ? 'Available; check health' : 'Not available')),
            text(geo.message),
            (geo.build_time || geo.build_epoch || geo.updated_at || geo.last_updated) ?
                'Database date: ' + date(geo.build_time || geo.build_epoch || geo.updated_at || geo.last_updated) : '',
            Number.isFinite(geo.age_days) ? 'Database age: ' + geo.age_days + ' days' : '',
            (geo.last_checked || geo.last_check) ? 'Last update check: ' + date(geo.last_checked || geo.last_check) : '',
            geo.stale ? 'Database is stale. Unfamiliar returning-player IPs cannot be approved from it.' : ''
        ], geo.issues || geo.warnings || (geo.error ? [geo.error] : []));
        if (data.auth) authRender(data.auth);
        controls();
    }
    function changed() {
        if (!saved) return false;
        return el('cg-mode').value !== saved.mode ||
            (!!saved.effective_identity_mode && saved.effective_identity_mode !== saved.identity_mode) ||
            Number(el('cg-radius').value) !== saved.radius_km ||
            el('cg-account-id').value.trim() !== text(saved.account_id) ||
            !!el('cg-license-key').value || el('cg-remove-key').checked;
    }
    function controls() {
        if (settings) {
            const authorized = can('settings.manage');
            const blocked = pending || !authorized;
            el('cg-settings-fields').disabled = blocked;
            ['cg-mode', 'cg-radius', 'cg-account-id', 'cg-license-key', 'cg-remove-key'].forEach(id => {
                el(id).disabled = blocked || !saved;
            });
            el('cg-unsaved').hidden = !settingsDirty;
            el('cg-save').disabled = blocked || !saved || !settingsDirty;
            el('cg-refresh-settings').disabled = blocked;
            el('cg-update-db').disabled = blocked || !saved || settingsDirty;
            el('cg-auth-check').disabled = blocked;
            el('cg-auth-install').disabled = blocked || settingsDirty || !auth || auth.can_install !== true;
            el('cg-auth-repair').disabled = blocked || settingsDirty || !auth || auth.can_repair !== true;
            el('cg-history-preview').disabled = blocked;
            el('cg-history-import').disabled = blocked || !preview || !Array.isArray(preview.candidates) ||
                !preview.candidates.length || preview.imported === true || preview.already_imported === true;
        }
        if (players) {
            ['cg-filter-submit', 'cg-events-refresh'].forEach(id => { el(id).disabled = pending; });
            el('cg-events-next').disabled = pending || !nextCursor;
            el('cg-event-list').querySelectorAll('button').forEach(button => { button.disabled = pending; });
        }
    }
    function schedule() {
        clearTimeout(timer);
        timer = null;
        if (visible() && !pending && !settingsDirty && (!players || !currentCursor)) {
            timer = setTimeout(() => refresh(), 15000);
        }
    }
    async function work(action) {
        if (!pageActive || pending) return;
        pending = true;
        clearTimeout(timer);
        controls();
        try { await action(); return true; }
        catch (error) { if (pageActive) message(text(error.message), true); return false; }
        finally { pending = false; controls(); schedule(); }
    }
    async function readSettings(hydrate) {
        const data = await api('/settings');
        if (!pageActive) return;
        settingsRender(data, hydrate);
        if (!data.auth && !auth) {
            const result = await api('/auth');
            if (pageActive) authRender(result);
        }
    }
    async function refresh() {
        if (!visible() || pending || settingsDirty) { schedule(); return; }
        await work(async () => {
            if (settings) {
                await readSettings(true);
                if (pageActive) message('Security status refreshed. Changes take effect only after saving.');
            } else await readEvents('');
        });
    }
    function previewRender(data) {
        preview = data;
        const candidates = Array.isArray(data.candidates) ? data.candidates : [];
        const alreadyImported = data.imported === true || data.already_imported === true;
        const target = el('cg-history-candidates');
        target.replaceChildren();
        candidates.slice(0, 500).forEach(candidate => {
            target.append(node('p', (text(candidate.username) || text(candidate.identity) || 'Unknown identity') +
                ' — ' + (text(candidate.ip) || 'Unknown IP') +
                ' — ' + locationText(candidate.location || candidate), 'cg-history-item'));
        });
        if (Array.isArray(data.warnings)) data.warnings.filter(value => typeof value === 'string')
            .forEach(warning => target.append(node('p', warning, 'cg-notice')));
        el('cg-history-summary').textContent = alreadyImported ? 'History has already been confirmed for this server scope.' :
            candidates.length + ' candidate references; ' + count(data.skipped) + ' records skipped.' +
            (candidates.length > 500 ? ' Showing the first 500 candidates.' : '');
    }
    function actionButton(label, action, permission, className = 'btn btn-outline') {
        if (permission && !can(permission)) return null;
        const button = node('button', label, className);
        button.type = 'button';
        if (permission) button.dataset.permission = permission;
        button.addEventListener('click', action);
        return button;
    }
    function isPending(event) { return event.review_status === 'pending' || event.pending_review === true; }
    function eventRender(event) {
        const item = node('article', undefined, 'cg-event');
        const heading = node('div', undefined, 'cg-heading');
        heading.append(node('strong', text(event.username) || text(event.identity) || 'Unknown player'));
        const badge = node('span', isPending(event) ? 'Pending review' : label(event.action) || 'Observed', 'cg-badge');
        badge.classList.toggle('pending', isPending(event));
        badge.classList.toggle('blocked', ['deny', 'reject', 'rejected', 'blocked'].includes(event.action));
        heading.append(badge);
        const location = locationText(event.location || event);
        item.append(heading, node('p', (text(event.ip) || 'Unknown IP') + ' — ' + location),
            node('p', date(event.created_at || event.timestamp || event.received_at) + ' · ' +
                (label(event.stage) || 'Connection attempt'), 'cg-event-meta'),
            node('p', label(event.reason) || 'No additional decision details.'),
            node('p', 'Joined: ' + (event.joined === true ? 'Yes' : 'Not confirmed') +
                ' · Authentication: ' + (event.authenticated === true ? 'Confirmed' : 'Not confirmed') +
                (event.disconnected === true ? ' · Disconnected' : ''), 'cg-event-meta'),
            node('p', 'Identity: ' + (text(event.identity) || 'Unknown') +
                ' · Reference: ' + (text(event.attempt_id) || 'Unavailable'), 'cg-event-meta'));
        const actions = node('div', undefined, 'cg-actions');
        if (event.identity) {
            actions.append(actionButton('Player details', () => showProfile(event.identity), 'players.view'));
            actions.append(actionButton('This player’s history', () => {
                if (pending) return;
                el('cg-filter-identity').value = text(event.identity);
                el('cg-filter-status').value = '';
                applyFilter();
            }, 'players.view'));
        }
        if (isPending(event) && event.attempt_id) {
            [
                ['Allow next join', 'allow_once', 'Allow one successful join for this identity and IP within 15 minutes? This does not permanently trust the area.'],
                ['Trust this area', 'trust_area', 'Trust this geographic area for this player? This expands their approved locations. GeoIP is approximate; verify the player independently first.'],
                ['Keep blocked', 'keep_blocked', 'Resolve this review without granting access? This does not ban the source IP.']
            ].forEach(([label, action, confirmation]) => {
                const button = actionButton(label, () => review(event, action, confirmation), 'players.security');
                if (button) actions.append(button);
            });
        }
        if (event.ip && event.username) {
            const ban = actionButton('Ban source IP', () => banIP(event), 'players.manage', 'btn btn-stop');
            if (ban) actions.append(ban);
        }
        item.append(actions);
        return item;
    }
    async function readEvents(cursor) {
        const params = new URLSearchParams({limit: '50'});
        if (cursor) params.set('cursor', cursor);
        if (filter.identity) params.set('identity', filter.identity);
        if (filter.status) params.set('status', filter.status);
        const data = await api('/events?' + params.toString());
        if (!pageActive) return;
        const events = Array.isArray(data.events) ? data.events : [];
        el('cg-event-list').replaceChildren(...events.map(eventRender));
        if (!events.length) el('cg-event-list').append(node('p', 'No connection records match this filter.', 'field-help'));
        currentCursor = cursor;
        nextCursor = typeof data.next_cursor === 'string' || typeof data.next_cursor === 'number' ? data.next_cursor : null;
        const totalPending = Number.isSafeInteger(data.pending_reviews) ? data.pending_reviews : events.filter(isPending).length;
        el('cg-review-count').textContent = totalPending + (data.pending_reviews === undefined ? ' pending on this page' : ' pending reviews');
        el('cg-review-count').classList.toggle('pending', totalPending > 0);
        message(events.length + ' connection records shown.' + (cursor ? ' Viewing older connections; automatic refresh is paused.' : ' Latest records refresh every 15 seconds while this page is visible.'));
    }
    async function review(event, action, confirmation) {
        if (pending || !can('players.security') || !window.confirm(confirmation)) return;
        await work(async () => {
            await api('/reviews/' + encodeURIComponent(event.attempt_id), {action});
            await readEvents(currentCursor);
            if (pageActive) message('Review saved. The player must reconnect; this does not authenticate their account.');
        });
    }
    async function banIP(event) {
        if (pending || !can('players.manage')) return;
        if (!window.confirm('Ban source IP ' + text(event.ip) + '? Shared networks, VPNs and households may use this IP, so other players can be blocked too. This is separate from the connection review.')) return;
        await work(async () => {
            await api('/api/player/ban', {username: text(event.username), ip: text(event.ip),
                ban_type: 'ip', reason: 'Byte Guard review: ' + text(event.attempt_id)});
            if (pageActive) message('Source IP ban applied. Other players sharing that IP can also be affected.');
        });
    }
    async function showProfile(identity) {
        if (pending || !can('players.view')) return;
        await work(async () => {
            const profile = await api('/players/' + encodeURIComponent(identity));
            if (!pageActive) return;
            el('cg-profile-title').textContent = 'Protection details — ' + (text(profile.username) || text(identity));
            const body = el('cg-profile-body');
            body.replaceChildren(node('p', 'Identity: ' + text(identity)),
                node('p', 'Enrollment source: ' + (label(profile.source) || 'Unknown')),
                node('p', 'Original IP: ' + (text(profile.original_ip) || 'Not recorded')),
                node('p', 'First seen: ' + date(profile.first_seen) +
                    (profile.last_seen ? ' · Last seen: ' + date(profile.last_seen) : '')),
                node('p', 'Authenticated enrollment: ' + (profile.authenticated === true ? 'Confirmed' : 'Not recorded')));
            body.append(node('h4', 'Approved geographic references'));
            const references = Array.isArray(profile.references) ? profile.references : [];
            if (!references.length) body.append(node('p', 'No usable geographic reference is recorded. An accepted exact IP may still be trusted.'));
            references.slice(0, 100).forEach((reference, index) => {
                const location = reference.location || reference;
                body.append(node('p', (index === 0 ? 'Original reference: ' : 'Approved reference: ') +
                    locationText(location) +
                    (Number.isFinite(location.accuracy_km) ? ' · GeoIP uncertainty: ' + location.accuracy_km + ' km' : '') +
                    (location.usable === false ? ' · Not usable for new-IP approval' : '')));
            });
            if (references.length > 100) body.append(node('p', 'Showing the first 100 references.'));
            body.append(node('h4', 'Accepted IP addresses'));
            const ips = Array.isArray(profile.accepted_ips) ? profile.accepted_ips.filter(ip => typeof ip === 'string') : [];
            body.append(node('p', ips.length ? ips.slice(0, 200).join(', ') : 'No accepted IPs recorded.'));
            if (ips.length > 200) body.append(node('p', 'Showing the first 200 of ' + ips.length + ' accepted IPs.'));
            el('cg-profile').hidden = false;
        });
    }
    function applyFilter() {
        if (pending) return;
        filter = {identity: el('cg-filter-identity').value.trim(), status: el('cg-filter-status').value};
        work(() => readEvents(''));
    }
    if (settings) {
        ['cg-mode', 'cg-radius', 'cg-account-id', 'cg-license-key', 'cg-remove-key'].forEach(id => {
            ['input', 'change'].forEach(event => el(id).addEventListener(event, () => {
                settingsDirty = changed();
                controls();
                schedule();
            }));
        });
        el('cg-settings-form').addEventListener('submit', event => {
            event.preventDefault();
            if (pending || !saved || !settingsDirty || !can('settings.manage')) return;
            const radius = Number(el('cg-radius').value);
            if (!Number.isInteger(radius) || radius < 1 || radius > 20000) {
                message('Enter a whole-number area radius between 1 and 20,000 km.', true); return;
            }
            const body = {mode: el('cg-mode').value, radius_km: radius,
                account_id: el('cg-account-id').value.trim(),
                license_key: el('cg-license-key').value,
                clear_license_key: el('cg-remove-key').checked};
            if (body.clear_license_key && body.license_key) {
                message('Choose either a new license key or removal of the saved key, not both.', true); return;
            }
            if (body.clear_license_key && !window.confirm('Remove the saved MaxMind license key? Database downloads will stop until new credentials are saved.')) return;
            if (saved.effective_identity_mode && saved.effective_identity_mode !== saved.identity_mode) {
                if (!window.confirm('Reconfigure Byte Guard for ' + saved.effective_identity_mode +
                    ' identities? Existing identities will not be converted. Pending approvals will be revoked. Offline mode still requires AuthMe passwords.')) return;
                body.confirm_identity_change = true;
            }
            work(async () => {
                message('Saving and validating security settings. A new MaxMind key requires a database download.');
                const result = await api('/settings', body, 'PUT');
                if (!pageActive) return;
                el('cg-license-key').value = '';
                if (result.mode) settingsRender(result);
                else await readSettings(true);
                message('Security settings saved. Check integration health before accepting protection as active.');
            });
        });
        el('cg-refresh-settings').addEventListener('click', () => work(async () => {
            await readSettings(!settingsDirty);
            if (pageActive) message('Health refreshed.' + (settingsDirty ? ' Your unsaved edits have been preserved.' : ''));
        }));
        el('cg-update-db').addEventListener('click', () => {
            if (settingsDirty || !can('settings.manage')) return;
            work(async () => {
                message('Updating the local GeoLite2 City database. This may take a few minutes.');
                const result = await api('/geoip/update', {});
                await readSettings(true);
                if (pageActive) message(text(result.message) || 'Database update requested. Check database status for the result.');
            });
        });
        el('cg-auth-check').addEventListener('click', () => work(async () => {
            const result = await api('/auth');
            if (pageActive) { authRender(result); message('Authentication check complete.'); }
        }));
        ['install', 'repair'].forEach(action => el('cg-auth-' + action).addEventListener('click', () => {
            if (settingsDirty || !can('settings.manage') || pending) return;
            const prompt = action === 'install'
                ? 'Install the verified compatible AuthMe plugin and required dependencies? Minecraft must be stopped. Existing authentication providers or incompatible versions will block installation.'
                : 'Repair the panel-managed AuthMe installation while Minecraft is stopped? Existing accounts are preserved. Review any reported configuration conflicts; unrelated settings are not silently overwritten.';
            if (!window.confirm(prompt)) return;
            work(async () => {
                message(action === 'install' ? 'Preparing AuthMe. Keep Minecraft stopped.' : 'Repairing managed AuthMe. Keep Minecraft stopped.');
                const result = await api('/auth/' + action, {confirm: true});
                authRender(await api('/auth'));
                await readSettings(true);
                if (pageActive) message(text(result.message) || 'Authentication preparation finished. Check health before enabling the guard.');
            });
        }));
        el('cg-history-preview').addEventListener('click', () => work(async () => {
            const data = await api('/history/preview');
            if (pageActive) { previewRender(data); message('Review the history preview before confirming its server.'); }
        }));
        el('cg-history-import').addEventListener('click', () => {
            if (!can('settings.manage') || !preview || pending ||
                !window.confirm('Confirm that this player history belongs to the current server and import its candidate reference locations? This one-time action grants historical trust; skip it if the history is uncertain.')) return;
            work(async () => {
                const result = await api('/history/import', {confirm: true});
                if (pageActive) {
                    preview = {...preview, imported: true};
                    previewRender(preview);
                    message(text(result.message) || 'Player history confirmed for this server.');
                }
            });
        });
    }
    if (players) {
        el('cg-filter-form').addEventListener('submit', event => { event.preventDefault(); applyFilter(); });
        el('cg-events-refresh').addEventListener('click', () => work(() => readEvents('')));
        el('cg-events-next').addEventListener('click', () => {
            if (nextCursor) work(() => readEvents(nextCursor));
        });
        el('cg-profile-close').addEventListener('click', () => { el('cg-profile').hidden = true; });
    }
    window.addEventListener('beforeunload', event => {
        if (settingsDirty) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('pagehide', () => { pageActive = false; clearTimeout(timer); });
    window.addEventListener('pageshow', event => { pageActive = true; if (event.persisted) refresh(); });
    document.addEventListener('visibilitychange', () => {
        if (visible()) refresh(); else clearTimeout(timer);
    });
    if (settings) document.querySelectorAll('[data-settings-tab]').forEach(tab => {
        tab.addEventListener('click', () => { if (visible()) refresh(); else clearTimeout(timer); });
        tab.addEventListener('keydown', () => setTimeout(() => { if (visible()) refresh(); else clearTimeout(timer); }, 0));
    });
    controls();
    // Settings need one initial read even when another section is selected.
    if (settings && can('settings.manage')) work(() => readSettings(true)).then(success => {
        if (success && pageActive && saved) message('Security settings loaded. Configured mode: ' +
            text(saved.mode || 'off') + '. Check integration health to verify runtime protection.');
    });
    else if (players && can('players.view')) refresh();
}());
