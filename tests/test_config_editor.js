'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const editor = require('../app/static/config_editor.js');
const textOf = result => result.tokens.map(token => token.text).join('');
const typed = (result, type) => result.tokens.filter(token => token.type === type).map(token => token.text).join('');

const cases = [
    ['config.yml', '# comment\r\nserver-name: "My Server"\r\nallow-flight: false\r\nport: 25565\r\nlist:\r\n  - name: \'It\'\'s fine\'\r\n'],
    ['config.yaml', 'defaults: &defaults\n  timeout: 5\ncopy: *defaults\nvalue: !!str "5"\n'],
    ['settings.json', '{"name": "<img src=x onerror=alert(1)>", "enabled": true, "count": -1.5e+2, "other": null}\n'],
    ['settings.json5', "// comment\n{unquoted: 'value', number: 0x10, array: [1, 2,], /* block */}\n"],
    ['server.properties', '# comment\n! another comment\nserver-port=25565\nwhite-list=true\nmotd=Hello World\nescaped\\ key=value\n'],
    ['loader.toml', '# TOML\n[[plugins]]\nname = "Example"\nvalue = 1_024\nactive = true\ntext = """first\nsecond"""\n'],
    ['settings.ini', '; comment\n[section]\nkey = value\ncount: 5\n'],
    ['settings.cfg', '[group]\nkey="value"\n'],
    ['settings.conf', '# comment\nkey = yes\n// another comment\n'],
    ['settings.xml', '<?xml version="1.0"?>\n<!-- comment -->\n<root key="value"><name>A &amp; B</name><![CDATA[<raw>]]></root>\n']
];
for (const [filename, source] of cases) {
    const result = editor.tokenize(source, filename);
    assert.equal(result.highlighted, true, filename);
    assert.equal(textOf(result), source, filename + ' must preserve every source character');
    assert.ok(result.tokens.some(token => token.type), filename + ' should receive syntax colors');
    assert.ok(result.tokens.length <= editor.limits.maxTokens);
}
assert.equal(editor.languageFor('plugins\\Example\\CONFIG.YAML'), 'yaml');
assert.equal(editor.languageFor('server.properties'), 'properties');
assert.equal(editor.languageFor('Dockerfile'), 'plain');
assert.equal(editor.languageFor('readme.md'), 'plain');
assert.equal(editor.languageFor('archive.json.bak'), 'plain');

const json = editor.tokenize('{"url": "https://example.com/#x", "html": "<script>x()</script>", "enabled": false}', 'config.json');
assert.equal(typed(json, 'key'), '"url""html""enabled"');
assert.ok(typed(json, 'string').includes('https://example.com/#x'));
assert.equal(typed(json, 'literal'), 'false');
assert.equal(typed(json, 'comment'), '');
assert.ok(typed(editor.tokenize('value: "x#y" # real comment', 'config.yaml'), 'comment').includes('# real comment'));
assert.equal(typed(editor.tokenize('motd=hello#world', 'server.properties'), 'comment'), '');
assert.equal(typed(editor.tokenize("name: 'a''b'", 'config.yaml'), 'string'), "'a''b'");
const escapedQuoted = '"escaped ' + String.fromCharCode(92) + '" quote"';
assert.equal(typed(editor.tokenize('value=' + escapedQuoted, 'config.toml'), 'string'), escapedQuoted);
assert.equal(typed(editor.tokenize('<root count="5">Hi</root>', 'config.xml'), 'tag'), 'rootroot');
assert.equal(typed(editor.tokenize('<root count="5">Hi</root>', 'config.xml'), 'attribute'), 'count');

// Unterminated input and Unicode remain editable source, never validation failures.
for (const [filename, source] of [
    ['config.yaml', 'key: "unfinished\nnext: value'],
    ['config.json5', '/* unfinished\n'],
    ['config.xml', '<!-- unfinished'],
    ['config.toml', 'key = """unfinished\n'],
    ['config.yaml', '\ufeffmessage: "こんにちは 👋"\r\nname: café\rtabs:\ttrue\n'],
    ['config.xml', '<script>alert("untrusted")</script><img onerror="oops"/>'],
    ['config.conf', 'x = </span><script>alert(1)</script>&lt;img&gt;']
]) assert.equal(textOf(editor.tokenize(source, filename)), source);

