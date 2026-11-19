'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createDocument} = require('./plugin_dom.js');
const PluginConfigSearch = require('../app/static/plugin_search.js');
const source = fs.readFileSync(path.join(__dirname, '../app/static/plugins.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

async function setup({manage = true, confirm = true, failDelete = false, uploadResponses = [], failedFolder = null,
    addonKind = 'plugin', highlight = false, fileResponses = [], saveResponses = []} = {}) {
    const document = createDocument();
    const manager = document.getElementById('plugin-manager');
    manager.dataset.csrfToken = 'plugin-csrf-test';
    if (addonKind === 'mod') {
        manager.dataset.addonKind = 'mod';
        manager.dataset.rootName = 'mods';
    }
    const ids = ['plugin-content', 'plugin-file-list', 'plugins-message', 'editor-state', 'save-config', 'reload-config',
        'config-search', 'search-previous', 'search-next', 'search-match-case', 'search-status', 'plugin-breadcrumbs',
        'plugin-list-status', 'editor-path', 'refresh-files', 'plugin-upload-form', 'plugin-upload', 'plugin-upload-destination'];
    for (const id of ids) manager.append(document.getElementById(id));
    const editor = document.getElementById('plugin-content');
    const focusCalls = [], syntaxCalls = [], confirmAnswers = [];
    // Native textareas normalize newlines and put the caret at EOF on .value assignment.
    // The basic shared DOM deliberately does not emulate these browser behaviors.
    let value = '';
    Object.defineProperty(editor, 'value', {
        get: () => value,
        set: content => {
            value = String(content).replace(/\r\n?/g, '\n');
            editor.selectionStart = editor.selectionEnd = value.length;
        },
    });
    editor.scrollTop = editor.scrollLeft = 0;
    editor.focus = options => {
        focusCalls.push({preventScroll: options && options.preventScroll, disabled: editor.disabled});
        document.activeElement = editor;
        if (!options || !options.preventScroll) editor.scrollTop = 99999;
    };
    const calls = [];
    const confirmations = [];
    let removed = false;
    const entries = [
        {path: 'Demo', name: 'Demo', kind: 'folder', size: 0, editable: false},
        {path: 'LuckPerms', name: 'LuckPerms', kind: 'folder', size: 0, editable: false},
        {path: 'Demo.JAR', name: 'Demo.JAR', kind: 'file', size: 1234, editable: false},
        {path: 'config.yml', name: 'config.yml', kind: 'file', size: 50, editable: true},
        {path: 'second.json', name: 'second.json', kind: 'file', size: 20, editable: true},
    ];
    const window = {
        PluginConfigSearch, panelCan: permission => permission === 'plugins.view' || manage,
        confirm: message => { confirmations.push(message); return confirmAnswers.length ? confirmAnswers.shift() : confirm; },
        addEventListener() {},
    };
    if (highlight) window.PanelConfigEditor = {attach(node) {
        assert.equal(node, editor);
        return {
            setFile: filename => syntaxCalls.push({filename, value: editor.value}),
            syncScroll: () => syntaxCalls.push({scrollTop: editor.scrollTop, scrollLeft: editor.scrollLeft}),
        };
    }};
    async function fetch(url, options = {}) {
        calls.push({url, options});
        let data;
        let status = 200;
        if (url.startsWith('/api/plugins/files?')) {
            const folder = new URLSearchParams(url.split('?')[1]).get('path');
            if (folder === failedFolder) { status = 403; data = {error: 'Cannot access this folder.'}; }
            else data = {path: folder, entries: folder === 'LuckPerms' ? [
                {path: 'LuckPerms/imports', name: 'imports', kind: 'folder', size: 0, editable: false},
                {path: 'LuckPerms/preset.json.gz', name: 'preset.json.gz', kind: 'file', size: 150, editable: false}
            ] : folder ? [] : entries.filter(entry => !removed || entry.path !== 'Demo.JAR')};
        } else if (url.startsWith('/api/plugins/file?')) {
            const filename = new URLSearchParams(url.split('?')[1]).get('path');
            const response = fileResponses.shift();
            status = response ? response.status || 200 : 200;
            data = response ? response.data
                : {path: filename, content: 'key: true\nkey: false', revision: 'config-revision'};
        } else if (url === '/api/plugins/file' && options.method === 'PUT') {
            const response = saveResponses.shift();
            status = response ? response.status || 200 : 200;
            data = response ? response.data : {revision: 'saved-revision', message: 'Config saved.'};
        } else if (url.startsWith('/api/plugins/delete?')) {
            data = {path: 'Demo.JAR', name: 'Demo.JAR', revision: 'jar-revision'};
        } else if (url === '/api/plugins/delete' && options.method === 'DELETE') {
            if (failDelete) { status = 409; data = {error: 'This JAR changed. Confirm again.'}; }
            else { removed = true; data = {message: (addonKind === 'mod' ? 'Mod' : 'Plugin') + ' JAR deleted. Configs kept.', backup: 'safe-Demo.JAR.bak'}; }
        } else if (url === '/api/plugins/upload' && options.method === 'POST') {
            const response = uploadResponses.shift() || {status: 201, data: {message: 'Uploaded'}};
            status = response.status;
            data = response.data;
        } else throw new Error('Unexpected request: ' + url);
        return {ok: status >= 200 && status < 300, status, headers: {get: () => 'application/json'}, json: async () => data};
    }
    class FormData {
        constructor() { this.values = new Map(); }
        append(name, value) { this.values.set(name, value); }
        get(name) { return this.values.get(name) ?? null; }
    }
    vm.runInNewContext(source, {document, window, fetch, console, FormData});
    await settle();
    const list = document.getElementById('plugin-file-list');
    const row = name => list.children.find(element => element.dataset.path === name);
    const deleteButton = () => row('Demo.JAR').children[2].children[0];
    return {document, calls, confirmations, row, deleteButton, editor, focusCalls, syntaxCalls,
        confirmAnswers, fileResponses, saveResponses};
}

(async () => {
    const ui = await setup();
    const folder = ui.row('Demo').children[0].children[0];
    assert.equal(folder.children[0].tagName, 'svg');
    assert.equal(folder.children[0].attributes['aria-hidden'], 'true');
    assert.equal(folder.children[1].textContent, 'Demo'); // Icon is left of folder name.
    assert.equal(ui.deleteButton().textContent, 'Delete');
    assert.equal(ui.deleteButton().attributes['aria-label'], 'Delete Demo.JAR');
    assert.equal(ui.row('Demo.JAR').querySelectorAll('a').length, 0);
    assert.equal(ui.row('config.yml').querySelectorAll('a')[0].textContent, 'Download');
    ui.row('config.yml').children[0].children[0].click();
    await settle();
    const editor = ui.document.getElementById('plugin-content');
    const search = ui.document.getElementById('config-search');
    assert.equal(search.disabled, false);
    search.value = 'key';
    search.fire('input');
    ui.document.getElementById('search-next').click();
    assert.equal(ui.document.getElementById('search-status').textContent, '1 of 2');
    editor.value = 'unsaved edit';
    editor.fire('input');
    ui.deleteButton().click();
    await settle();
    const deletion = ui.calls.find(call => call.options.method === 'DELETE');
    assert.deepEqual(JSON.parse(deletion.options.body), {path: 'Demo.JAR', revision: 'jar-revision'});
    assert.equal(deletion.options.headers['X-Plugin-CSRF-Token'], 'plugin-csrf-test');
    assert.match(ui.confirmations[0], /config folders will be kept/);
    assert.equal(ui.row('Demo.JAR'), undefined);
    assert.ok(ui.row('Demo'));
    assert.equal(editor.value, 'unsaved edit'); // Deleting a JAR doesn't discard config edits.
    assert.equal(ui.document.getElementById('editor-state').textContent, 'Unsaved changes');
    assert.match(ui.document.getElementById('plugins-message').textContent, /safe-Demo.JAR.bak/);

    const cancelled = await setup({confirm: false});
    cancelled.deleteButton().click();
    await settle();
    assert.equal(cancelled.calls.some(call => call.options.method === 'DELETE'), false);
    assert.ok(cancelled.row('Demo.JAR'));

    const denied = await setup({manage: false});
    assert.equal(denied.row('Demo.JAR').children[2].children.length, 0);
    denied.row('config.yml').children[0].children[0].click();
    await settle();
    assert.equal(denied.document.getElementById('plugin-content').readOnly, true);
    assert.equal(denied.document.getElementById('config-search').disabled, false);
    assert.equal(denied.document.getElementById('save-config').disabled, true);

    const conflict = await setup({failDelete: true});
    conflict.deleteButton().click();
    await settle();
    assert.ok(conflict.row('Demo.JAR'));
    assert.match(conflict.document.getElementById('plugins-message').textContent, /changed/);
    assert.equal(conflict.document.getElementById('plugins-message').dataset.error, 'true');
    assert.equal(conflict.deleteButton().disabled, false);

    const upload = await setup();
    upload.row('LuckPerms').children[0].children[0].click();
    await settle();
    assert.match(upload.document.getElementById('plugin-upload-destination').textContent, /plugins\/LuckPerms\//);
    assert.equal(upload.row('LuckPerms/preset.json.gz').children[0].children[1].textContent, 'Data file (download only)');
    assert.equal(upload.row('LuckPerms/preset.json.gz').children[2].children[0].textContent, 'Download');
    upload.row('LuckPerms/imports').children[0].children[0].click();
    await settle();
    assert.match(upload.document.getElementById('plugin-upload-destination').textContent, /plugins\/LuckPerms\/imports\//);
    assert.match(upload.document.getElementById('plugin-list-status').textContent, /Upload config or data files here/);
    const input = upload.document.getElementById('plugin-upload');
    input.files = [{name: 'preset.json.gz'}, {name: 'large.json'}, {name: 'Demo.JAR'}];
    upload.document.getElementById('plugin-upload-form').fire('submit');
    await settle();
    const uploaded = upload.calls.filter(call => call.url === '/api/plugins/upload');
    assert.equal(uploaded.length, 3);
    assert.deepEqual(uploaded.map(call => call.options.body.get('path')), ['LuckPerms/imports', 'LuckPerms/imports', '']);
    assert.deepEqual(uploaded.map(call => call.options.body.get('file').name), ['preset.json.gz', 'large.json', 'Demo.JAR']);
    assert.ok(uploaded.every(call => call.options.headers['X-Plugin-CSRF-Token'] === 'plugin-csrf-test'));
    assert.match(upload.document.getElementById('plugins-message').textContent, /3 file\(s\) uploaded/);
    assert.match(upload.document.getElementById('plugins-message').textContent, /uploads do not import data automatically/);
    assert.equal(upload.calls.at(-1).url, '/api/plugins/files?path=LuckPerms%2Fimports');

    for (const confirm of [true, false]) {
        const replacement = await setup({confirm, uploadResponses: [
            {status: 409, data: {error: 'Already exists', conflict: true, revision: 'data-revision'}}
        ]});
        replacement.row('LuckPerms').children[0].children[0].click();
        await settle();
        replacement.document.getElementById('plugin-upload').files = [{name: 'preset.json.gz'}];
        replacement.document.getElementById('plugin-upload-form').fire('submit');
        await settle();
        const writes = replacement.calls.filter(call => call.url === '/api/plugins/upload');
        assert.equal(writes.length, confirm ? 2 : 1);
        assert.match(replacement.confirmations[0], /plugins\/LuckPerms\/preset.json.gz/);
        assert.match(replacement.confirmations[0], /backup/);
        if (confirm) assert.equal(writes[1].options.body.get('replace_revision'), 'data-revision');
        else assert.match(replacement.document.getElementById('plugins-message').textContent, /skipped/);
    }

    denied.document.getElementById('plugin-upload').files = [{name: 'preset.json.gz'}];
    denied.document.getElementById('plugin-upload-form').fire('submit');
    await settle();
    assert.equal(denied.calls.some(call => call.url === '/api/plugins/upload'), false);

    const inaccessible = await setup({failedFolder: 'LuckPerms'});
    inaccessible.row('LuckPerms').children[0].children[0].click();
    await settle();
    assert.match(inaccessible.document.getElementById('plugin-upload-destination').textContent, /open a plugin subfolder/);
    assert.match(inaccessible.document.getElementById('plugins-message').textContent, /Cannot access/);

    const partial = await setup({uploadResponses: [{status: 413, data: {error: 'File is too large.'}}]});
    partial.row('LuckPerms').children[0].children[0].click();
    await settle();
    partial.document.getElementById('plugin-upload').files = [{name: 'too-large.dat'}, {name: 'preset.json.gz'}];
    partial.document.getElementById('plugin-upload-form').fire('submit');
    await settle();
    assert.match(partial.document.getElementById('plugins-message').textContent, /1 file\(s\) uploaded/);
    assert.match(partial.document.getElementById('plugins-message').textContent, /too-large.dat: File is too large/);
    assert.equal(partial.document.getElementById('plugins-message').dataset.error, 'true');
    const mods = await setup({addonKind: 'mod'});
    assert.equal(mods.document.getElementById('plugin-breadcrumbs').children[0].textContent, 'mods');
    assert.equal(mods.row('Demo.JAR').children[0].children[1].textContent, 'Mod JAR');
    assert.match(mods.document.getElementById('plugin-upload-destination').textContent, /mods\//);
    mods.row('config.yml').children[0].children[0].click();
    await settle();
    assert.equal(mods.document.getElementById('editor-path').textContent, 'mods/config.yml');
    mods.deleteButton().click();
    await settle();
    assert.match(mods.confirmations[0], /Delete mods\/Demo.JAR/);
    assert.match(mods.confirmations[0], /unload the mod/);
    assert.match(mods.document.getElementById('plugins-message').textContent, /\/data\/mod-config-backups\//);

    const modUpload = await setup({addonKind: 'mod', uploadResponses: [
        {status: 409, data: {error: 'Already exists', conflict: true, revision: 'mod-revision'}}
    ]});
    modUpload.document.getElementById('plugin-upload').files = [{name: 'Demo.JAR'}];
    modUpload.document.getElementById('plugin-upload-form').fire('submit');
    await settle();
    const modWrites = modUpload.calls.filter(call => call.url === '/api/plugins/upload');
    assert.equal(modWrites.length, 2);
    assert.equal(modWrites[1].options.body.get('path'), '');
    assert.equal(modWrites[1].options.body.get('replace_revision'), 'mod-revision');
    assert.match(modUpload.confirmations[0], /Replace mods\/Demo.JAR/);
    assert.match(modUpload.document.getElementById('plugins-message').textContent, /Mod JARs require a restart/);
    const modViewer = await setup({addonKind: 'mod', manage: false});
    assert.equal(modViewer.row('Demo.JAR').children[2].children.length, 0);

    // Both the syntax-enhanced path and missing-asset fallback must fix native EOF focus.
    for (const highlight of [false, true]) {
        const longContent = '# first line\r\n' + 'key: true\r\n'.repeat(500) + 'last: value\r\n';
        const opening = await setup({highlight, fileResponses: [
            {data: {path: 'config.yml', content: longContent, revision: 'long-revision'}},
        ]});
        opening.editor.scrollTop = 7000; opening.editor.scrollLeft = 300;
        opening.row('config.yml').children[0].children[0].click(); await settle();
        const atTop = () => assert.deepEqual(
            [opening.editor.selectionStart, opening.editor.selectionEnd, opening.editor.scrollTop, opening.editor.scrollLeft],
            [0, 0, 0, 0]);
        atTop();
        assert.equal(opening.editor.value, longContent.replace(/\r\n/g, '\n'));
        assert.deepEqual(opening.focusCalls.at(-1), {preventScroll: true, disabled: false});
        if (highlight) {
            assert.equal(opening.syntaxCalls.find(call => call.filename).filename, 'config.yml');
            assert.equal(opening.syntaxCalls.find(call => call.filename).value, opening.editor.value);
            assert.deepEqual(opening.syntaxCalls.at(-1), {scrollTop: 0, scrollLeft: 0});
        }
        opening.editor.setSelectionRange(30, 40);
        opening.editor.scrollTop = 4000; opening.editor.scrollLeft = 70;
        opening.document.getElementById('reload-config').click(); await settle();
        atTop();
        opening.editor.setSelectionRange(4, 8);
        opening.editor.scrollTop = 1000; opening.editor.scrollLeft = 90;
        opening.row('second.json').children[0].children[0].click(); await settle();
        atTop();
        if (highlight) assert.equal(opening.syntaxCalls.filter(call => call.filename).at(-1).filename, 'second.json');
        opening.editor.scrollTop = 400; opening.editor.scrollLeft = 20;
        opening.row('Demo').children[0].children[0].click(); await settle();
        atTop();
        assert.equal(opening.editor.value, '');
        if (highlight) assert.equal(opening.syntaxCalls.filter(call => 'filename' in call).at(-1).filename, '');
    }

    const retained = await setup({highlight: true});
    retained.row('config.yml').children[0].children[0].click(); await settle();
    retained.editor.value = 'draft: retain this\n'; retained.editor.fire('input');
    retained.editor.setSelectionRange(3, 8); retained.editor.scrollTop = 420; retained.editor.scrollLeft = 51;
    const view = () => [retained.editor.selectionStart, retained.editor.selectionEnd,
        retained.editor.scrollTop, retained.editor.scrollLeft];
    const expectedView = view();
    const initialSyntax = retained.syntaxCalls.length, initialFocus = retained.focusCalls.length;
    retained.confirmAnswers.push(false);
    retained.document.getElementById('reload-config').click(); await settle();
    assert.deepEqual(view(), expectedView);
    assert.equal(retained.editor.value, 'draft: retain this\n');
    retained.saveResponses.push({status: 409, data: {error: 'Config changed. Reload it.'}});
    retained.document.getElementById('save-config').click(); await settle();
    assert.deepEqual(view(), expectedView);
    assert.equal(retained.document.getElementById('editor-state').textContent, 'Unsaved changes');
    retained.document.getElementById('save-config').click(); await settle();
    assert.deepEqual(view(), expectedView);
    assert.equal(retained.document.getElementById('editor-state').textContent, 'Saved');
    retained.fileResponses.push({status: 503, data: {error: 'Config unavailable.'}});
    retained.document.getElementById('reload-config').click(); await settle();
    assert.deepEqual(view(), expectedView);
    assert.equal(retained.syntaxCalls.length, initialSyntax);
    assert.equal(retained.focusCalls.length, initialFocus);
    assert.equal(retained.editor.value, 'draft: retain this\n');
    assert.match(retained.document.getElementById('plugins-message').textContent, /Config unavailable/);

    console.log('Plugin/mod UI checks passed (dynamic roots, uploads, permissions, search, deletion, top-of-file open/reload, syntax integration/fallback, and retained save/error/cancel positions).');
})().catch(error => { console.error(error); process.exitCode = 1; });
