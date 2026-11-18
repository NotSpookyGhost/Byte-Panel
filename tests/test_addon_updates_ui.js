/* Update settings behavior with an isolated DOM; never contacts a real server. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createDocument} = require('./plugin_dom.js');
const source = fs.readFileSync(path.join(__dirname, '../app/static/addon_updates.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function state(overrides = {}) {
    return {
        enabled: false, projects: [], busy: false, status: 'idle', message: 'Ready to check updates.',
        last_checked: null, last_applied: null, engine: 'Paper', mc_version: '1.21.4',
        addons: [{filename: 'Example.jar', project_id: 'TestProj', installed_version: '1.0',
            available_version: '1.1', status: 'update_available', reason: 'Compatible release available.'},
        {filename: 'Unknown.jar', project_id: null, installed_version: null,
            available_version: null, status: 'unknown', reason: 'No verified source.'}],
        ...overrides,
    };
}

function reply(data, {status = 200, type = 'application/json', redirected = false, jsonError = null} = {}) {
    return {status, ok: status >= 200 && status < 300, redirected,
        headers: {get: () => type}, json: async () => {
            if (jsonError) throw jsonError;
            return data;
        }};
}

async function setup(initial = state(), {present = true} = {}) {
    const document = createDocument();
    const proto = Object.getPrototypeOf(document.createElement('div'));
    // Local fixture augmentation only: the shared test helper remains unchanged.
    proto.appendChild = function (node) { this.append(node); return node; };
    if (!Object.getOwnPropertyDescriptor(proto, 'innerHTML')) {
        Object.defineProperty(proto, 'innerHTML', {
            set() { throw new Error('Untrusted update text must not be rendered as HTML'); },
        });
    }
    const ids = ['addon-update-settings', 'addon-updates-enabled', 'addon-updates-rows',
        'addon-updates-message', 'addon-updates-save', 'addon-updates-check', 'addon-updates-apply',
        'addon-updates-unsaved', 'addon-updates-checked', 'addon-updates-backup-cleanup',
        'addon-updates-backup-warnings'];
    for (const id of ids) document.getElementById(id);
    if (!present) {
        const get = document.getElementById.bind(document);
        document.getElementById = id => id === 'addon-update-settings' ? null : get(id);
    }
    const calls = [], queue = [reply(initial)], timers = new Map(), events = new Map();
    let nextTimer = 1;
    const window = {addEventListener(name, callback) {
        const handlers = events.get(name) || [];
        handlers.push(callback);
        events.set(name, handlers);
    }};
    function fireWindow(name, properties = {}) {
        const event = {prevented: false, preventDefault() { this.prevented = true; }, ...properties};
        for (const callback of events.get(name) || []) callback(event);
        return event;
    }
    async function fetch(url, options = {}) {
        const call = {url, options};
        calls.push(call);
        assert.ok(queue.length, 'Unexpected request: ' + url);
        const response = queue.shift();
        if (response instanceof Error) throw response;
        return typeof response === 'function' ? response(call) : response;
    }
    function setTimeout(callback, delay) {
        const id = nextTimer++;
        timers.set(id, {callback, delay});
        return id;
    }
    function clearTimeout(id) { timers.delete(id); }
    async function runTimer() {
        assert.equal(timers.size, 1, 'Expected exactly one outstanding polling timer');
        const [id, timer] = timers.entries().next().value;
        timers.delete(id);
        await timer.callback();
        await settle();
    }
    vm.runInNewContext(source, {document, window, fetch, setTimeout, clearTimeout, console});
    await settle();
    return {document, calls, queue, timers, fireWindow, runTimer,
        el: id => document.getElementById('addon-updates-' + id),
        approvals: () => document.getElementById('addon-updates-rows').querySelectorAll('input')};
}

(async () => {
    const absent = await setup(state(), {present: false});
    assert.equal(absent.calls.length, 0);
    assert.equal(absent.timers.size, 0);

    const initial = await setup();
    assert.equal(initial.calls.length, 1);
    assert.equal(initial.calls[0].url, '/api/addon-updates');
    assert.equal(initial.calls[0].options.method, undefined);
    assert.equal(initial.calls[0].options.credentials, 'same-origin');
    assert.equal(initial.calls[0].options.cache, 'no-store');
    assert.equal(initial.el('save').disabled, true);
    assert.equal(initial.el('check').disabled, false);
    assert.equal(initial.el('apply').disabled, true);
    assert.equal(initial.approvals().length, 1); // Unknown JARs get no approval checkbox.
    assert.equal(initial.timers.size, 0); // Reading settings never checks external providers.
    assert.equal(initial.fireWindow('beforeunload').prevented, false);
    assert.equal(initial.el('backup-cleanup').hidden, true); // Older API responses remain valid.
    assert.equal(initial.el('backup-warnings').hidden, true);

    const cleanup = await setup(state({backup_retention: {keep: 2, max_age_days: 7},
        backup_cleanup: {last_run: '2026-11-18T13:00:00Z', removed: 3, retained: 2,
            protected: 1, warnings: ['An interrupted update backup needs manual review.']}}));
    assert.equal(cleanup.el('backup-cleanup').hidden, false);
    assert.match(cleanup.el('backup-cleanup').textContent,
        /3 removed, 2 retained, 1 protected for manual review/);
    assert.equal(cleanup.el('backup-warnings').hidden, false);
    assert.equal(cleanup.el('backup-warnings').children[0].textContent,
        'An interrupted update backup needs manual review.');
    assert.equal(cleanup.el('check').disabled, false); // Protected copies do not block update controls.
    assert.equal(cleanup.el('enabled').checked, false); // Cleanup status is available with updates disabled.
    assert.equal(cleanup.calls.length, 1); // Rendering status never starts cleanup or an update.

    const notRun = await setup(state({backup_cleanup: {last_run: null, removed: 0,
        retained: 0, protected: 0, warnings: []}}));
    assert.equal(notRun.el('backup-cleanup').textContent, 'Rollback backup cleanup has not run yet.');
    assert.equal(notRun.el('backup-warnings').hidden, true);

    const partialCleanup = await setup(state({backup_cleanup: {last_run: 'invalid-date',
        removed: -1, retained: '2', warnings: [null, 42, {}, '']}}));
    assert.equal(partialCleanup.el('backup-cleanup').textContent,
        'Rollback backup cleanup: Unknown \u00b7 0 removed, 0 retained, 0 protected for manual review.');
    assert.equal(partialCleanup.el('backup-warnings').hidden, true);

    cleanup.queue.push(reply(state()));
    cleanup.el('check').click();
    await settle();
    assert.equal(cleanup.el('backup-cleanup').hidden, true);
    assert.equal(cleanup.el('backup-cleanup').textContent, '');
    assert.equal(cleanup.el('backup-warnings').hidden, true);
    assert.equal(cleanup.el('backup-warnings').children.length, 0);

    initial.el('enabled').checked = true;
    initial.el('enabled').fire('change');
    initial.approvals()[0].checked = true;
    initial.approvals()[0].fire('change');
    assert.equal(initial.el('save').disabled, false);
    assert.equal(initial.el('check').disabled, true);
    assert.equal(initial.el('apply').disabled, true);
    assert.equal(initial.el('unsaved').hidden, false);
    const unsaved = initial.fireWindow('beforeunload');
    assert.equal(unsaved.prevented, true);
    assert.equal(unsaved.returnValue, '');
    initial.el('check').click();
    initial.el('apply').click();
    await settle();
    assert.equal(initial.calls.length, 1); // Dirty form must be saved before tasks run.
    initial.queue.push(reply(state({enabled: true, projects: ['TestProj']})));
    initial.el('save').click();
    await settle();
    const saveCall = initial.calls[1];
    assert.equal(saveCall.url, '/api/addon-updates');
    assert.equal(saveCall.options.method, 'PUT');
    assert.equal(saveCall.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(saveCall.options.body), {enabled: true, projects: ['TestProj']});
    assert.equal(initial.el('save').disabled, true);
    assert.equal(initial.el('apply').disabled, false);
    assert.equal(initial.el('unsaved').hidden, true);
    assert.equal(initial.fireWindow('beforeunload').prevented, false);

    const duplicates = await setup(state({enabled: true, projects: ['Previous'], addons: [
        ...state().addons, {...state().addons[0], filename: 'Duplicate.jar'}]}));
    const checkboxes = duplicates.approvals();
    assert.equal(checkboxes.length, 3);
    assert.match(duplicates.el('rows').textContent, /Previously approved Modrinth project: Previous/);
    checkboxes[0].checked = true;
    checkboxes[0].fire('change');
    assert.equal(checkboxes[1].checked, true); // Duplicate JAR rows share one project approval.
    checkboxes[2].checked = false;
    checkboxes[2].fire('change');
    duplicates.queue.push(reply(state({enabled: true, projects: ['TestProj']})));
    duplicates.el('save').click();
    await settle();
    assert.deepEqual(JSON.parse(duplicates.calls[1].options.body).projects, ['TestProj']);

    const active = state({enabled: true, projects: ['TestProj'], status: 'checking', busy: true});
    initial.queue.push(reply(active, {status: 202}));
    initial.el('check').click();
    await settle();
    assert.equal(initial.calls[2].url, '/api/addon-updates/check');
    assert.equal(initial.calls[2].options.method, 'POST');
    assert.equal(initial.el('enabled').disabled, true);
    assert.equal(initial.approvals()[0].disabled, true);
    assert.equal(initial.el('save').disabled, true);
    assert.equal(initial.el('check').disabled, true);
    assert.equal(initial.el('apply').disabled, true);
    assert.equal(initial.timers.size, 1);
    assert.equal(initial.timers.values().next().value.delay, 1500);
    initial.queue.push(reply({...active, status: 'idle'}));
    await initial.runTimer();
    assert.equal(initial.timers.size, 1); // Busy remains authoritative between phases.
    assert.equal(initial.el('check').disabled, true);
    initial.queue.push(reply(state({enabled: true, projects: ['TestProj'], message: 'Finished'})));
    await initial.runTimer();
    assert.equal(initial.timers.size, 0);
    assert.equal(initial.el('check').disabled, false);
    assert.equal(initial.el('apply').disabled, false);
    assert.equal(initial.el('message').textContent, 'Finished');
    initial.queue.push(reply({...active, status: 'applying'}, {status: 202}));
    initial.el('apply').click();
    await settle();
    assert.equal(initial.calls.at(-1).url, '/api/addon-updates/apply');
    assert.equal(initial.calls.at(-1).options.method, 'POST');
    initial.queue.push(reply(state({enabled: true, projects: ['TestProj']})));
    await initial.runTimer();
    assert.equal(initial.timers.size, 0);

    const malicious = '<img src=x onerror=alert(1)>';
    const safe = await setup(state({message: malicious,
        backup_cleanup: {last_run: '2026-11-18T13:00:00Z', removed: 0, retained: 2,
            protected: 1, warnings: [malicious]}, addons: [{filename: malicious,
        installed_version: malicious, available_version: malicious, project_id: 'TestProj',
        reason: malicious, status: 'update_available'}]}));
    assert.equal(safe.el('rows').querySelectorAll('img').length, 0);
    assert.equal(safe.el('rows').children[0].children[0].textContent, malicious);
    assert.equal(safe.el('message').textContent, malicious);
    assert.equal(safe.el('backup-warnings').hidden, false);
    assert.equal(safe.el('backup-warnings').children[0].textContent, malicious);
    assert.equal(safe.el('backup-warnings').querySelectorAll('img').length, 0);
    assert.match(safe.approvals()[0].attributes['aria-label'], /<img src=x/);

    for (const failure of [reply({}, {redirected: true}), reply({}, {type: 'text/html'}),
        reply({}, {jsonError: new SyntaxError('Invalid JSON')}),
        reply({error: 'Server operation busy'}, {status: 409}), new Error('Network unavailable')]) {
        const ui = await setup(state({enabled: true, projects: ['TestProj']}));
        const oldRows = ui.el('rows').children;
        ui.queue.push(failure);
        ui.el('check').click();
        await settle();
        assert.equal(ui.el('rows').children, oldRows);
        assert.equal(ui.el('enabled').checked, true);
        assert.equal(ui.approvals()[0].checked, true);
        assert.equal(ui.el('message').attributes.role, 'alert');
        assert.ok(ui.el('message').className.split(' ').includes('error'));
        assert.equal(ui.el('check').disabled, false);
        assert.equal(ui.timers.size, 0);
    }

    const failedSave = await setup();
    failedSave.el('enabled').checked = true;
    failedSave.el('enabled').fire('change');
    failedSave.approvals()[0].checked = true;
    failedSave.approvals()[0].fire('change');
    failedSave.queue.push(reply({error: 'Disk is full'}, {status: 500}));
    failedSave.el('save').click();
    await settle();
    assert.equal(failedSave.el('enabled').checked, true);
    assert.equal(failedSave.approvals()[0].checked, true);
    assert.equal(failedSave.el('save').disabled, false);
    assert.equal(failedSave.el('check').disabled, true);
    assert.equal(failedSave.el('apply').disabled, true);
    assert.equal(failedSave.fireWindow('beforeunload').prevented, true);

    const pollingFailure = await setup(active);
    pollingFailure.queue.push(new Error('Temporary network failure'));
    await pollingFailure.runTimer();
    assert.equal(pollingFailure.timers.size, 1);
    assert.equal(pollingFailure.timers.values().next().value.delay, 5000);
    assert.equal(pollingFailure.el('enabled').disabled, true);
    pollingFailure.queue.push(reply(state({enabled: true, projects: ['TestProj']})));
    await pollingFailure.runTimer();
    assert.equal(pollingFailure.timers.size, 0);

    const hidden = await setup(active);
    hidden.fireWindow('pagehide');
    assert.equal(hidden.timers.size, 0);

    for (const shouldFail of [false, true]) {
        const leaving = await setup(active);
        let resolveFetch, rejectFetch;
        leaving.queue.push(() => new Promise((resolve, reject) => {
            resolveFetch = resolve;
            rejectFetch = reject;
        }));
        const inFlight = leaving.runTimer();
        await settle();
        assert.equal(leaving.calls.length, 2);
        leaving.fireWindow('pagehide');
        if (shouldFail) rejectFetch(new Error('Request failed after navigation'));
        else resolveFetch(reply(active));
        await inFlight;
        assert.equal(leaving.timers.size, 0,
            'A pending ' + (shouldFail ? 'failed' : 'successful') + ' response must not restart hidden-page polling');

        // Back/forward cache restoration resumes read-only status checks, not an update action.
        leaving.queue.push(reply(active));
        leaving.fireWindow('pageshow', {persisted: true});
        await settle();
        assert.equal(leaving.calls.length, 3);
        assert.equal(leaving.calls.at(-1).url, '/api/addon-updates');
        assert.equal(leaving.calls.at(-1).options.method, undefined);
        assert.equal(leaving.timers.size, 1);
        assert.equal(leaving.timers.values().next().value.delay, 1500);
        leaving.queue.push(reply(state({enabled: true, projects: ['TestProj']})));
        await leaving.runTimer();
        assert.equal(leaving.timers.size, 0);
    }

    const dirtyReturn = await setup();
    dirtyReturn.el('enabled').checked = true;
    dirtyReturn.el('enabled').fire('change');
    dirtyReturn.fireWindow('pagehide');
    dirtyReturn.fireWindow('pageshow', {persisted: true});
    await settle();
    assert.equal(dirtyReturn.calls.length, 1, 'Restoration must not overwrite unsaved settings');
    assert.equal(dirtyReturn.el('enabled').checked, true);
    assert.equal(dirtyReturn.fireWindow('beforeunload').prevented, true);
    assert.equal(dirtyReturn.timers.size, 0);
    console.log('Add-on update UI: opt-in settings, cleanup status, safe rendering, polling, errors and dirty-state checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
