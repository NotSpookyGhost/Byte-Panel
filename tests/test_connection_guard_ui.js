/* Isolated connection-guard UI tests; no real server, credentials or network. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createDocument} = require('./plugin_dom.js');
const source = fs.readFileSync(path.join(__dirname, '../app/static/connection_guard.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function state(extra = {}) {
    return {mode: 'off', radius_km: 250, account_id: '1234', license_key_set: true,
        identity_mode: 'online', geoip: {status: 'ready', age_days: 2, updated_at: '2026-11-18T12:00:00Z'},
        health: {status: 'disconnected', message: 'Start Minecraft to connect.'},
        auth: {status: 'missing', installed: false, can_install: true, can_repair: false}, ...extra};
}
function events(extra = {}) {
    return {events: [{attempt_id: 'attempt-1', identity: 'offline:Player', username: 'Player',
        ip: '203.0.113.42', created_at: '2026-11-18T12:00:00Z', country: 'Example',
        city: 'Example City', reason: 'Outside approved area', action: 'reject',
        stage: 'pre_login', review_status: 'pending'}], next_cursor: null, ...extra};
}
function reply(data, {status = 200, type = 'application/json', redirected = false, jsonError = false} = {}) {
    return {status, ok: status >= 200 && status < 300, redirected,
        headers: {get: () => type}, json: async () => {
            if (jsonError) throw new Error('JSON parse failed');
            return data;
        }};
}
async function setup({kind = 'settings', initial, permissions = ['settings.manage', 'players.view', 'players.security', 'players.manage'],
    visible = true, firstResponse, extraResponses = [], abortSupport = false} = {}) {
    const document = createDocument();
    const prototype = Object.getPrototypeOf(document.createElement('div'));
    if (!Object.getOwnPropertyDescriptor(prototype, 'innerHTML')) {
        Object.defineProperty(prototype, 'innerHTML', {set() { throw new Error('Unsafe HTML rendering'); }});
    }
    const getElementById = document.getElementById.bind(document);
    document.getElementById = id => {
        if (id === 'settings-security' && kind !== 'settings') return null;
        if (id === 'cg-player-security' && kind !== 'players') return null;
        return getElementById(id);
    };
    const listeners = {}, windowListeners = {};
    document.hidden = !visible;
    document.addEventListener = (name, fn) => { (listeners[name] ||= []).push(fn); };
    document.querySelectorAll = () => [];
    if (kind === 'settings') document.getElementById('settings-security').hidden = false;
    const calls = [], prompts = [], confirmations = [], queue = [];
    if (kind !== 'none' && permissions.length) queue.push(firstResponse || reply(initial || (kind === 'settings' ? state() : events())));
    queue.push(...extraResponses);
    let timerId = 0;
    const timers = new Map();
    const window = {
        panelCan: permission => permissions.includes(permission),
        confirm: prompt => { prompts.push(prompt); return confirmations.length ? confirmations.shift() : true; },
        addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); }
    };
    const fetch = async (url, options) => {
        calls.push({url, options});
        assert.ok(queue.length, 'Unexpected fetch: ' + url);
        const result = queue.shift();
        if (result instanceof Error) throw result;
        return typeof result === 'function' ? result({url, options}) : result;
    };
    vm.runInNewContext(source, {
        document, window, fetch, URLSearchParams, console, AbortController: abortSupport ? AbortController : undefined,
        setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
        clearTimeout: id => timers.delete(id)
    });
    await settle();
    const trigger = (handlers, name, args) => {
        const event = {preventDefault() { this.prevented = true; }, ...args};
        for (const fn of handlers[name] || []) fn(event);
        return event;
    };
    return {
        document, calls, queue, timers, prompts, confirmations,
        el: name => document.getElementById(name.startsWith('cg-') ? name : 'cg-' + name),
        windowEvent: (name, args = {}) => trigger(windowListeners, name, args),
        documentEvent: (name, args = {}) => trigger(listeners, name, args),
        async timer() {
            assert.equal(timers.size, 1);
            const [id, item] = timers.entries().next().value;
            timers.delete(id); await item.callback(); await settle();
        },
        buttons() { return document.getElementById('cg-event-list').querySelectorAll('button'); },
        button(label) { return this.buttons().find(button => button.textContent === label); }
    };
}

(async () => {
    const absent = await setup({kind: 'none'});
    assert.equal(absent.calls.length, 0);
    const denied = await setup({permissions: []});
    assert.equal(denied.calls.length, 0);
    assert.equal(denied.el('save').disabled, true);

    const initial = await setup();
    assert.equal(initial.calls.length, 1);
    assert.equal(initial.calls[0].url, '/api/connection-security/settings');
    assert.equal(initial.calls[0].options.credentials, 'same-origin');
    assert.equal(initial.calls[0].options.cache, 'no-store');
    assert.equal(initial.el('mode').value, 'off');
    assert.equal(initial.el('radius').value, '250');
    assert.equal(initial.el('license-key').value, '');
    assert.match(initial.el('license-help').textContent, /saved.*blank to keep/i);
    assert.match(initial.el('geoip').textContent, /2 days/);
    assert.equal(initial.el('save').disabled, true);
    assert.equal(initial.el('auth-install').disabled, false);
    assert.equal(initial.timers.size, 1);

    initial.el('radius').value = '300';
    initial.el('radius').fire('input');
    assert.equal(initial.el('save').disabled, false);
    assert.equal(initial.el('update-db').disabled, true);
    assert.equal(initial.timers.size, 0);
    assert.equal(initial.windowEvent('beforeunload').prevented, true);
    initial.queue.push(reply(state({radius_km: 300})));
    initial.el('settings-form').fire('submit');
    await settle();
    assert.deepEqual(JSON.parse(initial.calls[1].options.body), {
        mode: 'off', radius_km: 300, account_id: '1234', license_key: '', clear_license_key: false
    });
    assert.equal(initial.calls[1].options.method, 'PUT');
    assert.equal(initial.el('save').disabled, true);
    assert.equal(initial.windowEvent('beforeunload').prevented, undefined);

    initial.el('license-key').value = 'a-test-key-not-a-real-secret';
    initial.el('license-key').fire('input');
    initial.el('remove-key').checked = true;
    initial.el('remove-key').fire('change');
    initial.el('settings-form').fire('submit');
    await settle();
    assert.equal(initial.calls.length, 2);
    assert.match(initial.el('settings-message').textContent, /not both/);
    initial.el('remove-key').checked = false;
    initial.el('remove-key').fire('change');
    initial.queue.push(reply(state({radius_km: 300})));
    initial.el('settings-form').fire('submit');
    await settle();
    assert.equal(initial.el('license-key').value, '');
    assert.ok(!initial.el('settings-message').textContent.includes('a-test-key'));
    initial.el('remove-key').checked = true;
    initial.el('remove-key').fire('change');
    initial.confirmations.push(false);
    initial.el('settings-form').fire('submit');
    await settle();
    assert.equal(initial.calls.length, 3);
    initial.confirmations.push(true);
    initial.queue.push(reply(state({radius_km: 300, license_key_set: false})));
    initial.el('settings-form').fire('submit');
    await settle();
    assert.equal(JSON.parse(initial.calls[3].options.body).clear_license_key, true);
    assert.match(initial.el('license-help').textContent, /No license key/);

    const modeChange = await setup({initial: state({effective_identity_mode: 'offline'})});
    assert.equal(modeChange.el('save').disabled, false);
    modeChange.confirmations.push(false);
    modeChange.el('settings-form').fire('submit');
    await settle();
    assert.equal(modeChange.calls.length, 1);
    modeChange.queue.push(reply(state({identity_mode: 'offline', effective_identity_mode: 'offline'})));
    modeChange.el('settings-form').fire('submit');
    await settle();
    assert.equal(JSON.parse(modeChange.calls[1].options.body).confirm_identity_change, true);
    assert.match(modeChange.prompts[1], /identities will not be converted/);
    assert.equal(modeChange.el('save').disabled, true);

    const invalid = await setup();
    invalid.el('radius').value = '0';
    invalid.el('radius').fire('input');
    invalid.el('settings-form').fire('submit');
    await settle();
    assert.equal(invalid.calls.length, 1);
    assert.match(invalid.el('settings-message').textContent, /whole-number/);

    const install = await setup();
    install.confirmations.push(false);
    install.el('auth-install').click();
    await settle();
    assert.equal(install.calls.length, 1);
    install.queue.push(reply({message: 'AuthMe prepared.'}), reply({status: 'ready', installed: true, can_install: false}),
        reply(state({auth: {status: 'ready', installed: true, can_install: false}})));
    install.el('auth-install').click();
    await settle();
    assert.equal(install.calls[1].url, '/api/connection-security/auth/install');
    assert.deepEqual(JSON.parse(install.calls[1].options.body), {confirm: true});
    assert.match(install.prompts[1], /stopped/);
    assert.equal(install.el('auth-install').disabled, true);

    const history = await setup();
    history.queue.push(reply({candidates: [{username: '<img src=x onerror=alert(1)>',
        ip: '203.0.113.1', country: '<b>Example</b>'}], skipped: 2, warnings: ['<script>unsafe</script>']}));
    history.el('history-preview').click();
    await settle();
    assert.match(history.el('history-candidates').textContent, /<img src=x/);
    assert.match(history.el('history-summary').textContent, /1 candidate references; 2 records skipped/);
    assert.equal(history.el('history-import').disabled, false);
    history.confirmations.push(false);
    history.el('history-import').click();
    await settle();
    assert.equal(history.calls.length, 2);
    history.queue.push(reply({message: 'Imported.'}));
    history.el('history-import').click();
    await settle();
    assert.deepEqual(JSON.parse(history.calls[2].options.body), {confirm: true});
    assert.equal(history.el('history-import').disabled, true);
    assert.match(history.prompts[1], /one-time.*historical trust/);

    for (const bad of [
        reply({}, {type: 'text/html'}), reply({}, {redirected: true}),
        reply({}, {jsonError: true}), reply(null), reply({error: ''}, {status: 503})
    ]) {
        const failure = await setup({firstResponse: bad});
        assert.ok(failure.el('settings-message').textContent.length > 20);
        assert.equal(failure.el('settings-message').attributes.role, 'alert');
        assert.equal(failure.el('refresh-settings').disabled, false);
    }
    const authError = await setup({initial: state({auth: null}),
        extraResponses: [reply({error: 'Authentication status unavailable.'}, {status: 503})]});
    // An auth-status error after loading settings must not be overwritten by a success notice.
    assert.equal(authError.el('settings-message').attributes.role, 'alert');
    assert.equal(authError.el('settings-message').textContent, 'Authentication status unavailable.');
    const timedOut = await setup({abortSupport: true, firstResponse: ({options}) => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
    })});
    assert.equal(timedOut.el('refresh-settings').disabled, true);
    await timedOut.timer();
    assert.match(timedOut.el('settings-message').textContent, /timed out.*still be completing/);
    assert.equal(timedOut.el('refresh-settings').disabled, false);

    const dirty = await setup();
    dirty.el('account-id').value = '9999';
    dirty.el('account-id').fire('input');
    dirty.queue.push(reply(state({health: {status: '<img unsafe>'}})));
    dirty.el('refresh-settings').click();
    await settle();
    assert.equal(dirty.el('account-id').value, '9999');
    assert.match(dirty.el('health').textContent, /<img unsafe>/);
    assert.equal(dirty.el('save').disabled, false);

    const poll = await setup();
    let resolvePoll;
    poll.queue.push(() => new Promise(resolve => { resolvePoll = resolve; }));
    const polling = poll.timer();
    await settle();
    assert.equal(poll.calls.length, 2);
    poll.el('refresh-settings').click();
    poll.documentEvent('visibilitychange');
    assert.equal(poll.calls.length, 2); // One request at a time.
    poll.windowEvent('pagehide');
    resolvePoll(reply(state({mode: 'enforce'})));
    await polling;
    assert.equal(poll.timers.size, 0);
    assert.equal(poll.el('mode').value, 'off'); // No stale response painted after navigation.

    const hidden = await setup({kind: 'players', visible: false});
    assert.equal(hidden.calls.length, 0);
    assert.equal(hidden.timers.size, 0);
    hidden.document.hidden = false;
    hidden.documentEvent('visibilitychange');
    await settle();
    assert.equal(hidden.calls.length, 1);

    const player = await setup({kind: 'players'});
    assert.equal(player.calls.length, 1);
    assert.match(player.calls[0].url, /events\?limit=50/);
    assert.equal(player.buttons().length, 6);
    assert.match(player.el('event-list').textContent, /pre login/);
    assert.match(player.el('review-count').textContent, /1 pending/);
    player.confirmations.push(false);
    player.button('Allow next join').click();
    await settle();
    assert.equal(player.calls.length, 1);
    player.queue.push(reply({status: 'success'}), reply(events({events: []})));
    player.button('Allow next join').click();
    await settle();
    assert.equal(player.calls[1].url, '/api/connection-security/reviews/attempt-1');
    assert.deepEqual(JSON.parse(player.calls[1].options.body), {action: 'allow_once'});
    assert.match(player.prompts[1], /15 minutes/);
    assert.match(player.el('events-message').textContent, /must reconnect/);

    const viewOnly = await setup({kind: 'players', permissions: ['players.view']});
    assert.deepEqual(viewOnly.buttons().map(button => button.textContent), ['Player details', 'This player’s history']);
    assert.equal(viewOnly.button('Allow next join'), undefined);
    assert.equal(viewOnly.button('Ban source IP'), undefined);
    const safeProfile = {username: 'Player', safe_ips: [
        {ip: '2001:db8::7', source: 'player', added_at: '2026-11-18T12:00:00Z'}
    ]};
    viewOnly.queue.push(reply(safeProfile));
    viewOnly.button('Player details').click();
    await settle();
    assert.match(viewOnly.el('safe-ips').textContent, /2001:db8::7.*Saved in Minecraft/);
    assert.equal(viewOnly.el('safe-ip-form').hidden, true);
    assert.equal(viewOnly.el('safe-ips').querySelectorAll('button').length, 0);

    const safe = await setup({kind: 'players'});
    safe.queue.push(reply(safeProfile));
    safe.button('Player details').click();
    await settle();
    assert.equal(safe.el('safe-ip-form').hidden, false);
    const saved = {...safeProfile, safe_ips: [...safeProfile.safe_ips, {ip: '1.1.1.1', source: 'panel'}]};
    safe.queue.push(reply(saved));
    safe.el('safe-ip-input').value = ' 1.1.1.1 ';
    safe.el('safe-ip-form').fire('submit');
    await settle();
    assert.equal(safe.calls[2].url, '/api/connection-security/players/offline%3APlayer/safe-ips');
    assert.equal(safe.calls[2].options.method, 'POST');
    assert.deepEqual(JSON.parse(safe.calls[2].options.body), {ip: '1.1.1.1'});
    assert.match(safe.el('safe-ips').textContent, /1.1.1.1.*Added in panel/);
    safe.queue.push(reply(safeProfile));
    safe.el('safe-ips').querySelectorAll('button')[1].click();
    await settle();
    assert.equal(safe.calls[3].options.method, 'DELETE');
    assert.deepEqual(JSON.parse(safe.calls[3].options.body), {ip: '1.1.1.1'});
    assert.match(safe.el('safe-ip-message').textContent, /removed/);

    const ban = await setup({kind: 'players'});
    ban.queue.push(reply({status: 'success'}));
    ban.button('Ban source IP').click();
    await settle();
    assert.match(ban.prompts[0], /Shared networks.*other players/);
    assert.equal(ban.calls[1].url, '/api/player/ban');
    assert.equal(JSON.parse(ban.calls[1].options.body).ban_type, 'ip');
    assert.equal(ban.calls.length, 2); // Banning does not implicitly approve or resolve a review.

    const escaped = await setup({kind: 'players', initial: events({events: [{
        ...events().events[0], username: '<img onerror=unsafe>', reason: '<script>unsafe</script>',
        identity: 'offline:a/b?c', attempt_id: 'attempt/unsafe'
    }]})});
    assert.match(escaped.el('event-list').textContent, /<script>unsafe/);
    escaped.queue.push(reply({username: '<b>test</b>', references: [{city: '<script>unsafe</script>'}],
        original_ip: '203.0.113.42', accepted_ips: ['203.0.113.42'], authenticated: false}));
    escaped.button('Player details').click();
    await settle();
    assert.equal(escaped.calls[1].url, '/api/connection-security/players/offline%3Aa%2Fb%3Fc');
    assert.match(escaped.el('profile-title').textContent, /<b>test/);
    assert.match(escaped.el('profile-body').textContent, /<script>unsafe/);
    escaped.el('profile-close').click();
    assert.equal(escaped.el('profile').hidden, true);

    const coreFields = await setup({kind: 'players', initial: events({events: [{
        ...events().events[0], created_at: undefined, country: undefined, city: undefined,
        timestamp: '2026-11-18T12:00:00Z', location: {country: 'US', country_name: 'United States', city: 'Test City'},
        joined: true, authenticated: true, disconnected: true
    }]})});
    assert.match(coreFields.el('event-list').textContent, /Test City, United States/);
    assert.match(coreFields.el('event-list').textContent, /Joined: Yes.*Authentication: Confirmed.*Disconnected/);
    assert.ok(!coreFields.el('event-list').textContent.includes('Never'));

    const pagination = await setup({kind: 'players', initial: events({next_cursor: 'older&one'})});
    pagination.queue.push(reply(events()));
    pagination.el('events-next').click();
    await settle();
    assert.match(pagination.calls[1].url, /cursor=older%26one/);
    assert.equal(pagination.timers.size, 0);
    pagination.el('filter-identity').value = 'offline:test&player';
    pagination.el('filter-status').value = 'pending';
    pagination.queue.push(reply(events()));
    pagination.el('filter-form').fire('submit');
    await settle();
    assert.match(pagination.calls[2].url, /identity=offline%3Atest%26player&status=pending/);
    assert.ok(!pagination.calls[2].url.includes('cursor='));
    assert.equal(pagination.timers.size, 1);

    const templates = ['settings', 'players'].map(name => fs.readFileSync(path.join(__dirname, '../app/templates/' + name + '.html'), 'utf8'));
    templates.forEach(template => {
        assert.match(template, /filename='connection_guard.js'/);
        assert.match(template, /<h2(?: id="cg-events-title")?>{{ guard_mark\(config.get\('theme'\), 'logo'\) }}<\/h2>/);
        assert.ok(!template.includes('Connection Guard'));
    });
    assert.match(templates[0], /id="cg-license-key" type="password"/);
    assert.match(templates[0], /AuthMe is required/);
    assert.match(templates[1], /data-permission="players.view"/);
    assert.ok(!source.includes('innerHTML'));
    assert.ok(!source.includes('localStorage'));
    assert.ok(!source.includes('sessionStorage'));
    const tabsDocument = createDocument();
    const keys = ['general', 'security', 'email', 'operations'];
    const tabs = keys.map(key => {
        const tab = tabsDocument.getElementById('settings-tab-' + key);
        tab.dataset.settingsTab = key;
        return tab;
    });
    const panels = keys.map(key => {
        const panel = tabsDocument.getElementById('settings-' + key);
        panel.dataset.settingsPanel = key;
        return panel;
    });
    tabsDocument.querySelectorAll = selector => selector === '[data-settings-tab]' ? tabs :
        selector === '[data-settings-panel]' ? panels : [];
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/settings_ui.js'), 'utf8'), {
        document: tabsDocument, sessionStorage: {getItem: () => null, setItem() {}}
    });
    tabs[1].click();
    assert.equal(panels[1].hidden, false);
    assert.equal(tabsDocument.getElementById('settingsForm').hidden, true);
    assert.equal(tabsDocument.getElementById('settings-main-card').hidden, true);
    tabs[0].click();
    assert.equal(panels[1].hidden, true);
    assert.equal(tabsDocument.getElementById('settingsForm').hidden, false);
    assert.equal(tabsDocument.getElementById('settings-main-card').hidden, false);
    console.log('Byte Guard UI tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
