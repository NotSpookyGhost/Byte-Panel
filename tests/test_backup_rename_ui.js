'use strict';
// Run the real modal and rename functions. Missing DOM nodes stay missing, so a
// misplaced showModal argument cannot be hidden by an auto-creating DOM double.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../app/templates/backups.html'), 'utf8');
const begin = template.indexOf('function showModal(');
const end = template.indexOf('async function createManualBackup(', begin);
assert(begin >= 0 && end > begin, 'Real backup modal and rename functions must be present');
const source = template.slice(begin, end);
const escapeBegin = template.indexOf('function escapeBackupLabel(');
const escapeEnd = template.indexOf('function renderServerSection(', escapeBegin);
assert(escapeBegin >= 0 && escapeEnd > escapeBegin, 'Real backup label escaping must be present');
const escapeSource = template.slice(escapeBegin, escapeEnd);

class Element {
    constructor(tagName, ownerDocument) {
        this.tagName = tagName.toLowerCase();
        this.ownerDocument = ownerDocument;
        this.parentNode = null;
        this.children = [];
        this.attributes = {};
        this.listeners = {};
        this.style = {};
        this.className = '';
        this.value = '';
        this.selectionStart = this.selectionEnd = 0;
        this._text = '';
        this._html = '';
    }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    remove() {
        if (this.parentNode) {
            this.parentNode.children = this.parentNode.children.filter(child => child !== this);
            this.parentNode = null;
        }
        if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'class') this.className = String(value);
        if (name === 'value') this.value = String(value);
    }
    getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
    set innerHTML(html) {
        this._html = String(html);
        this._text = '';
        this.children = [];
        const stack = [this];
        const tokens = this._html.match(/<[^>]*>|[^<]+/g) || [];
        for (const token of tokens) {
            const closing = token.match(/^<\/([a-z][\w-]*)\s*>$/i);
            if (closing) {
                if (stack.length > 1 && stack.at(-1).tagName === closing[1].toLowerCase()) stack.pop();
                continue;
            }
            const opening = token.match(/^<([a-z][\w-]*)([\s\S]*?)\/?>$/i);
            if (opening) {
                const child = new Element(opening[1], this.ownerDocument);
                const attrs = opening[2].matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g);
                for (const attr of attrs) child.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
                stack.at(-1).appendChild(child);
                if (!['input', 'img', 'br', 'hr', 'meta', 'link'].includes(child.tagName) && !token.endsWith('/>')) stack.push(child);
            } else {
                const child = new Element('#text', this.ownerDocument);
                child._text = token;
                stack.at(-1).appendChild(child);
            }
        }
    }
    get innerHTML() { return this._html; }
    set textContent(text) { this._text = String(text); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    matches(selector) {
        if (selector.startsWith('#')) return this.getAttribute('id') === selector.slice(1);
        if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
        return this.tagName === selector.toLowerCase();
    }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    removeEventListener(type, callback) {
        this.listeners[type] = (this.listeners[type] || []).filter(item => item !== callback);
    }
    fire(type, detail = {}) {
        const event = {type, target: this, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; }, ...detail};
        for (const callback of this.listeners[type] || []) callback(event);
        if (typeof this['on' + type] === 'function') this['on' + type](event);
        return event;
    }
    click() { this.fire('click'); }
    focus() { this.ownerDocument.activeElement = this; }
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
}

function createDocument() {
    const document = {activeElement: null};
    document.createElement = tag => new Element(tag, document);
    document.body = document.createElement('body');
    document.getElementById = id => document.body.querySelector('#' + id);
    document.querySelector = selector => document.body.querySelector(selector);
    document.querySelectorAll = selector => document.body.querySelectorAll(selector);
    return document;
}

class TestFormData {
    constructor() { this.values = new Map(); }
    append(key, value) { this.values.set(key, String(value)); }
    get(key) { return this.values.get(key) ?? null; }
}

function harness({reply = {status: 'success'}, ok = true, networkError = false, jsonError = false,
                  helperSource = source} = {}) {
    const document = createDocument();
    const requests = [];
    let reloads = 0;
    const context = {
        document, FormData: TestFormData,
        setTimeout: callback => { callback(); return 1; }, clearTimeout() {},
        fetchBackups: async () => { reloads += 1; },
        fetch: async (url, options) => {
            requests.push({url, options});
            if (networkError) throw new Error('network unavailable');
            return {ok, status: ok ? 200 : 409, json: async () => {
                if (jsonError) throw new SyntaxError('Invalid JSON');
                return reply;
            }};
        }
    };
    vm.runInNewContext(escapeSource + '\n' + helperSource, context, {filename: 'backups-template.js'});
    return {context, document, requests, get reloads() { return reloads; }};
}

