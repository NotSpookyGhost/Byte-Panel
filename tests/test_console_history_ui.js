'use strict';
// Exercise the real console history and submission functions with an in-memory
// input and fetch doubles. No live server commands or browser storage are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../app/templates/console.html'), 'utf8');
const begin = template.indexOf('const COMMAND_HISTORY_LIMIT');
const end = template.indexOf('function showToast(', begin);
assert(begin >= 0 && end > begin, 'Real console history block must be present');
const source = template.slice(begin, end);
assert(/onkeydown="handleCommandKeydown\(event\)"/.test(template), 'Input must use the real history key handler');
assert(/onclick="sendCommand\(\)"/.test(template), 'Send button must share command submission');
assert(!/\b(?:localStorage|sessionStorage)\b/.test(source), 'Commands must remain in this page memory only');

function harness({response = {status: 'success'}, networkError = false, deferred = false} = {}) {
    const requests = [];
    const toasts = [];
    const input = {
        value: '', disabled: false, readOnly: false,
        selectionStart: 0, selectionEnd: 0,
        focus() { this.focused = true; },
        setSelectionRange(start, finish) { this.selectionStart = start; this.selectionEnd = finish; }
    };
    const context = {
        document: {getElementById: id => id === 'commandInput' ? input : null},
        showToast: (message, color) => toasts.push({message, color}),
        fetch: (url, options) => {
            assert.equal(url, '/api/command');
            assert.equal(options.method, 'POST');
            assert.equal(options.headers['Content-Type'], 'application/json');
            const request = {url, options, command: JSON.parse(options.body).command};
            requests.push(request);
            if (deferred) return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
            if (networkError) return Promise.reject(new Error('Network unavailable'));
            return Promise.resolve({ok: response.status !== 'error', json: async () => response});
        }
    };
    const storageAccess = () => { throw new Error('Console command history must not use browser storage'); };
    Object.defineProperties(context, {localStorage: {get: storageAccess}, sessionStorage: {get: storageAccess}});
    context.window = {};
    Object.defineProperties(context.window, {localStorage: {get: storageAccess}, sessionStorage: {get: storageAccess}});
    vm.createContext(context);
    vm.runInContext(source, context, {filename: 'console-template-history.js'});
    return {
        context, input, requests, toasts,
        history: () => Array.from(vm.runInContext('commandHistory', context)),
        submit(value) { input.value = value; context.sendCommand(); },
        key(key, overrides = {}) {
            const event = {
                key, target: input, currentTarget: input, altKey: false, ctrlKey: false,
                metaKey: false, shiftKey: false, isComposing: false, keyCode: 0,
                repeat: false, defaultPrevented: false, preventCount: 0,
                preventDefault() { this.defaultPrevented = true; this.preventCount += 1; },
                ...overrides
            };
            context.handleCommandKeydown(event);
            return event;
        }
    };
}

async function flush() { for (let index = 0; index < 20; index += 1) await Promise.resolve(); }
const sent = env => env.requests.map(request => request.command);