const plain = editor.tokenize('<img src=x onerror=alert(1)>', 'notes.txt');
assert.equal(plain.highlighted, false);
assert.equal(plain.reason, 'plain-text');
assert.equal(textOf(plain), '<img src=x onerror=alert(1)>');
const huge = 'k: value\n'.repeat(Math.ceil(editor.limits.maxLength / 9) + 1);
assert.equal(editor.tokenize(huge, 'config.yml').reason, 'large-file');
const unicodeHuge = ('key: "' + '界'.repeat(1000) + '"\n').repeat(360);
assert.equal(editor.tokenize(unicodeHuge, 'config.yml').reason, 'large-file');
const longLine = 'key: ' + 'x'.repeat(editor.limits.maxLine);
assert.equal(editor.tokenize(longLine, 'config.yaml').reason, 'long-line');
const dense = '[true,false,1,2]\n'.repeat(3000);
const denseResult = editor.tokenize(dense, 'config.json');
assert.equal(denseResult.reason, 'token-limit');
assert.equal(denseResult.tokens.length, 1);
assert.equal(textOf(denseResult), dense);
const normalLongConfig = 'some-setting: true # explanatory comment\n'.repeat(2000);
assert.equal(editor.tokenize(normalLongConfig, 'config.yml').highlighted, true);

// Seeded fuzz input exercises progress/termination and exact preservation.
let random = 1234567;
const alphabet = 'ab09_{}[]:=#;"\'/<>&\\ \t\r\n\x00界';
for (const [filename] of cases) {
    for (let sample = 0; sample < 40; sample++) {
        let source = '';
        for (let index = 0; index < 160; index++) {
            random = (Math.imul(random, 1664525) + 1013904223) >>> 0;
            source += alphabet[random % alphabet.length];
        }
        assert.equal(textOf(editor.tokenize(source, filename)), source, filename + ' fuzz preservation');
    }
}

