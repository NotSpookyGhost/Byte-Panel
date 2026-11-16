'use strict';
// Exercise the real account/SMTP form script using isolated browser API doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/accounts.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function response(data, {status = 200, type = 'application/json', redirected = false, invalid = false} = {}) {
    return {ok: status >= 200 && status < 300, status, redirected,
        headers: {get: () => type},
        async json() {
            this.parsed = true;
            if (invalid) throw new SyntaxError('Unexpected token < <!DOCTYPE secret HTML>');
            return data;
        }};
}
function form(elements) {
    const button = {disabled: false};
    return {elements, button, listeners: {},
        querySelector: () => button,
        querySelectorAll: () => [...Object.values(elements), button],
        addEventListener(name, callback) { this.listeners[name] = callback; }};
}
async function setup(result, {loadingResult, enabled = true, saveResult} = {}) {
    const config = {enabled, host: 'smtp.gmail.com', port: 587, security: 'starttls',
        username: 'test@example.com', password_set: true, notify_new_users: true,
        from_email: 'test@example.com', from_name: 'Byte Panel', panel_url: 'https://panel.example.com'};
    const elements = Object.fromEntries(Object.entries(config).map(([key, value]) => [key,
        {type: typeof value === 'boolean' ? 'checkbox' : 'text', value: '', checked: false}]));
    elements.password = {value: ''};
    elements.clear_password = {checked: false};
    const smtpForm = form(elements);
    const testForm = form({recipient: {value: 'test@example.com'}});
    const notice = {hidden: true, dataset: {}, textContent: ''};
    const state = {textContent: ''};
    const nodes = {'smtp-form': smtpForm, 'smtp-test-form': testForm, 'smtp-message': notice,
        'smtp-password-status': {textContent: ''}, 'smtp-state': state};
    const calls = [];
    const fetch = async (url, options) => {
        calls.push({url, options});
        if (url === '/api/settings/smtp') {
            if (options.method === 'PUT') {
                if (saveResult) return saveResult;
                Object.assign(config, JSON.parse(options.body));
                if (config.password) config.password_set = true;
                delete config.password;
                return response({smtp: config});
            }
            return loadingResult || response({smtp: config});
        }
        if (url !== '/api/settings/smtp/test') throw new Error('Unexpected URL: ' + url);
        if (result instanceof Error) throw result;
        return result;
    };
    vm.runInNewContext(source, {document: {getElementById: id => nodes[id] || null}, fetch});
    await settle();
    async function submit() {
        const event = {preventDefault() { this.prevented = true; }};
        await testForm.listeners.submit(event);
        assert.equal(event.prevented, true);
    }
    async function save() { await smtpForm.listeners.submit({preventDefault() {}}); }
    function change(key, value) {
        const field = elements[key];
        if (field.type === 'checkbox') field.checked = value;
        else field.value = value;
        smtpForm.listeners.input({target: field});
    }
    return {submit, save, change, notice, state, calls, testForm, smtpForm};
}

