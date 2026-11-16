'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createDocument} = require('./plugin_dom.js');
const PluginConfigSearch = require('../app/static/plugin_search.js');
const source = fs.readFileSync(path.join(__dirname, '../app/static/plugins.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

async function setup({manage = true, confirm = true, failDelete = false} = {}) {
    const document = createDocument();
    const manager = document.getElementById('plugin-manager');
    manager.dataset.csrfToken = 'plugin-csrf-test';
    const ids = ['plugin-content', 'plugin-file-list', 'plugins-message', 'editor-state', 'save-config', 'reload-config',
        'config-search', 'search-previous', 'search-next', 'search-match-case', 'search-status', 'plugin-breadcrumbs',
        'plugin-list-status', 'editor-path', 'refresh-files', 'plugin-upload-form'];
    for (const id of ids) manager.append(document.getElementById(id));
    const calls = [];
    const confirmations = [];
    let removed = false;
    const entries = [
        {path: 'Demo', name: 'Demo', kind: 'folder', size: 0, editable: false},
        {path: 'Demo.JAR', name: 'Demo.JAR', kind: 'file', size: 1234, editable: false},
        {path: 'config.yml', name: 'config.yml', kind: 'file', size: 50, editable: true},
    ];
    const window = {
        PluginConfigSearch, panelCan: permission => permission === 'plugins.view' || manage,
        confirm: message => { confirmations.push(message); return confirm; },
        addEventListener() {},
    };
    async function fetch(url, options = {}) {
        calls.push({url, options});
        let data;
        let status = 200;
        if (url.startsWith('/api/plugins/files?')) {
            data = {path: '', entries: entries.filter(entry => !removed || entry.path !== 'Demo.JAR')};
        } else if (url.startsWith('/api/plugins/file?')) {
            data = {path: 'config.yml', content: 'key: true\nkey: false', revision: 'config-revision'};
        } else if (url.startsWith('/api/plugins/delete?')) {
            data = {path: 'Demo.JAR', name: 'Demo.JAR', revision: 'jar-revision'};
        } else if (url === '/api/plugins/delete' && options.method === 'DELETE') {
            if (failDelete) { status = 409; data = {error: 'This JAR changed. Confirm again.'}; }
            else { removed = true; data = {message: 'Plugin JAR deleted. Configs kept.', backup: 'safe-Demo.JAR.bak'}; }
        } else throw new Error('Unexpected request: ' + url);
        return {ok: status === 200, status, headers: {get: () => 'application/json'}, json: async () => data};
    }
    vm.runInNewContext(source, {document, window, fetch, console});
    await settle();
    const list = document.getElementById('plugin-file-list');
    const row = name => list.children.find(element => element.dataset.path === name);
    const deleteButton = () => row('Demo.JAR').children[2].children[0];
    return {document, calls, confirmations, row, deleteButton};
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
    console.log('Plugin UI checks passed (folder icons, JAR actions, confirmation/cancellation, permissions, editor preservation).');
})().catch(error => { console.error(error); process.exitCode = 1; });
