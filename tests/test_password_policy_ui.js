// Exercise the real browser validator, optionally using policy data rendered by Flask.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const rules = process.argv[2] ? JSON.parse(process.argv[2]).rules : [
    ['[A-Z]', 'an uppercase letter (A-Z)'],
    ['[0-9]', 'a number (0-9)'],
    ['[\\x21-\\x2f\\x3a-\\x40\\x5b-\\x60\\x7b-\\x7e]', 'a punctuation symbol (for example !, @ or #)']
];
const pattern = process.argv[2] ? JSON.parse(process.argv[2]).pattern
    : rules.map(([expression]) => '(?=[\\s\\S]*' + expression + ')').join('') + '[\\s\\S]{12,256}';
const nativePattern = new RegExp('^(?:' + pattern + ')$', 'v');
const source = fs.readFileSync('app/static/password_policy.js', 'utf8');
function field() {
    const listeners = {};
    return {
        value: '', error: '', listeners, dataset: {passwordRules: JSON.stringify(rules)},
        form: {listeners: {}, addEventListener(event, callback) { this.listeners[event] = callback; }},
        addEventListener(event, callback) { listeners[event] = callback; },
        setCustomValidity(error) { this.error = error; }
    };
}
const password = field();
const confirm = field();
const items = ['length', ...rules.map((_, index) => String(index))].map(key => {
    const icon = {textContent: '\u25cb'};
    const state = {textContent: 'Not yet met'};
    return {dataset: {passwordCheck: key, met: 'false'}, icon, state,
        querySelector: selector => selector === '.password-check-icon' ? icon : state};
});
password.dataset.passwordChecklist = 'password-requirements';
const checklist = {querySelectorAll: selector => {
    assert.equal(selector, '[data-password-check]');
    return items;
}};
vm.runInNewContext(source, {document: {querySelectorAll: selector => {
    assert.equal(selector, '[data-password-rules]');
    return [password, confirm];
}, getElementById: id => {
    assert.equal(id, 'password-requirements');
    return checklist;
}}});
const checklistState = () => items.map(item => item.dataset.met === 'true');
assert.deepEqual(checklistState(), [false, false, false, false]);
for (const [value, expected] of [
    ['abc', [false, false, false, false]],
    ['A', [false, true, false, false]],
    ['A1', [false, true, true, false]],
    ['A1!', [false, true, true, true]],
    ['Uppercase123!', [true, true, true, true]],
    ['Uppercase123', [true, true, true, false]],
    ['Uppercase12', [false, true, true, false]],
    ['A1!' + 'a'.repeat(254), [false, true, true, true]]
]) {
    password.value = value;
    password.listeners.input();
    assert.deepEqual(checklistState(), expected, value);
    items.forEach((item, index) => {
        assert.equal(item.icon.textContent, expected[index] ? '\u2713' : '\u25cb');
        assert.equal(item.state.textContent, expected[index] ? 'Met' : 'Not yet met');
        assert.ok(!item.state.textContent.includes(value));
    });
}
for (const [value, expected] of [
    ['lowercase123!', 'uppercase letter'],
    ['Uppercaseonly!', 'number'],
    ['Uppercase1234', 'punctuation symbol'],
    ['Uppercase123 ', 'punctuation symbol'],
    ['Uppercase123\u00e9', 'punctuation symbol'],
    ['A1!', 'between 12 and 256'],
    ['A1!' + 'a'.repeat(254), 'between 12 and 256']
]) {
    password.value = value;
    password.listeners.input();
    assert.ok(password.error.includes(expected), password.error);
    assert.equal(nativePattern.test(value), false, value);
    assert.ok(!password.error.includes(value), 'Do not echo a password in validation messages');
}
for (const value of ['UPPERCASE123!', 'Uppercase123!', 'A1!' + 'a'.repeat(253), 'Uppercase123! caf\u00e9']) {
    password.value = value;
    password.listeners.change();
    assert.equal(password.error, '');
    assert.equal(nativePattern.test(value), true);
}
password.value = 'onlylowercase';
password.listeners.invalid();
assert.match(password.error, /uppercase letter/);
assert.match(password.error, /number/);
assert.match(password.error, /punctuation symbol/);
password.form.listeners.reset();
assert.equal(password.error, '');
assert.deepEqual(checklistState(), [false, false, false, false]);
password.value = '';
password.listeners.input();
assert.equal(password.error, '', 'Empty fields use native required validation');
confirm.value = 'Uppercase1234';
confirm.listeners.input();
assert.match(confirm.error, /punctuation symbol/);
assert.deepEqual(checklistState(), [false, false, false, false], 'Confirmation must not overwrite the new-password checklist');
console.log('Password policy UI checks passed (live checklist, shared rules, accessible states, deletion, boundaries and reset).');
