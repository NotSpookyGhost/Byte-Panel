'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/backup_progress.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const id = 'a'.repeat(32);
const job = (status, extra = {}) => ({job_id: id, status, message: status, ...extra});
const reply = (data, status = 200, type = 'application/json') => ({
    ok: status >= 200 && status < 300, status, redirected: false,
    headers: {get: () => type}, json: async () => data
});

function harness(responses, restore = false) {
    const timers = new Map();
    const calls = [];
    const messages = [];
    let timerId = 0;
    let outcome;
    const window = {};
    vm.runInNewContext(source, {
        window, AbortController,
        setTimeout: (callback, delay) => {
            timers.set(++timerId, {callback, delay});
            return timerId;
        },
        clearTimeout: id => timers.delete(id),
        fetch: async (url, options) => {
            calls.push({url, options});
            assert.equal(options.cache, 'no-store');
            assert.ok(responses.length, 'Unexpected extra request');
            const response = responses.shift();
            return typeof response === 'function' ? response(options) : response;
        }
    });
    window.runBackupJob(restore, message => messages.push(message)).then(
        result => { outcome = {result}; }, error => { outcome = {error}; }
    );
    return {
        calls, messages, timers, get outcome() { return outcome; },
        async tick(delay = 1000) {
            await flush();
            const entry = [...timers].find(([, value]) => value.delay === delay);
            assert.ok(entry, 'Expected timer for ' + delay);
            timers.delete(entry[0]);
            entry[1].callback();
            await flush();
        }
    };
}

async function main() {
    for (const restore of [false, true]) {
        const test = harness([
            reply(job('running'), 202),
            reply(job('running', {message: 'Verifying archive...'})),
            reply(job('success', {file: 'backup.zip'}))
        ], restore);
        await flush();
        assert.deepEqual(JSON.parse(test.calls[0].options.body), {background: true, is_restore: restore});
        assert.equal(test.calls[0].options.method, 'POST');
        await test.tick();
        assert.equal(test.outcome, undefined, 'Must wait for verified success before restoring');
        assert.ok(test.messages.includes('Verifying archive...'));
        await test.tick();
        assert.equal(test.outcome.result.file, 'backup.zip');
        assert.equal(test.calls.length, 3);
        assert.equal(test.calls[1].url, '/api/backup/status/' + id);
        assert.equal(test.calls[1].options.method, undefined);
        assert.equal(test.timers.size, 0);
    }

    const failed = harness([reply(job('running'), 202), reply(job('error', {message: 'Not enough free space.'}))], true);
    await failed.tick();
    assert.match(failed.outcome.error.message, /free space/);
    assert.equal(failed.outcome.result, undefined, 'Failed safety backup never authorizes a restore');
    assert.equal(failed.timers.size, 0);

    const recoverable = [
        reply('<html>Gateway timeout</html>', 504, 'text/html'),
        reply('<html>Error</html>', 200, 'text/html'),
        reply({message: 'Unavailable'}, 503),
        reply(null),
        reply(job('success', {file: 'not-a-zip'})),
        reply(job('success')),
        reply({...job('running'), job_id: 'wrong'}),
        {...reply(null), json: async () => { throw new SyntaxError('bad json'); }},
        () => { throw new Error('offline'); }
    ];
    for (const response of recoverable) {
        const test = harness([reply(job('running'), 202), response, reply(job('success', {file: 'ok.zip'}))]);
        await test.tick();
        assert.equal(test.outcome, undefined);
        assert.match(test.messages.at(-1), /does not mean the backup failed/);
        await test.tick(3000);
        assert.equal(test.outcome.result.file, 'ok.zip');
        assert.equal(test.calls.filter(call => call.options.method === 'POST').length, 1);
    }

    for (const [response, expected] of [
        [reply({message: 'Another operation is running'}, 409), /operation is running/],
        [reply({}, 401), /session expired/],
        [reply({}, 403), /access denied/],
        [{...reply('', 200, 'text/html'), redirected: true}, /session expired/],
        [reply('<html>Proxy error</html>', 502, 'text/html'), /status is unknown/]
    ]) {
        const test = harness([response]);
        await flush();
        assert.match(test.outcome.error.message, expected);
        assert.equal(test.calls.length, 1, 'Never blindly resubmit backup creation');
        assert.equal(test.timers.size, 0);
    }

    for (const response of [reply({}, 401), reply({}, 403), reply({message: 'Panel restarted; check backup list.'}, 404)]) {
        const test = harness([reply(job('running')), response]);
        await test.tick();
        assert.ok(test.outcome.error);
        assert.equal(test.timers.size, 0);
    }

    const repeated = harness([reply(job('running')), ...Array.from({length: 20}, () => reply('', 502, 'text/html'))]);
    await repeated.tick();
    for (let count = 1; count < 20; count++) await repeated.tick(3000);
    assert.match(repeated.outcome.error.message, /may still be running/);
    assert.equal(repeated.calls.filter(call => call.options.method === 'POST').length, 1);
    assert.equal(repeated.timers.size, 0);

    const timeout = harness([
        reply(job('running')),
        options => new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('timeout')))),
        reply(job('success', {file: 'ok.zip'}))
    ]);
    await timeout.tick();
    await timeout.tick(15000);
    assert.equal(timeout.outcome, undefined);
    await timeout.tick(3000);
    assert.equal(timeout.outcome.result.file, 'ok.zip');

    const template = fs.readFileSync('app/templates/backups.html', 'utf8');
    assert.match(template, /runBackupJob\(false/);
    assert.match(template, /runBackupJob\(true/);
    assert.match(template, /textContent = msg/);
    assert.ok(!template.includes("fetch('/api/backup/create'"), 'Both creation workflows use the background job helper');
    console.log('Backup progress tests passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
