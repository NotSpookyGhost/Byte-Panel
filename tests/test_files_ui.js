'use strict';
// Behavior tests for the real file-manager script. Browser/layout tests live separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createDocument} = require('./plugin_dom.js');
const PluginConfigSearch = require('../app/static/plugin_search.js');
const source = fs.readFileSync(path.join(__dirname, '../app/static/files.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const tests = [];
const test = (name, action) => tests.push({name, action});
const fileEntry = (name, editable = true) => ({
    name: name.split('/').at(-1), path: name, kind: 'file', size: 25, editable,
});
const folderEntry = name => ({name: name.split('/').at(-1), path: name, kind: 'folder', size: 0, editable: false});

async function setup({manage = true, initialPath = '', extraEntries = [], responses = {}} = {}) {
    const document = createDocument();
    // Fail loudly if a future renderer interprets a filename or API message as HTML.
    const guarded = new WeakSet();
    const guard = node => {
        if (!guarded.has(node)) {
            Object.defineProperty(node, 'innerHTML', {
                set() { throw new Error('File-manager content must not use innerHTML.'); },
            });
            guarded.add(node);
        }
        return node;
    };
    for (const method of ['getElementById', 'createElement', 'createElementNS']) {
        const original = document[method].bind(document);
        document[method] = (...args) => guard(original(...args));
    }
    const byId = id => document.getElementById(id);
    const manager = byId('file-manager');
    const ids = ['files-content', 'files-message', 'files-list', 'files-search', 'files-search-previous',
        'files-search-next', 'files-search-case', 'files-search-status', 'files-save', 'files-reload',
        'files-close', 'files-editor-state', 'files-editor-path', 'files-filter', 'files-list-status',
        'files-breadcrumbs', 'files-upload-form', 'files-upload', 'files-upload-button', 'files-new-folder',
        'files-new-file', 'files-refresh'];
    for (const id of ids) manager.append(byId(id));
    for (const id of ['files-save', 'files-reload', 'files-close', 'files-upload', 'files-upload-button',
        'files-new-folder', 'files-new-file', 'files-refresh']) byId(id).className = 'file-interactive';
    for (const id of ['files-save', 'files-new-folder', 'files-new-file', 'files-upload-form']) {
        byId(id).dataset.permission = 'files.manage';
    }
    byId('files-upload').files = [];
    byId('files-message').hidden = true;
    const entries = new Map([folderEntry('world'), fileEntry('config.txt'), fileEntry('second.txt'),
        fileEntry('server.jar', false), fileEntry('world/settings.yml'), ...extraEntries]
        .map(entry => [entry.path, {...entry}]));
    const contents = new Map([
        ['config.txt', 'key=true\nkey=false\n'], ['second.txt', 'second file\n'], ['world/settings.yml', 'world: enabled\n'],
    ]);
    const calls = [], confirmations = [], confirmAnswers = [], prompts = [], promptAnswers = [], history = [];
    const listeners = new Map();
    const queues = new Map(Object.entries(responses).map(([key, values]) => [key, [...values]]));
    const window = {
        PluginConfigSearch, panelCan: permission => permission === 'files.view' || manage,
        confirm: text => { confirmations.push(text); return confirmAnswers.length ? confirmAnswers.shift() : false; },
        prompt: (text, value) => { prompts.push({text, value}); return promptAnswers.length ? promptAnswers.shift() : null; },
        location: new URL('http://panel.test/files' + (initialPath ? '?path=' + encodeURIComponent(initialPath) : '')),
        history: {replaceState: (state, title, url) => {
            history.push(url); window.location = new URL(url, window.location);
        }},
        addEventListener: (type, action) => { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(action); },
    };
    class FormData {
        constructor() { this.values = new Map(); }
        append(name, value) { this.values.set(name, value); }
        get(name) { return this.values.get(name) ?? null; }
    }
    function normalResponse(url, options, body) {
        const request = new URL(url, window.location);
        const target = request.searchParams.get('path') || '';
        if (request.pathname === '/api/files/list') {
            return {data: {path: target, entries: [...entries.values()].filter(entry => {
                const parent = entry.path.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/')) : '';
                return parent === target;
            })}};
        }
        if (request.pathname === '/api/files/file' && !options.method) {
            return {data: {path: target, content: contents.get(target) || 'plain text\n', revision: 'revision:' + target}};
        }
        if (request.pathname === '/api/files/entry' && !options.method) {
            return {data: {...entries.get(target), revision: 'preview:' + target}};
        }
        if (request.pathname === '/api/files/file' && options.method === 'PUT') {
            contents.set(body.path, body.content);
            return {data: {path: body.path, revision: 'saved:' + body.path, message: 'File saved.', recovery_id: 'save-copy'}};
        }
        if (request.pathname === '/api/files/upload') {
            const name = (body.path ? body.path + '/' : '') + body.file.name;
            entries.set(name, fileEntry(name));
            return {status: 201, data: {path: name, message: 'File uploaded.'}};
        }
        if (request.pathname === '/api/files/move') {
            const entry = entries.get(body.path);
            entries.delete(body.path);
            entries.set(body.destination, {...entry, path: body.destination, name: body.destination.split('/').at(-1)});
            return {data: {path: body.destination, message: 'Entry moved.'}};
        }
        if (request.pathname === '/api/files/entry' && options.method === 'DELETE') {
            for (const name of entries.keys()) if (name === body.path || name.startsWith(body.path + '/')) entries.delete(name);
            return {data: {path: body.path, message: 'Moved to recovery.', recovery_id: 'deleted-copy', recovery_kind: 'trash'}};
        }
        throw new Error('Unexpected request: ' + (options.method || 'GET') + ' ' + url);
    }
    async function fetch(url, options = {}) {
        const body = options.body instanceof FormData ? Object.fromEntries(options.body.values)
            : typeof options.body === 'string' ? JSON.parse(options.body) : null;
        const call = {url, options, body, jsonReads: 0}; calls.push(call);
        const key = (options.method || 'GET') + ' ' + new URL(url, window.location).pathname;
        const queue = queues.get(key);
        const queued = queue && queue.length ? queue.shift() : undefined;
        const response = queued === undefined ? normalResponse(url, options, body)
            : typeof queued === 'function' ? await queued(call) : await queued;
        if (response instanceof Error) throw response;
        const status = response.status || 200;
        return {
            ok: status >= 200 && status < 300, status, redirected: Boolean(response.redirected),
            headers: {get: () => response.contentType || 'application/json'},
            json: async () => { call.jsonReads += 1; if (response.jsonError) throw response.jsonError; return response.data; },
        };
    }
    vm.runInNewContext(source, {document, window, fetch, FormData, URL, URLSearchParams, console});
    await settle();
    const row = name => byId('files-list').children.find(node => node.dataset.path === name);
    const action = (name, label) => row(name).querySelectorAll('button').find(node => node.textContent === label);
    return {byId, calls, confirmations, confirmAnswers, prompts, promptAnswers, history, row, action, queues,
        async open(name) { row(name).children[0].children[0].click(); await settle(); },
        async click(id) { byId(id).click(); await settle(); },
        draft(value) { byId('files-content').value = value; byId('files-content').fire('input'); },
        async upload(files) { byId('files-upload').files = files; byId('files-upload-form').fire('submit'); await settle(); },
        emit(type) {
            const event = {preventDefault() { this.prevented = true; }};
            for (const listener of listeners.get(type) || []) listener(event);
            return event;
        },
    };
}

test('filenames stay text, folder icons precede labels, downloads encode paths, and filtering is literal', async () => {
    const name = '<img onerror=alert(1)> & \u0027quoted\u0027 #.txt';
    const ui = await setup({extraEntries: [fileEntry(name)]});
    const folder = ui.row('world').children[0].children[0];
    assert.equal(folder.children[0].tagName, 'svg');
    assert.equal(folder.children[0].attributes['aria-hidden'], 'true');
    assert.equal(folder.children[1].textContent, 'world');
    assert.equal(ui.row(name).children[0].children[0].textContent, name);
    assert.equal(ui.row(name).querySelectorAll('img').length, 0);
    assert.equal(ui.row(name).querySelectorAll('a')[0].href, '/api/files/download?path=' + encodeURIComponent(name));
    await ui.open('server.jar');
    assert.match(ui.byId('files-message').textContent, /Use Download.*server\.jar/);
    assert.equal(ui.calls.some(call => call.url.startsWith('/api/files/file?')), false);
    ui.byId('files-filter').value = '<IMG'; ui.byId('files-filter').fire('input');
    assert.equal(ui.byId('files-list').children.length, 1);
    assert.ok(ui.row(name));
    assert.match(ui.byId('files-list-status').textContent, /^1 of 5 entries$/);
});

test('read-only users can browse, download and search but cannot issue writes', async () => {
    const ui = await setup({manage: false});
    assert.equal(ui.row('config.txt').querySelectorAll('button').length, 1);
    assert.equal(ui.row('config.txt').querySelectorAll('a').length, 1);
    assert.equal(ui.byId('files-new-file').disabled, true);
    assert.equal(ui.byId('files-new-folder').disabled, true);
    await ui.open('config.txt');
    assert.equal(ui.byId('files-content').readOnly, true);
    assert.equal(ui.byId('files-search').disabled, false);
    ui.byId('files-search').value = 'key'; ui.byId('files-search').fire('input');
    await ui.click('files-search-next');
    assert.equal(ui.byId('files-search-status').textContent, '1 of 2');
    ui.draft('synthetic change');
    // Even synthetic events bypassing disabled controls may not send a mutation.
    for (const id of ['files-save', 'files-new-file', 'files-new-folder']) { ui.byId(id).fire('click'); await settle(); }
    await ui.upload([{name: 'config.txt', size: 5}]);
    assert.equal(ui.calls.some(call => call.options.method), false);
    assert.equal(ui.byId('files-save').disabled, true);
});

test('upload replacements require confirmation and use the exact server revision', async () => {
    for (const replace of [false, true]) {
        const ui = await setup({responses: {'POST /api/files/upload': [
            {status: 409, data: {error: 'File exists.', conflict: true, revision: 'existing-upload-revision'}},
        ]}});
        await ui.open('world');
        ui.confirmAnswers.push(replace);
        await ui.upload([{name: 'settings.yml', size: 20}]);
        const uploads = ui.calls.filter(call => call.url === '/api/files/upload');
        assert.equal(uploads.length, replace ? 2 : 1);
        assert.equal(uploads[0].body.path, 'world');
        assert.equal(uploads[0].body.replace_revision, undefined);
        assert.match(ui.confirmations[0], /Replace settings\.yml.*backed up/);
        if (replace) assert.equal(uploads[1].body.replace_revision, 'existing-upload-revision');
        assert.equal(ui.byId('files-message').textContent, (replace ? 1 : 0) + ' file(s) uploaded.');
        assert.equal(ui.calls.at(-1).url, '/api/files/list?path=world');
    }
});

test('stale saves retain the draft, selected file, and original revision', async () => {
    const error = '<strong>File changed</strong>; reload before saving.';
    const ui = await setup({responses: {'PUT /api/files/file': [{status: 409, data: {error}}]}});
    await ui.open('config.txt');
    ui.draft('draft=keep this\n');
    await ui.click('files-save');
    assert.equal(ui.byId('files-content').value, 'draft=keep this\n');
    assert.equal(ui.byId('files-editor-path').textContent, 'server/config.txt');
    assert.equal(ui.byId('files-editor-state').textContent, 'Unsaved changes');
    assert.equal(ui.byId('files-save').disabled, false);
    assert.equal(ui.byId('files-message').textContent, error);
    assert.equal(ui.byId('files-message').dataset.error, 'true');
    assert.equal(ui.byId('files-message').children.length, 0);
    await ui.click('files-save');
    const saves = ui.calls.filter(call => call.options.method === 'PUT');
    assert.ok(saves.every(call => call.body.revision === 'revision:config.txt'));
    assert.equal(ui.byId('files-editor-state').textContent, 'Saved');
    assert.match(ui.byId('files-message').textContent, /\/data\/file-manager-backups\/save-copy/);
});

test('cancelled dirty navigation, reload, close and page unload protect draft content', async () => {
    const ui = await setup();
    await ui.open('config.txt'); ui.draft('unsaved draft');
    const count = ui.calls.length;
    await ui.open('world'); await ui.open('second.txt');
    await ui.click('files-reload'); await ui.click('files-close');
    assert.equal(ui.confirmations.length, 4);
    assert.ok(ui.confirmations.every(text => /Discard unsaved/.test(text)));
    assert.equal(ui.calls.length, count);
    assert.equal(ui.history.length, 0);
    assert.equal(ui.byId('files-content').value, 'unsaved draft');
    const event = ui.emit('beforeunload');
    assert.equal(event.prevented, true); assert.equal(event.returnValue, '');
    ui.confirmAnswers.push(true);
    await ui.open('world');
    assert.equal(ui.byId('files-content').value, '');
    assert.equal(ui.byId('files-editor-state').textContent, 'No file selected');
    assert.equal(ui.history.at(-1), '/files?path=world');
    assert.equal(ui.emit('beforeunload').prevented, undefined);
});

test('rename/move previews its revision and respects cancellation before writing', async () => {
    const ui = await setup();
    await ui.open('config.txt'); ui.draft('do not discard');
    const start = ui.calls.length;
    ui.action('config.txt', 'Rename / move').click(); await settle();
    assert.equal(ui.calls.length, start);
    assert.equal(ui.prompts.length, 0);
    ui.confirmAnswers.push(true); ui.promptAnswers.push(null);
    ui.action('config.txt', 'Rename / move').click(); await settle();
    assert.match(ui.calls.at(-1).url, /^\/api\/files\/entry\?path=config\.txt$/);
    assert.equal(ui.calls.some(call => call.url === '/api/files/move'), false);
    assert.equal(ui.byId('files-content').value, 'do not discard');
    const destination = 'world/new & quoted.txt';
    ui.confirmAnswers.push(true); ui.promptAnswers.push(destination);
    ui.action('config.txt', 'Rename / move').click(); await settle();
    const moved = ui.calls.find(call => call.url === '/api/files/move');
    assert.deepEqual(moved.body, {path: 'config.txt', destination, revision: 'preview:config.txt'});
    assert.equal(ui.prompts.at(-1).value, 'config.txt');
    assert.equal(ui.byId('files-content').value, '');
    assert.equal(ui.row('config.txt'), undefined);
});

test('deletion requires an exact path and revision, and explains folder recovery', async () => {
    const ui = await setup();
    for (const answer of [null, 'WORLD', 'world ']) {
        ui.promptAnswers.push(answer);
        ui.action('world', 'Delete').click(); await settle();
        assert.equal(ui.calls.some(call => call.options.method === 'DELETE'), false);
        assert.ok(ui.row('world'));
    }
    assert.match(ui.byId('files-message').textContent, /did not match.*Nothing was deleted/);
    ui.promptAnswers.push('world');
    ui.action('world', 'Delete').click(); await settle();
    const deletion = ui.calls.find(call => call.options.method === 'DELETE');
    assert.deepEqual(deletion.body, {path: 'world', revision: 'preview:world', confirmation: 'world'});
    assert.match(ui.prompts.at(-1).text, /folder and ALL its contents/);
    assert.match(ui.prompts.at(-1).text, /exact path.*\nworld/s);
    assert.equal(ui.row('world'), undefined);
    assert.match(ui.byId('files-message').textContent, /\/data\/file-manager-trash\/deleted-copy/);
});

test('HTML and redirected responses produce a visible safe error and allow retry', async () => {
    for (const response of [
        {status: 502, contentType: 'text/html', data: '<!DOCTYPE html><h1>Gateway error</h1>'},
        {status: 200, redirected: true, data: {message: 'Login page'}},
    ]) {
        const ui = await setup({responses: {'GET /api/files/list': [response]}});
        assert.equal(ui.calls[0].jsonReads, 0);
        assert.equal(ui.byId('files-message').hidden, false);
        assert.equal(ui.byId('files-message').dataset.error, 'true');
        assert.match(ui.byId('files-message').textContent, /unexpected response.*sign in/);
        assert.equal(ui.byId('files-message').children.length, 0);
        assert.equal(ui.byId('files-refresh').disabled, false);
        await ui.click('files-refresh');
        assert.ok(ui.row('config.txt'));
    }
});

test('oversize and partial upload failures retain an accurate count and unlock controls', async () => {
    const ui = await setup({responses: {'POST /api/files/upload': [
        {status: 201, data: {path: 'first.txt', message: 'Uploaded.'}},
        {status: 409, data: {error: 'Stop Minecraft before changing files.'}},
    ]}});
    await ui.upload([{name: 'first.txt', size: 1}, {name: 'blocked.txt', size: 1}, {name: 'not-attempted.txt', size: 1}]);
    assert.equal(ui.calls.filter(call => call.url === '/api/files/upload').length, 2);
    assert.match(ui.byId('files-message').textContent, /1 file\(s\) uploaded\. Stop Minecraft/);
    assert.equal(ui.byId('files-message').dataset.error, 'true');
    assert.equal(ui.byId('files-upload-button').disabled, false);
    const bothFailed = await setup({responses: {'POST /api/files/upload': [
        {status: 201, data: {path: 'first.txt', message: 'Uploaded.'}},
        {status: 409, data: {error: 'Original upload failure.'}},
    ]}});
    bothFailed.queues.set('GET /api/files/list', [{status: 503, data: {error: 'Directory unavailable.'}}]);
    await bothFailed.upload([{name: 'first.txt', size: 1}, {name: 'blocked.txt', size: 1}]);
    assert.match(bothFailed.byId('files-message').textContent, /1 file\(s\) uploaded\. Original upload failure\./);
    assert.match(bothFailed.byId('files-message').textContent, /list could not be refreshed: Directory unavailable/);
    assert.equal(bothFailed.byId('files-message').dataset.error, 'true');
    assert.equal(bothFailed.byId('files-upload-button').disabled, false);
    const large = await setup();
    await large.upload([{name: 'huge.dat', size: 256 * 1024 * 1024 + 1}]);
    assert.equal(large.calls.some(call => call.url === '/api/files/upload'), false);
    assert.match(large.byId('files-message').textContent, /0 file\(s\) uploaded\. huge\.dat exceeds the 256 MiB/);
    assert.equal(large.byId('files-refresh').disabled, false);
});

test('initial folder and breadcrumbs work, while busy operations prevent duplicate writes', async () => {
    let release;
    const blockedSave = new Promise(resolve => { release = resolve; });
    const ui = await setup({initialPath: 'world', responses: {'PUT /api/files/file': [blockedSave]}});
    assert.equal(ui.calls[0].url, '/api/files/list?path=world');
    assert.equal(ui.byId('files-breadcrumbs').querySelectorAll('button').at(-1).textContent, 'world');
    await ui.open('world/settings.yml'); ui.draft('changed: true\n');
    ui.byId('files-save').fire('click'); await settle();
    assert.equal(ui.byId('files-content').disabled, true);
    assert.equal(ui.byId('files-search').disabled, true);
    assert.equal(ui.byId('files-save').disabled, true);
    ui.byId('files-save').fire('click'); ui.byId('files-refresh').fire('click');
    ui.byId('files-upload-form').fire('submit'); await settle();
    assert.equal(ui.calls.filter(call => call.options.method === 'PUT').length, 1);
    release({data: {path: 'world/settings.yml', revision: 'new-revision', message: 'Saved.'}});
    await settle();
    assert.equal(ui.byId('files-content').disabled, false);
    assert.equal(ui.byId('files-editor-state').textContent, 'Saved');
    assert.equal(ui.byId('files-search').disabled, false);
    ui.byId('files-breadcrumbs').querySelectorAll('button')[0].click(); await settle();
    assert.equal(ui.history.at(-1), '/files');
    assert.ok(ui.row('world'));
    assert.ok(ui.calls.every(call => call.options.credentials === 'same-origin' && call.options.cache === 'no-store'));
});

(async () => {
    for (const {name, action} of tests) { await action(); console.log('PASS ' + name); }
    console.log('File-manager UI: ' + tests.length + ' checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
