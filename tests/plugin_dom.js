/* Minimal DOM double for plugin behavior tests; not a browser/layout test. */
'use strict';
class Element {
    constructor(tag, document) {
        this.tagName = tag;
        this.ownerDocument = document;
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this.listeners = {};
        this.className = '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.selectionStart = this.selectionEnd = 0;
        this.clientHeight = 400;
        this.clientWidth = 500;
        this.classList = {
            add: value => { this.className += ' ' + value; },
            toggle: (value, active) => {
                this.className = this.className.split(' ').filter(item => item && item !== value).join(' ');
                if (active) this.classList.add(value);
            },
        };
    }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.text = ''; this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, action) { (this.listeners[name] ||= []).push(action); }
    fire(name, options = {}) {
        const event = {target: this, preventDefault() { this.prevented = true; }, ...options};
        for (const action of this.listeners[name] || []) action(event);
        return event;
    }
    click() { if (!this.disabled) this.fire('click'); }
    focus() { this.ownerDocument.activeElement = this; }
    select() { this.setSelectionRange(0, this.value.length); }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    querySelectorAll(selector) {
        const matches = element => selector.startsWith('.')
            ? element.className.split(' ').includes(selector.slice(1)) : element.tagName === selector;
        return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
}
function createDocument() {
    const elements = new Map();
    const document = {
        defaultView: {getComputedStyle: () => ({lineHeight: '22.4px', fontSize: '14px'})},
        createElement: tag => new Element(tag, document),
        createElementNS: (namespace, tag) => {
            const node = new Element(tag, document);
            node.namespace = namespace;
            return node;
        },
        createTextNode: text => { const node = new Element('#text', document); node.textContent = text; return node; },
        querySelector: selector => {
            for (const el of elements.values()) {
                const match = el.querySelectorAll(selector)[0];
                if (match) return match;
            }
            return null;
        },
        getElementById: id => {
            if (!elements.has(id)) elements.set(id, new Element(id === 'plugin-content' ? 'textarea' : 'div', document));
            return elements.get(id);
        },
    };
    return document;
}
module.exports = {createDocument};
