'use strict';
const assert = require('node:assert/strict');
const {findMatches, nextIndex, attach} = require('../app/static/plugin_search.js');
const {createDocument} = require('./plugin_dom.js');

assert.deepEqual(findMatches('anything', ''), []);
assert.deepEqual(findMatches('Key key KEY', 'key'), [{start: 0, end: 3}, {start: 4, end: 7}, {start: 8, end: 11}]);
assert.deepEqual(findMatches('Key key KEY', 'key', true), [{start: 4, end: 7}]);
for (const query of ['[world]', '.', '*', '\\', '()', '^$', '{key}', 'a+b?', '|']) {
    assert.deepEqual(findMatches('prefix ' + query + ' suffix', query), [{start: 7, end: 7 + query.length}]);
}
assert.deepEqual(findMatches('İ abc 😀abc', 'abc'), [{start: 2, end: 5}, {start: 8, end: 11}]);
assert.deepEqual(findMatches('😀 value 😀', '😀'), [{start: 0, end: 2}, {start: 9, end: 11}]);
assert.deepEqual(findMatches('abc', 'missing'), []);
assert.equal(findMatches('x'.repeat(1024 * 1024 - 6) + 'needle', 'needle')[0].start, 1024 * 1024 - 6);
const matches = findMatches('one one one', 'one');
assert.equal(nextIndex(matches, -1, 1, 0, 0), 0);
assert.equal(nextIndex(matches, 0, 1, 0, 3), 1);
assert.equal(nextIndex(matches, 2, 1, 8, 11), 0);
assert.equal(nextIndex(matches, 0, -1, 0, 3), 2);
assert.equal(nextIndex(matches, -1, -1, 7, 7), 1);
assert.equal(nextIndex(matches, 0, 1, 7, 7), 2); // Honor manually moved caret.
assert.equal(nextIndex([], -1, 1, 0, 0), -1);

const document = createDocument();
const editor = document.getElementById('plugin-content');
const controls = Object.fromEntries(['input', 'previous', 'next', 'matchCase', 'status'].map(name => [name, document.getElementById(name)]));
const search = attach(editor, controls);
assert.equal(controls.input.disabled, true);
editor.value = 'key: yes\n' + 'line\n'.repeat(10000) + '\t'.repeat(100) + 'KEY: no';
const original = editor.value;
search.setEnabled(true);
controls.input.value = 'key';
controls.input.fire('input');
assert.equal(controls.status.textContent, '2 matches');
controls.next.click();
assert.equal(editor.selectionStart, 0);
assert.equal(controls.status.textContent, '1 of 2');
controls.next.click();
assert.equal(editor.selectionStart, editor.value.indexOf('KEY'));
assert.ok(editor.scrollTop > 100000);
assert.ok(editor.scrollLeft > 1000);
assert.equal(document.activeElement, editor);
assert.equal(editor.value, original); // Searching never changes config contents.
controls.next.click();
assert.equal(editor.selectionStart, 0); // Wrap forward.
controls.previous.click();
assert.equal(controls.status.textContent, '2 of 2'); // Wrap backward.
editor.fire('keydown', {key: 'F3', shiftKey: true});
assert.equal(controls.status.textContent, '1 of 2');
assert.equal(editor.fire('keydown', {key: 'f', ctrlKey: true}).prevented, true);
assert.equal(document.activeElement, controls.input);
controls.input.fire('keydown', {key: 'Enter'});
assert.equal(controls.status.textContent, '2 of 2');
editor.fire('keydown', {key: 'f', metaKey: true});
assert.equal(document.activeElement, controls.input);
controls.input.fire('keydown', {key: 'Escape'});
assert.equal(document.activeElement, editor);
controls.matchCase.checked = true;
controls.matchCase.fire('change');
assert.equal(controls.status.textContent, '1 match');
editor.value = 'changed in editor';
editor.fire('input');
assert.equal(controls.status.textContent, 'No matches');
assert.equal(controls.next.disabled, true);
editor.readOnly = true;
editor.value = 'key';
search.refresh();
controls.next.click();
assert.equal(editor.selectionEnd, 3); // View-only users can search.
assert.equal(editor.readOnly, true);
search.setEnabled(false);
assert.equal(controls.input.disabled, true);
assert.equal(controls.previous.disabled, true);
console.log('Plugin search checks passed (literal/Unicode, navigation, shortcuts, long files, read-only).');
