'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/panel_shell.js'), 'utf8');
const label = {textContent:''};
const status = {dataset:{}, querySelector:() => label};
const dot = {dataset:{}, setAttribute(name, value) { this[name] = value; }};
const document = {readyState:'loading', addEventListener() {},
    querySelectorAll:selector => selector === '[data-server-status]' ? [status] : [dot]};
const context = {window:{}, document};
vm.runInNewContext(source, context);
const shell = context.window.BytePanelShell;
for (const [value, expected] of [['Online','online'],['Starting','transition'],['Restarting','transition'],
    ['Shutting Down','transition'],['Stopping','transition'],['Offline','offline'],
    ['Unexpected','unknown'],[undefined,'unknown']]) {
    assert.equal(shell.stateFor(value), expected);
    shell.renderStatus(value);
    assert.equal(status.dataset.state, expected);
    assert.equal(dot.dataset.state, expected);
}
assert.equal(label.textContent, 'Status unavailable');
assert(!source.includes("setInterval"), 'Polls must not overlap');
console.log('Shared shell status checks passed (real state mapping, unknown fallback, no accent-derived status).');