// Purpose-built DOM double: innerHTML is forbidden and native textarea writes are counted.
class Node {
    constructor(tagName, document) {
        this.tagName = tagName;
        this.ownerDocument = document;
        this.children = [];
        this.parentNode = null;
        this.className = '';
        this.dataset = {};
        this.attributes = {};
        this.listeners = {};
        this.style = {setProperty(key, value) { this[key] = value; }};
        this.scrollTop = this.scrollLeft = 0;
        this.offsetTop = 8; this.offsetLeft = 0; this.clientTop = this.clientLeft = 1;
        this.clientWidth = 600; this.clientHeight = 390;
        this.selectionStart = this.selectionEnd = 0;
        this._value = ''; this.valueWrites = 0;
        this.readOnly = false; this.disabled = false;
        this.classList = {
            add: value => { if (!this.hasClass(value)) this.className += ' ' + value; },
            toggle: (value, enabled) => {
                this.className = this.className.split(/\s+/).filter(token => token && token !== value).join(' ');
                if (enabled) this.className += ' ' + value;
            }
        };
    }
    hasClass(value) { return this.className.split(/\s+/).includes(value); }
    set innerHTML(value) { throw new Error('Untrusted HTML must never be used'); }
    set value(value) { this._value = value; this.valueWrites++; }
    get value() { return this._value; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
    setAttribute(key, value) { this.attributes[key] = value; }
    appendChild(node) {
        if (node.tagName === '#fragment') {
            for (const child of [...node.children]) this.appendChild(child);
            return node;
        }
        if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(child => child !== node);
        node.parentNode = this;
        this.children.push(node);
        return node;
    }
    insertBefore(node, reference) {
        const index = this.children.indexOf(reference);
        assert.notEqual(index, -1);
        node.parentNode = this;
        this.children.splice(index, 0, node);
    }
    replaceChildren(...nodes) {
        this.text = '';
        for (const child of this.children) child.parentNode = null;
        this.children = [];
        for (const node of nodes) this.appendChild(node);
    }
    addEventListener(type, listener, options) { (this.listeners[type] ||= []).push({listener, options}); }
    fire(type) { for (const {listener} of this.listeners[type] || []) listener({target: this}); }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    focus(options) { this.ownerDocument.activeElement = this; this.focusOptions = options; }
}
function fixture(source = 'key: true\n') {
    const timers = new Map();
    const observers = [];
    let timerId = 0;
    const document = {
        activeElement: null,
        createdTags: [],
        createElement(tag) { this.createdTags.push(tag); return new Node(tag, document); },
        createDocumentFragment: () => new Node('#fragment', document),
        createTextNode: text => { const node = new Node('#text', document); node.textContent = text; return node; },
    };
    const view = document.defaultView = {
        getComputedStyle: () => ({
            color: '#eee', backgroundColor: '#111', fontFamily: 'Consolas, monospace',
            fontSize: '14px', lineHeight: '22.4px', fontWeight: '400', tabSize: '4',
            paddingTop: '10px', paddingRight: '12px', paddingBottom: '10px', paddingLeft: '12px'
        }),
        setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
        clearTimeout(id) { timers.delete(id); },
        ResizeObserver: class {
            constructor(callback) { this.callback = callback; observers.push(this); }
            observe(target) { this.target = target; }
        }
    };
    const parent = document.createElement('section');
    const textarea = document.createElement('textarea');
    textarea.setAttribute('id', 'config-source');
    textarea.setAttribute('aria-label', 'Config contents');
    textarea.value = source;
    parent.appendChild(textarea);
    const api = editor.attach(textarea);
    const wrapper = parent.children[0];
    const overlay = wrapper.children.find(node => node.tagName === 'pre');
    const code = overlay.children[0];
    const status = wrapper.children.find(node => node.hasClass('panel-config-editor-status'));
    function flush() {
        for (const [id, callback] of [...timers]) { timers.delete(id); callback(); }
    }
    return {document, view, parent, textarea, wrapper, overlay, code, status, api, timers, observers, flush};
}

const f = fixture();
assert.equal(editor.attach(f.textarea), f.api, 'attach is idempotent');
assert.equal(f.parent.children.length, 1, 're-attach must not add another wrapper');
assert.equal(f.textarea.attributes.id, 'config-source');
assert.equal(f.textarea.attributes['aria-label'], 'Config contents');
assert.equal(f.overlay.attributes['aria-hidden'], 'true');
assert.equal(f.overlay.attributes.inert, '');
assert.equal(f.code.attributes['aria-hidden'], 'true');
assert.equal(f.textarea.wrap, 'off');
assert.equal(f.wrapper.dataset.highlighted, 'false');
assert.equal(f.status.hidden, true, 'no fallback notice when no file is selected');
f.textarea.selectionStart = 5; f.textarea.selectionEnd = 9;
f.textarea.scrollTop = 500; f.textarea.scrollLeft = 80;
const writes = f.textarea.valueWrites;
f.api.setFile('config.yaml');
assert.equal(f.wrapper.dataset.language, 'yaml');
assert.equal(f.wrapper.dataset.highlighted, 'true');
assert.equal(f.code.textContent, f.textarea.value);
assert.equal(f.textarea.selectionStart, 0); assert.equal(f.textarea.selectionEnd, 0);
assert.equal(f.textarea.scrollTop, 0); assert.equal(f.textarea.scrollLeft, 0);
assert.equal(f.overlay.scrollTop, 0); assert.equal(f.overlay.scrollLeft, 0);
assert.equal(f.textarea.valueWrites, writes, 'setFile must never rewrite value/undo history');
assert.equal(f.document.activeElement, null, 'setFile must not steal focus');
assert.equal(f.overlay.style.top, '9px');
assert.equal(f.overlay.style.left, '1px');
assert.equal(f.overlay.style.width, '600px');
assert.equal(f.overlay.style.height, '390px');
assert.equal(f.overlay.style.fontFamily, 'Consolas, monospace');
assert.equal(f.overlay.style.lineHeight, '22.4px');
assert.equal(f.overlay.style.paddingLeft, '12px');

f.textarea.selectionStart = 4; f.textarea.selectionEnd = 8;
f.textarea.scrollTop = 180; f.textarea.scrollLeft = 99;
f.textarea.fire('scroll');
assert.equal(f.overlay.scrollTop, 180); assert.equal(f.overlay.scrollLeft, 99);
f.api.refresh();
assert.equal(f.textarea.selectionStart, 4); assert.equal(f.textarea.selectionEnd, 8);
assert.equal(f.textarea.scrollTop, 180); assert.equal(f.overlay.scrollTop, 180);
f.api.resetPosition({focus: true});
assert.equal(f.document.activeElement, f.textarea);
assert.equal(f.textarea.focusOptions.preventScroll, true);
assert.equal(f.textarea.scrollTop, 0); assert.equal(f.textarea.scrollLeft, 0);
assert.equal(f.overlay.scrollTop, 0); assert.equal(f.overlay.scrollLeft, 0);
assert.equal(f.textarea.valueWrites, writes);
assert.equal(f.textarea.listeners.keydown, undefined, 'native editing/undo keys must not be intercepted');

f.textarea.readOnly = true; f.textarea.disabled = true;
f.api.setFile('config.json');
assert.equal(f.textarea.readOnly, true); assert.equal(f.textarea.disabled, true);
f.textarea.readOnly = false; f.textarea.disabled = false;
f.textarea.value = '{"safe": "<img src=x onerror=alert(1)>"}\n';
f.textarea.selectionStart = f.textarea.selectionEnd = 6;
f.textarea.fire('input');
assert.equal(f.wrapper.dataset.highlighted, 'false', 'native updated text must remain visible during debounce');
assert.equal(f.wrapper.dataset.fallback, 'editing');
assert.equal(f.timers.size, 1);
f.textarea.fire('input');
assert.equal(f.timers.size, 1, 'repeated typing must debounce');
f.flush();
assert.equal(f.wrapper.dataset.highlighted, 'true');
assert.equal(f.code.textContent, f.textarea.value);
assert.equal(f.textarea.selectionStart, 6);
assert.ok(f.document.createdTags.every(tag => ['section', 'textarea', 'div', 'pre', 'code', 'span'].includes(tag)));
assert.ok(f.code.children.some(node => node.tagName === 'span' && node.textContent.includes('<img')));

f.textarea.fire('compositionstart');
assert.equal(f.wrapper.dataset.highlighted, 'false');
f.textarea.value = '{"message":"日本語"}\n';
f.textarea.fire('input');
assert.equal(f.timers.size, 0, 'IME must remain native until composition finishes');
f.textarea.fire('compositionend');
assert.equal(f.timers.size, 1);
f.flush();
assert.equal(f.code.textContent, f.textarea.value);
assert.equal(f.wrapper.dataset.highlighted, 'true');

f.textarea.clientWidth = 320; f.textarea.clientHeight = 250;
f.observers[0].callback();
assert.equal(f.overlay.style.width, '320px');
assert.equal(f.overlay.style.height, '250px');
f.textarea.clientWidth = 0;
f.observers[0].callback();
assert.equal(f.wrapper.dataset.highlighted, 'false');
assert.equal(f.wrapper.dataset.fallback, 'hidden');
f.textarea.clientWidth = 320;
f.observers[0].callback();
assert.equal(f.wrapper.dataset.highlighted, 'true', 'showing a hidden editor must restore highlighting');
f.api.setFile('notes.txt');
assert.equal(f.wrapper.dataset.highlighted, 'false');
assert.equal(f.wrapper.dataset.fallback, 'plain-text');
assert.equal(f.code.children.length, 0, 'plain fallback must avoid duplicating file text');
assert.equal(f.status.hidden, false);
assert.match(f.status.textContent, /Plain-text editing/);
f.textarea.value = longLine;
f.api.setFile('config.yml');
assert.equal(f.wrapper.dataset.fallback, 'long-line');
assert.equal(f.textarea.value, longLine);
assert.match(f.status.textContent, /very long line/);
f.textarea.value = '';
f.api.setFile('');
assert.equal(f.status.hidden, true);
assert.equal(f.code.textContent, '');
assert.equal(f.textarea.selectionStart, 0);

const css = fs.readFileSync(path.join(__dirname, '../app/static/config_editor.css'), 'utf8');
assert.match(css, /@media\s*\(forced-colors:\s*active\)/);
assert.match(css, /color:\s*CanvasText\s*!important/);
assert.match(css, /caret-color:/);
assert.match(css, /::selection/);
assert.match(css, /font:\s*14px\/1\.6 ui-monospace,\s*Consolas,\s*monospace/);
assert.match(css, /\.theme-light .*config-token-key/);
assert.match(css, /\.obsidian \.panel-config-editor #plugin-content/);
assert.match(css, /pointer-events:\s*none/);
assert.match(css, /white-space:\s*pre/);
assert.ok(!/\bcontenteditable\b/.test(fs.readFileSync(path.join(__dirname, '../app/static/config_editor.js'), 'utf8')));
console.log('Config editor tests passed: tokenization, exact text/XSS, limits, native editing, reset, scroll, resize, IME and fallbacks.');