const overlays = env => env.document.querySelectorAll('.modal-overlay');
function currentModal(env) {
    const modal = overlays(env).at(-1);
    assert(modal, 'Expected a real, still-connected modal');
    return modal;
}
function required(modal, selector) {
    const node = modal.querySelector(selector);
    assert(node, 'Missing actual modal node: ' + selector);
    return node;
}
function openRename(env, oldName = 'Before Chunky.zip') {
    const pending = env.context.renameBackup('QA_Realm', oldName);
    // Keep a malformed implementation from producing an unrelated unhandled
    // rejection before the assertion/timeout reports the actual regression.
    pending.catch(() => {});
    return {pending, modal: currentModal(env)};
}
async function flush() { for (let index = 0; index < 12; index += 1) await Promise.resolve(); }
async function completed(promise) {
    let timer;
    try {
        return await Promise.race([promise, new Promise((resolve, reject) => {
            timer = setTimeout(() => reject(new Error('Modal operation never completed')), 1000);
        })]);
    } finally { clearTimeout(timer); }
}

(async () => {
    // The strict harness actually detects the original wrong-argument defect:
    // customButtons=true suppresses Cancel, then null.onclick throws.
    const brokenSource = source.replace('false, false, false, null, true, oldBaseName',
                                        'false, false, false, true, oldBaseName');
    assert.notEqual(brokenSource, source, 'Mutation must reproduce the old seven-argument call');
    const broken = harness({helperSource: brokenSource});
    assert.equal(broken.document.getElementById('never-created'), null);
    await assert.rejects(broken.context.renameBackup('QA_Realm', 'Before Chunky.zip'), /null/);
    assert.equal(currentModal(broken).querySelector('#modal-btn-no'), null);

    // Real rename opens with both actionable buttons and a selected, focused name.
    const save = harness();
    const opened = openRename(save, 'Before.Chunky.ZIP');
    const input = required(opened.modal, '#modal-input-field');
    assert.equal(input.value, 'Before.Chunky');
    assert.equal(save.document.activeElement, input);
    assert.equal(input.selectionStart, 0);
    assert.equal(input.selectionEnd, input.value.length);
    assert.equal(required(opened.modal, '#modal-btn-yes').textContent.trim(), 'Save');
    assert.equal(required(opened.modal, '#modal-btn-no').textContent.trim(), 'Cancel');
    input.value = '  After Chunky  ';
    required(opened.modal, '#modal-btn-yes').click();
    await completed(opened.pending);
    assert.equal(save.requests.length, 1);
    assert.equal(save.requests[0].url, '/api/backup/rename');
    assert.equal(save.requests[0].options.method, 'POST');
    assert.equal(save.requests[0].options.body.get('server_name'), 'QA_Realm');
    assert.equal(save.requests[0].options.body.get('old_name'), 'Before.Chunky.ZIP');
    assert.equal(save.requests[0].options.body.get('new_name'), 'After Chunky');
    assert.equal(save.reloads, 1);
    assert.equal(overlays(save).length, 0);

    const enter = harness();
    const entering = openRename(enter);
    const enterInput = required(entering.modal, '#modal-input-field');
    enterInput.value = 'Keyboard backup';
    assert.equal(enterInput.fire('keydown', {key: 'Enter'}).defaultPrevented, true);
    await completed(entering.pending);
    assert.equal(enter.requests[0].options.body.get('new_name'), 'Keyboard backup');
    assert.equal(overlays(enter).length, 0);

    for (const cancelWithKey of [false, true]) {
        const cancel = harness();
        const editing = openRename(cancel);
        const field = required(editing.modal, '#modal-input-field');
        field.value = 'Never submit';
        if (cancelWithKey) assert.equal(field.fire('keydown', {key: 'Escape'}).defaultPrevented, true);
        else required(editing.modal, '#modal-btn-no').click();
        await completed(editing.pending);
        assert.equal(cancel.requests.length, 0);
        assert.equal(overlays(cancel).length, 0);
    }

    const composition = harness();
    const composing = openRename(composition);
    const composeInput = required(composing.modal, '#modal-input-field');
    composeInput.value = 'Still composing';
    assert.equal(composeInput.fire('keydown', {key: 'Enter', isComposing: true}).defaultPrevented, false);
    await flush();
    assert.equal(composition.requests.length, 0);
    assert.equal(overlays(composition).length, 1);
    composeInput.fire('keydown', {key: 'Escape'});
    await completed(composing.pending);

    for (const value of ['', '   ', 'Original', ' Original ', 'Original.ZIP']) {
        const unchanged = harness();
        const editing = openRename(unchanged, 'Original.ZIP');
        required(editing.modal, '#modal-input-field').value = value;
        required(editing.modal, '#modal-btn-yes').click();
        await completed(editing.pending);
        assert.equal(unchanged.requests.length, 0, 'Blank/unchanged name must not POST: ' + JSON.stringify(value));
        assert.equal(unchanged.reloads, 0);
        assert.equal(overlays(unchanged).length, 0);
    }

    // A filename is assigned as an input value, never interpolated into HTML.
    const untrusted = harness();
    const base = 'Archive" autofocus onfocus="evil()"><img src=x onerror=evil()> & notes.zip';
    const inert = openRename(untrusted, base + '.ZIP');
    const inertInput = required(inert.modal, '#modal-input-field');
    assert.equal(inertInput.value, base, 'Strip only the final, case-insensitive ZIP suffix');
    assert.equal(inertInput.getAttribute('value'), null);
    assert.equal(inertInput.getAttribute('onfocus'), null);
    assert.equal(inert.modal.querySelector('img'), null);
    required(inert.modal, '#modal-btn-no').click();
    await completed(inert.pending);

    // Simultaneous overlays with repeated IDs must not steal one another's buttons.
    const scoped = harness();
    const firstPending = scoped.context.showModal('First dialog', 'Leave this open', true);
    const first = currentModal(scoped);
    const second = openRename(scoped);
    required(second.modal, '#modal-input-field').value = 'Scoped backup';
    required(second.modal, '#modal-btn-yes').click();
    await completed(second.pending);
    assert.equal(overlays(scoped).length, 1);
    assert.equal(overlays(scoped)[0], first);
    required(first, '#modal-btn-no').click();
    assert.equal(await completed(firstPending), false);
    assert.equal(overlays(scoped).length, 0);

    for (const scenario of [
        {reply: {status: 'error', message: 'A backup with that name already exists'}, expected: 'already exists'},
        {ok: false, reply: {status: 'success', message: 'Rename not permitted'}, expected: 'not permitted'},
        {networkError: true, expected: 'Unable to confirm'},
        {jsonError: true, expected: 'Unable to confirm'}
    ]) {
        const failure = harness(scenario);
        const renaming = openRename(failure);
        required(renaming.modal, '#modal-input-field').value = 'Duplicate or unavailable';
        required(renaming.modal, '#modal-btn-yes').click();
        await flush();
        const errorModal = currentModal(failure);
        assert.equal(required(errorModal, '#modal-title-text').textContent, 'Error');
        const errorText = required(errorModal, '#modal-msg-text').textContent;
        assert(errorText.includes(scenario.expected), JSON.stringify({scenario, errorText}));
        assert.equal(failure.reloads, 0);
        required(errorModal, '#modal-btn-yes').click();
        await completed(renaming.pending);
        assert.equal(overlays(failure).length, 0, 'Error dialog must remain closable');
        assert.equal(failure.requests.length, 1, 'A failed rename must not be retried silently');
    }

    // Shared progress-modal controls also remain scoped and closable after the fix.
    const progress = harness();
    const progressHandle = await progress.context.showModal('Backup', 'Working', false, true);
    const progressModal = currentModal(progress);
    progressHandle.error('Disk is full');
    assert.equal(required(progressModal, '#modal-msg-text').textContent, 'Disk is full');
    required(progressModal, '#modal-btn-err').click();
    assert.equal(overlays(progress).length, 0);

    console.log('Backup rename modal checks passed (original defect, prefill, safe values, Save/Enter, Cancel/Escape, no-op names, scoped controls and closable errors).');
})().catch(error => { console.error(error); process.exitCode = 1; });
