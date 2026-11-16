// Test the real account UI with DOM doubles; no browser or external requests.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/accounts.js', 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
function node() {
    return {
        children: [], listeners: {}, dataset: {}, disabled: false, hidden: false, textContent: '',
        addEventListener(name, callback) { this.listeners[name] = callback; },
        appendChild(child) { this.children.push(child); },
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        scrollIntoView() {}, focus() {}
    };
}
function response(data) {
    return {ok: true, status: 200, redirected: false,
        headers: {get: () => 'application/json'}, json: async () => data};
}
function form(elements) {
    const result = node();
    result.elements = elements;
    result.button = node();
    result.querySelector = () => result.button;
    result.querySelectorAll = () => [];
    result.reset = () => {
        for (const field of Object.values(elements)) {
            if (field.type === 'checkbox') field.checked = true;
            else field.value = '';
        }
        if (elements.role) elements.role.value = 'custom';
    };
    return result;
}
async function userUi() {
    const elements = {username: {...node(), value: 'new-user'}, email: {value: 'new@example.com', required: true},
        role: {...node(), value: 'custom', querySelector: () => ({})}, enabled: {type: 'checkbox', checked: true}};
    // Deliberately no password input: the UI must not read or submit one.
    const userForm = form(elements);
    userForm.dataset = {currentId: 'owner', userId: ''};
    const nodes = {'user-form': userForm, 'user-permissions': node(), 'users-list': node(),
        'new-user': node(), 'cancel-user': node(), 'user-form-title': node(), 'save-user': node(),
        'accounts-message': node()};
    let inviteStatus = 'sent';
    let confirmed = true;
    let holdEmail = false;
    let releaseEmail;
    const calls = [];
    const member = {id: 'member', username: 'member', email: 'member@example.com', role: 'custom',
        enabled: true, permissions: [], must_change_password: true, temporary_password_expires: Date.now() / 1000 + 86400};
    const fetch = async (url, options) => {
        calls.push({url, options});
        if (options.method === 'GET') return response({users: [member]});
        if (options.method === 'POST' && holdEmail) await new Promise(resolve => { releaseEmail = resolve; });
        return response({user: member, email_status: inviteStatus});
    };
    vm.runInNewContext(source, {document: {getElementById: id => nodes[id] || null, createElement: node},
        fetch, confirm: () => confirmed, window: {panelCan: () => true}});
    await settle();
    return {nodes, userForm, elements, calls,
        setConfirmed: value => { confirmed = value; }, setStatus: value => { inviteStatus = value; },
        holdEmail: () => { holdEmail = true; }, releaseEmail: () => { holdEmail = false; releaseEmail(); },
        actions: () => nodes['users-list'].children[0].children[4].children};
}
async function passwordUi(redirect) {
    const fields = {current_password: {value: 'Temporary-password-123'}, password: {value: 'Chosen-password-123'},
        confirm_password: {value: 'Chosen-password-123'}};
    const passwordForm = form(fields);
    const notice = node();
    const calls = [];
    const redirects = [];
    class FormData {
        constructor(target) { return Object.entries(target.elements).map(([key, value]) => [key, value.value]); }
    }
    vm.runInNewContext(source, {
        document: {getElementById: id => id === 'password-form' ? passwordForm : id === 'accounts-message' ? notice : null},
        FormData, window: {location: {assign: value => redirects.push(value)}},
        fetch: async (url, options) => { calls.push({url, options}); return response({message: 'Password changed.', redirect}); }
    });
    return {fields, passwordForm, notice, calls, redirects};
}
(async () => {
    const ui = await userUi();
    assert.match(ui.nodes['users-list'].children[0].children[3].textContent, /Password change required/);
    assert.equal(ui.actions()[1].textContent, 'Resend invitation');
    await ui.userForm.listeners.submit({preventDefault() {}});
    const create = ui.calls.find(call => call.options.method === 'POST' && call.url === '/api/users');
    assert.ok(create);
    const body = JSON.parse(create.options.body);
    assert.equal(body.email, 'new@example.com');
    assert.equal(body.username, 'new-user');
    assert.equal(Object.hasOwn(body, 'password'), false);
    assert.match(ui.nodes['accounts-message'].textContent, /generated temporary password/);
    assert.equal(ui.elements.email.required, true);

    let requests = ui.calls.length;
    ui.setConfirmed(false);
    await ui.actions()[1].listeners.click();
    assert.equal(ui.calls.length, requests);
    ui.setConfirmed(true);
    await ui.actions()[1].listeners.click();
    assert.ok(ui.calls.some(call => call.url === '/api/users/member/invite' && call.options.method === 'POST'));
    assert.match(ui.nodes['accounts-message'].textContent, /24 hours/);
    ui.setStatus('failed');
    await ui.actions()[1].listeners.click();
    assert.equal(ui.nodes['accounts-message'].dataset.error, 'true');
    assert.match(ui.nodes['accounts-message'].textContent, /password was replaced.*email delivery failed/);
    ui.setStatus('password_failed');
    await ui.actions()[1].listeners.click();
    assert.equal(ui.nodes['accounts-message'].dataset.error, 'true');
    assert.match(ui.nodes['accounts-message'].textContent, /welcome email was accepted.*temporary-password email failed/);
    assert.doesNotMatch(ui.nodes['accounts-message'].textContent, /both accepted/);
    ui.setStatus('cancelled');
    await ui.actions()[1].listeners.click();
    assert.equal(ui.nodes['accounts-message'].dataset.error, 'true');
    assert.match(ui.nodes['accounts-message'].textContent, /stopped because the account or email settings changed/);
    await ui.actions()[0].listeners.click();
    assert.equal(ui.userForm.dataset.userId, 'member');
    assert.equal(ui.elements.email.required, true);
    await ui.userForm.listeners.submit({preventDefault() {}});
    const edit = ui.calls.find(call => call.options.method === 'PUT');
    assert.equal(Object.hasOwn(JSON.parse(edit.options.body), 'password'), false);

    for (const status of ['password_failed', 'cancelled']) {
        const partial = await userUi();
        partial.setStatus(status);
        await partial.userForm.listeners.submit({preventDefault() {}});
        assert.equal(partial.nodes['accounts-message'].dataset.error, 'true');
        assert.match(partial.nodes['accounts-message'].textContent, /Account created/);
        assert.doesNotMatch(partial.nodes['accounts-message'].textContent, /both accepted/);
    }

    const waiting = await userUi();
    waiting.holdEmail();
    const pendingCreate = waiting.userForm.listeners.submit({preventDefault() {}});
    await settle();
    assert.equal(waiting.userForm.button.disabled, true);
    assert.match(waiting.nodes['accounts-message'].textContent, /waiting about 12 seconds/);
    const pendingCalls = waiting.calls.length;
    await waiting.userForm.listeners.submit({preventDefault() {}});
    assert.equal(waiting.calls.length, pendingCalls);
    waiting.releaseEmail();
    await pendingCreate;
    assert.equal(waiting.userForm.button.disabled, false);
    assert.match(waiting.nodes['accounts-message'].textContent, /both accepted by SMTP/);

    waiting.holdEmail();
    const inviteButton = waiting.actions()[1];
    const pendingInvite = inviteButton.listeners.click();
    await settle();
    assert.equal(inviteButton.disabled, true);
    assert.match(waiting.nodes['accounts-message'].textContent, /waiting about 12 seconds/);
    const inviteCalls = waiting.calls.length;
    await inviteButton.listeners.click();
    assert.equal(waiting.calls.length, inviteCalls);
    waiting.releaseEmail();
    await pendingInvite;
    assert.match(waiting.nodes['accounts-message'].textContent, /both accepted by SMTP/);

    const firstLogin = await passwordUi('/overview');
    await firstLogin.passwordForm.listeners.submit({preventDefault() {}});
    assert.deepEqual(firstLogin.redirects, ['/overview']);
    assert.equal(firstLogin.calls[0].url, '/api/account/password');
    const normal = await passwordUi(null);
    await normal.passwordForm.listeners.submit({preventDefault() {}});
    assert.deepEqual(normal.redirects, []);
    const mismatch = await passwordUi('/overview');
    mismatch.fields.confirm_password.value = 'wrong';
    await mismatch.passwordForm.listeners.submit({preventDefault() {}});
    assert.equal(mismatch.calls.length, 0);
    assert.match(mismatch.notice.textContent, /do not match/);
    console.log('Invitation UI checks passed (two-email progress, partial failure/cancellation, duplicate prevention, generated credentials and first-login redirect).');
})().catch(error => { console.error(error); process.exitCode = 1; });
