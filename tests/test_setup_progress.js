/* Exercise the production poller without a browser, network, or Minecraft. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/setup_progress.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const progress = (status, percent, msg = status) => ({status, percent, msg});
const reply = (data, status = 200, type = 'application/json') => ({
    status, ok: status >= 200 && status < 300, redirected: false,
    headers: {get: () => type}, json: async () => data
});

function harness(responses) {
    const timers = new Map();
    const elements = Object.fromEntries(['title', 'msg', 'fill', 'percent'].map(name =>
        ['progress-' + name, {innerText: '', style: {backgroundColor: '', width: '0%'}}]));
    const calls = [];
    const modals = [];
    const location = {href: '/setup'};
    let id = 0;
    const window = {};
    vm.runInNewContext(source, {
        window, location, AbortController,
        document: {getElementById: name => elements[name]},
        showModal: (...args) => modals.push(args),
        setTimeout: (callback, delay) => {
            timers.set(++id, {callback, delay});
            return id;
        },
        clearTimeout: timer => timers.delete(timer),
        fetch: async (url, options) => {
            assert.equal(url, '/api/setup/progress');
            assert.ok(!options.method || options.method === 'GET', 'Polling never resubmits setup');
            assert.equal(options.cache, 'no-store');
            calls.push({url, options});
            assert.ok(responses.length, 'Unexpected extra request');
            const response = responses.shift();
            return typeof response === 'function' ? response(options) : response;
        }
    });
    return {
        elements, timers, calls, modals, location, start: window.startSetupProgress,
        async tick(delay) {
            const entry = [...timers].find(([, timer]) => timer.delay === delay);
            assert.ok(entry, 'Expected timer for ' + delay + 'ms');
            timers.delete(entry[0]);
            entry[1].callback();
            await flush();
        },
        text(name) { return elements['progress-' + name].innerText; }
    };
}

async function main() {
    const success = harness([
        reply(progress('running', 90, 'Creating Initial Backup/Restore Point')),
        reply(progress('done', 100, 'Done'))
    ]);
    success.start();
    await success.tick(1000);
    assert.equal(success.text('percent'), '90%');
    assert.equal(success.location.href, '/setup');
    await success.tick(1000);
    assert.equal(success.text('percent'), '100%');
    assert.equal(success.location.href, '/overview');
    assert.equal(success.text('title'), 'Server ready');
    assert.equal(success.timers.size, 0);
    assert.equal(success.modals.length, 0);

    // Even 100% is not success without the worker's explicit done status.
    const failure = harness([
        reply(progress('running', 100)),
        reply(progress('error', 100, 'Error: backup failed'))
    ]);
    failure.start();
    await failure.tick(1000);
    assert.equal(failure.location.href, '/setup');
    await failure.tick(1000);
    assert.equal(failure.location.href, '/setup');
    assert.equal(failure.text('title'), 'Server Build Failed');
    assert.equal(failure.elements['progress-fill'].style.backgroundColor, '#dc2626');
    assert.deepEqual(failure.modals, [['Server Build Failed', 'Error: backup failed']]);
    assert.equal(failure.timers.size, 0);

    const unavailable = [
        // The old 90% bug's HTTP 409 body must never become a build failure.
        reply({status: 'error', message: 'Reset the server before running setup again.'}, 409),
        reply(progress('error', 90, 'Gateway failure'), 500),
        reply('<!DOCTYPE html>', 502, 'text/html'),
        reply('<!DOCTYPE html>', 200, 'text/html'),
        {...reply(null), json: async () => { throw new SyntaxError('Invalid JSON'); }},
        reply(null),
        reply({status: 'error', message: 'Not an installer result'}),
        reply(progress('running', '90')),
        reply(progress('running', -1)),
        reply(progress('running', 101)),
        reply(progress('running', NaN)),
        reply(progress('unexpected', 90)),
        () => { throw new Error('Network disconnected'); }
    ];
    for (const response of unavailable) {
        const test = harness([
            reply(progress('running', 90)), response, reply(progress('done', 100))
        ]);
        test.start();
        await test.tick(1000);
        await test.tick(1000);
        assert.equal(test.text('title'), 'Progress unavailable');
        assert.match(test.text('msg'), /Retrying automatically/);
        assert.equal(test.text('percent'), '90%');
        assert.equal(test.elements['progress-fill'].style.backgroundColor, '');
        assert.equal(test.modals.length, 0);
        assert.equal(test.location.href, '/setup');
        await test.tick(3000);
        assert.equal(test.location.href, '/overview');
        assert.equal(test.timers.size, 0);
    }

    for (const response of [
        reply({status: 'error'}, 401), reply({status: 'error'}, 403),
        {...reply('<html>Sign in</html>', 200, 'text/html'), redirected: true}
    ]) {
        const test = harness([response]);
        test.start();
        await test.tick(1000);
        assert.equal(test.text('title'), 'Progress unavailable');
        assert.match(test.text('msg'), /Sign in/);
        assert.equal(test.modals.length, 0);
        assert.equal(test.timers.size, 0, 'Auth errors must not retry indefinitely');
        assert.equal(test.location.href, '/setup');
    }

    const timeout = harness([
        options => new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(new Error('Timed out')));
        }),
        reply(progress('done', 100))
    ]);
    timeout.start();
    await timeout.tick(1000);
    assert.equal(timeout.calls.length, 1);
    assert.deepEqual([...timeout.timers.values()].map(timer => timer.delay), [15000],
        'No new poll while the request is pending');
    await timeout.tick(15000);
    assert.ok(timeout.calls[0].options.signal.aborted);
    assert.equal(timeout.text('title'), 'Progress unavailable');
    await timeout.tick(3000);
    assert.equal(timeout.location.href, '/overview');
    assert.equal(timeout.timers.size, 0);

    // Restarting the monitor aborts the old one and ignores late responses.
    let finishOld;
    const restart = harness([
        () => new Promise(resolve => { finishOld = resolve; }),
        reply(progress('done', 100))
    ]);
    restart.start();
    await restart.tick(1000);
    restart.start();
    assert.ok(restart.calls[0].options.signal.aborted);
    finishOld(reply(progress('error', 90, 'Stale result')));
    await flush();
    assert.equal(restart.modals.length, 0);
    assert.notEqual(restart.text('title'), 'Server Build Failed');
    await restart.tick(1000);
    assert.equal(restart.location.href, '/overview');
    assert.equal(restart.timers.size, 0);

    console.log('Setup progress: completion, real failure, 13 transient errors, 3 auth errors, timeout, and stale-response checks passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