(async () => {
    const success = await setup(response({success: true, message: 'Test email accepted.'}));
    await success.submit();
    assert.equal(success.notice.textContent, 'Test email accepted.');
    assert.equal(success.notice.dataset.error, 'false');
    assert.equal(success.notice.hidden, false);
    assert.equal(success.testForm.button.disabled, false);
    const sent = success.calls.at(-1);
    assert.equal(sent.options.method, 'POST');
    assert.equal(sent.options.headers.Accept, 'application/json');
    assert.equal(sent.options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(sent.options.body), {recipient: 'test@example.com'});
    assert.equal(sent.options.body.includes('password'), false);

    const authentication = await setup(response({success: false, status: 'error',
        error: 'SMTP authentication was rejected. (SMTP 535)', error_code: 'authentication_failed'}));
    await authentication.submit();
    assert.match(authentication.notice.textContent, /SMTP 535/);
    assert.equal(authentication.notice.dataset.error, 'true');

    for (const [status, expected] of [[401, /sign in/], [403, /permissions/], [404, /API was not found/],
        [500, /non-JSON response/], [502, /error page/], [503, /error page/], [504, /error page/], [200, /non-JSON response/]]) {
        const html = response('<!DOCTYPE html><html>private proxy details</html>', {status, type: 'text/html', invalid: true});
        const ui = await setup(html);
        await ui.submit();
        assert.match(ui.notice.textContent, expected);
        assert.match(ui.notice.textContent, new RegExp('HTTP ' + status));
        assert.equal(html.parsed, undefined); // Never attempt to parse or expose HTML.
        assert.equal(ui.notice.dataset.error, 'true');
        assert.doesNotMatch(ui.notice.textContent, /Unexpected token|DOCTYPE|private proxy details/);
    }
    const login = response(null, {redirected: true, type: 'text/html'});
    const redirected = await setup(login);
    await redirected.submit();
    assert.match(redirected.notice.textContent, /redirected.*sign in/);
    assert.equal(login.parsed, undefined);

    const malformed = await setup(response(null, {invalid: true}));
    await malformed.submit();
    assert.match(malformed.notice.textContent, /unreadable JSON/);
    assert.doesNotMatch(malformed.notice.textContent, /DOCTYPE|secret HTML/);
    for (const value of [null, [], 'wrong shape']) {
        const invalid = await setup(response(value));
        await invalid.submit();
        assert.match(invalid.notice.textContent, /invalid API result/);
    }
    const csrf = await setup(response({error: 'Refresh the page to verify your session.'},
        {status: 403, type: 'application/problem+json; charset=utf-8'}));
    await csrf.submit();
    assert.equal(csrf.notice.textContent, 'Refresh the page to verify your session.');
    const unreachable = await setup(new TypeError('Failed to fetch'));
    await unreachable.submit();
    assert.match(unreachable.notice.textContent, /Could not reach the panel/);
    assert.equal(unreachable.calls.filter(call => call.url.endsWith('/test')).length, 1); // No automatic duplicate sends.

    let finish;
    const pending = await setup(new Promise(resolve => { finish = resolve; }));
    const request = pending.submit();
    assert.equal(pending.testForm.button.disabled, true);
    assert.match(pending.notice.textContent, /saved SMTP settings/);
    finish(response({success: true, message: 'Sent'}));
    await request;
    assert.equal(pending.testForm.button.disabled, false);

    const failedLoad = await setup(response({}), {loadingResult: response(null, {status: 502, type: 'text/html'})});
    assert.match(failedLoad.notice.textContent, /HTTP 502/);
    assert.equal(failedLoad.smtpForm.button.disabled, true);
    assert.equal(failedLoad.testForm.button.disabled, true);
    assert.match(failedLoad.state.textContent, /could not be loaded/);
    await failedLoad.submit();
    assert.equal(failedLoad.calls.filter(call => call.url.endsWith('/test')).length, 0);

    // Reproduce the screenshot: SMTP disabled with a saved password.
    const disabled = await setup(response({success: true, message: 'Sent'}), {enabled: false});
    assert.equal(disabled.testForm.button.disabled, true);
    assert.match(disabled.state.textContent, /delivery is disabled.*Enable SMTP delivery.*Save/);
    await disabled.submit();
    assert.equal(disabled.calls.filter(call => call.url.endsWith('/test')).length, 0);
    disabled.change('enabled', true);
    assert.match(disabled.state.textContent, /Unsaved SMTP changes/);
    assert.equal(disabled.testForm.button.disabled, true);
    await disabled.submit();
    assert.equal(disabled.calls.filter(call => call.url.endsWith('/test')).length, 0);
    await disabled.save();
    const saved = JSON.parse(disabled.calls.find(call => call.options.method === 'PUT').options.body);
    assert.equal(saved.enabled, true);
    assert.equal(saved.host, 'smtp.gmail.com'); // Disabled controls must still be serialized.
    assert.equal(saved.from_email, 'test@example.com');
    assert.equal(saved.password, ''); // Preserve saved password on backend.
    assert.equal(saved.notify_new_users, true);
    assert.equal(saved.clear_password, false);
    assert.equal(disabled.testForm.button.disabled, false);
    assert.match(disabled.notice.textContent, /Delivery is enabled/);
    await disabled.submit();
    assert.equal(disabled.notice.textContent, 'Sent');
    disabled.change('enabled', false);
    await disabled.save();
    assert.equal(disabled.testForm.button.disabled, true);
    assert.match(disabled.notice.textContent, /no emails will be sent/);

    const dirty = await setup(response({success: true, message: 'Sent'}));
    dirty.change('host', 'changed.example.com');
    assert.equal(dirty.testForm.button.disabled, true);
    await dirty.submit();
    assert.equal(dirty.calls.filter(call => call.url.endsWith('/test')).length, 0);
    await dirty.save();
    assert.equal(dirty.testForm.button.disabled, false);
    const rejectedSave = await setup(response({success: true, message: 'Sent'}),
        {saveResult: response({error: 'Invalid hostname'}, {status: 400})});
    rejectedSave.change('host', 'bad host');
    await rejectedSave.save();
    assert.equal(rejectedSave.testForm.button.disabled, true);
    assert.equal(rejectedSave.smtpForm.button.disabled, false);
    assert.equal(rejectedSave.notice.textContent, 'Invalid hostname');

    for (const data of [{}, {success: true}, {success: true, message: ''}, {success: true, message: '   '}]) {
        const empty = await setup(response(data));
        await empty.submit();
        assert.match(empty.notice.textContent, /no delivery confirmation/);
        assert.equal(empty.notice.dataset.error, 'true');
    }
    let completeLoad;
    const loading = await setup(response({success: true, message: 'Sent'}),
        {loadingResult: new Promise(resolve => { completeLoad = resolve; })});
    assert.equal(loading.testForm.button.disabled, true);
    assert.match(loading.state.textContent, /Loading saved SMTP/);
    await loading.submit();
    assert.equal(loading.calls.filter(call => call.url.endsWith('/test')).length, 0);
    completeLoad(response({smtp: {enabled: false, password_set: true}}));
    await settle();
    assert.match(loading.state.textContent, /delivery is disabled/);
    console.log('Account/SMTP UI checks passed (HTML/JSON, nonblank results, disabled/unsaved settings, save/test flow, progress, no automatic retries).');
})().catch(error => { console.error(error); process.exitCode = 1; });