(async () => {
    const empty = harness();
    empty.input.value = 'Unsent draft';
    for (const key of ['ArrowUp', 'ArrowDown']) {
        assert.equal(empty.key(key).preventCount, 0, 'Empty history navigation must be a no-op');
        assert.equal(empty.input.value, 'Unsent draft');
    }
    for (const value of ['', '  ', '\t\n']) empty.submit(value);
    assert.deepEqual(empty.history(), []);
    assert.equal(empty.requests.length, 0);

    // Button and Enter share history; command prefixes pass through unchanged.
    const basic = harness();
    basic.submit('  help  ');
    assert.deepEqual(basic.history(), ['help'], 'Remember immediately, before fetch settles');
    assert.equal(basic.input.value, '');
    basic.input.value = '/plugins';
    assert.equal(basic.key('Enter').preventCount, 1);
    basic.submit('//pos1');
    basic.submit('minecraft:help');
    assert.deepEqual(sent(basic), ['help', '/plugins', '//pos1', 'minecraft:help']);
    assert.deepEqual(basic.history(), sent(basic));
    assert.equal(basic.input.value, '');
    await flush();

    // Moving through history never emits, clamps at the oldest, then restores
    // the unsent draft exactly (including whitespace) after the newest entry.
    const navigation = harness();
    navigation.submit('help');
    navigation.submit('list');
    navigation.submit('/plugins');
    navigation.input.value = '  say Not sent yet  ';
    navigation.key('ArrowDown');
    assert.equal(navigation.input.value, '  say Not sent yet  ');
    for (const expected of ['/plugins', 'list', 'help', 'help']) {
        assert.equal(navigation.key('ArrowUp').preventCount, 1);
        assert.equal(navigation.input.value, expected);
    }
    for (const expected of ['list', '/plugins', '  say Not sent yet  ']) {
        assert.equal(navigation.key('ArrowDown').preventCount, 1);
        assert.equal(navigation.input.value, expected);
    }
    navigation.key('ArrowDown');
    assert.equal(navigation.input.value, '  say Not sent yet  ');
    assert.equal(navigation.requests.length, 3, 'Browsing history must never send commands');

    navigation.input.value = 'A different draft';
    navigation.key('ArrowUp');
    navigation.input.value = 'Edited recalled command';
    navigation.key('ArrowUp');
    assert.equal(navigation.input.value, 'list');
    navigation.key('ArrowDown');
    assert.equal(navigation.input.value, '/plugins', 'Editing a recall must not mutate saved history');
    navigation.key('ArrowDown');
    assert.equal(navigation.input.value, 'A different draft');
    assert.deepEqual(navigation.history(), ['help', 'list', '/plugins']);

    // Submitting an edited recall resets both history position and old draft.
    navigation.key('ArrowUp');
    navigation.input.value = 'say Edited command';
    navigation.key('Enter');
    assert.equal(navigation.input.value, '');
    navigation.key('ArrowUp');
    assert.equal(navigation.input.value, 'say Edited command');
    navigation.key('ArrowDown');
    assert.equal(navigation.input.value, '', 'A sent command must not resurrect an older unsent draft');
    assert.deepEqual(navigation.history(), ['help', 'list', '/plugins', 'say Edited command']);

    const duplicates = harness();
    for (const command of ['help', 'help', ' help ', 'list', 'help']) duplicates.submit(command);
    assert.deepEqual(duplicates.history(), ['help', 'list', 'help']);
    assert.equal(duplicates.requests.length, 5, 'Duplicate suppression affects history, not explicit command submission');
    duplicates.key('ArrowUp');
    duplicates.key('Enter');
    assert.deepEqual(duplicates.history(), ['help', 'list', 'help']);
    duplicates.key('ArrowUp');
    duplicates.key('ArrowDown');
    assert.equal(duplicates.input.value, '');

    const limited = harness();
    for (let index = 1; index <= 105; index += 1) limited.submit('say command ' + index);
    assert.equal(limited.history().length, 100);
    assert.equal(limited.history()[0], 'say command 6');
    assert.equal(limited.history().at(-1), 'say command 105');
    limited.input.value = 'Limit test draft';
    for (let index = 0; index < 110; index += 1) limited.key('ArrowUp');
    assert.equal(limited.input.value, 'say command 6');
    for (let index = 0; index < 100; index += 1) limited.key('ArrowDown');
    assert.equal(limited.input.value, 'Limit test draft');
    assert.equal(limited.requests.length, 105);
    assert.deepEqual(harness().history(), [], 'Reloaded/new page history starts empty');

    // Modified, already-handled, IME, and read-only/disabled keys cannot leak
    // commands or move through another control's active edit operation.
    for (const guard of [
        {altKey: true}, {ctrlKey: true}, {metaKey: true}, {shiftKey: true},
        {defaultPrevented: true}, {isComposing: true}, {keyCode: 229}
    ]) {
        const guarded = harness();
        guarded.submit('help');
        guarded.input.value = 'Protected draft';
        for (const key of ['ArrowUp', 'ArrowDown', 'Enter']) {
            assert.equal(guarded.key(key, guard).preventCount, 0, JSON.stringify({key, guard}));
            assert.equal(guarded.input.value, 'Protected draft');
        }
        assert.equal(guarded.requests.length, 1);
        assert.deepEqual(guarded.history(), ['help']);
    }
    for (const state of ['disabled', 'readOnly']) {
        const guarded = harness();
        guarded.submit('help');
        guarded.input[state] = true;
        guarded.input.value = 'Cannot send';
        guarded.context.sendCommand();
        for (const key of ['ArrowUp', 'ArrowDown', 'Enter']) guarded.key(key);
        assert.equal(guarded.input.value, 'Cannot send');
        assert.equal(guarded.requests.length, 1);
        assert.deepEqual(guarded.history(), ['help']);
    }
    const repeated = harness();
    repeated.input.value = 'say Do not repeat';
    repeated.key('Enter', {repeat: true});
    assert.equal(repeated.requests.length, 0);
    assert.equal(repeated.input.value, 'say Do not repeat');
    repeated.key('Enter');
    repeated.key('Enter', {repeat: true});
    assert.equal(repeated.requests.length, 1);
    repeated.input.value = 'Unrelated key draft';
    assert.equal(repeated.key('ArrowLeft').preventCount, 0);
    assert.equal(repeated.input.value, 'Unrelated key draft');

    // Slow or reordered responses must not reorder immediately recorded attempts.
    const ordered = harness({deferred: true});
    ordered.submit('first');
    ordered.submit('/second');
    assert.deepEqual(ordered.history(), ['first', '/second']);
    ordered.requests[1].resolve({json: async () => ({status: 'success'})});
    ordered.requests[0].resolve({json: async () => ({status: 'error', message: 'First command refused'})});
    await flush();
    assert.deepEqual(ordered.history(), ['first', '/second']);
    assert(ordered.toasts.some(toast => toast.message.includes('First command refused')));

    for (const failure of [{response: {status: 'error', message: 'Server offline'}}, {networkError: true}]) {
        const retry = harness(failure);
        retry.submit('/plugins');
        assert.deepEqual(retry.history(), ['/plugins']);
        await flush();
        assert(retry.toasts.length > 0, 'Command failure should still be reported');
        retry.key('ArrowUp');
        assert.equal(retry.input.value, '/plugins', 'Failed command stays available for intentional retry');
        assert.equal(retry.requests.length, 1, 'Failure must not automatically resubmit a command');
        retry.key('Enter');
        await flush();
        assert.equal(retry.requests.length, 2);
        assert.deepEqual(retry.history(), ['/plugins']);
    }

    await flush();
    console.log('Console history checks passed (Up/Down, drafts, submission order, prefixes, limits, duplicate suppression, keyboard/permission guards and retryable failed attempts).');
})().catch(error => { console.error(error); process.exitCode = 1; });
