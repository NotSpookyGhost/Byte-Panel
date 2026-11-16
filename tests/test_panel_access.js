'use strict';
// Browser API contract checks, with no external services or DOM dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const events = {};
const calls = [];
class Form {
    constructor(method = 'post', action = '/settings') { this.method = method; this.action = action; this.fields = []; }
    querySelector() { return this.fields.find(field => field.name === '_csrf'); }
    appendChild(field) { this.fields.push(field); }
    submit() { this.submitted = true; }
}
class XHR {
    constructor() { this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(key, value) { this.headers[key] = value; }
}
const document = {
    querySelector: () => ({content: 'csrf-test'}),
    getElementById: () => ({textContent: '["console.view"]'}),
    addEventListener: (name, callback) => { events[name] = callback; },
    createElement: () => ({}),
};
const window = {fetch: async (...args) => { calls.push(args); return {}; }};
const context = {window, document, location: {href: 'https://panel.example.com/settings', origin: 'https://panel.example.com'},
    URL, Headers, Request, XMLHttpRequest: XHR, HTMLFormElement: Form};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/panel_access.js'), 'utf8'), context);
(async () => {
    assert.equal(window.panelCan('console.view'), true);
    assert.equal(window.panelCan('console.command'), false);
    await window.fetch('/api/users', {method: 'POST', headers: {'Content-Type': 'application/json'}});
    assert.equal(calls.at(-1)[1].headers.get('X-Panel-CSRF'), 'csrf-test');
    assert.equal(calls.at(-1)[1].headers.get('Content-Type'), 'application/json');
    await window.fetch('https://external.example.com/', {method: 'POST'});
    assert.equal(calls.at(-1)[1].headers, undefined);
    await window.fetch('/api/users');
    assert.equal(calls.at(-1)[1].headers, undefined);
    await window.fetch(new Request('https://panel.example.com/api/users', {method: 'PUT'}));
    assert.equal(calls.at(-1)[1].headers.get('X-Panel-CSRF'), 'csrf-test');
    const local = new XHR(); local.open('POST', '/api/import', true);
    assert.equal(local.headers['X-Panel-CSRF'], 'csrf-test');
    const external = new XHR(); external.open('POST', 'https://external.example.com', true);
    assert.equal(external.headers['X-Panel-CSRF'], undefined);
    const form = new Form(); form.submit();
    assert.equal(form.fields[0].value, 'csrf-test');
    assert.equal(form.submitted, true);
    events.submit({target: form});
    assert.equal(form.fields.length, 1);
    const externalForm = new Form('post', 'https://external.example.com'); externalForm.submit();
    assert.equal(externalForm.fields.length, 0);
    const getForm = new Form('get'); getForm.submit();
    assert.equal(getForm.fields.length, 0);
    console.log('Panel browser API checks passed (permissions, fetch, XHR, forms, cross-origin token isolation).');
})().catch(error => { console.error(error); process.exitCode = 1; });
