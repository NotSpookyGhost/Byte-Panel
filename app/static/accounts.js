/* All user-entered account data is rendered as text, never interpolated HTML. */
(() => {
    'use strict';
    function message(text, error = false, id = 'accounts-message') {
        const element = document.getElementById(id);
        if (typeof text !== 'string' || !text.trim()) {
            text = 'The panel returned no readable result. Refresh the page and check the running panel version.';
            error = true;
        }
        element.textContent = text; element.dataset.error = String(error); element.hidden = false;
    }
    async function api(path, method = 'GET', body) {
        let response;
        try {
            response = await fetch(path, {method, credentials: 'same-origin', cache: 'no-store',
                headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
                ...(body === undefined ? {} : {body: JSON.stringify(body)})});
        } catch {
            throw new Error('Could not reach the panel. Check your connection and reverse proxy. If testing email, check the inbox before retrying.');
        }
        if (response.redirected) {
            throw new Error('The panel redirected this request instead of returning a result. Refresh and sign in again; check proxy authentication if this continues.');
        }
        const status = 'HTTP ' + response.status;
        const contentType = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
        if (contentType !== 'application/json' && !contentType.endsWith('+json')) {
            if (response.status === 401 || response.status === 403) {
                throw new Error('The panel or proxy refused access (' + status + '). Refresh and sign in again, then check your permissions.');
            }
            if (response.status === 404) {
                throw new Error('The panel API was not found (' + status + '). Check that the updated container is running and the proxy routes /api/ to the panel.');
            }
            if ([502, 503, 504].includes(response.status)) {
                throw new Error('The panel or reverse proxy returned an error page (' + status + '), not an SMTP result. Check the container/proxy logs and connection timeouts. Check the inbox before retrying.');
            }
            throw new Error('The panel returned a non-JSON response (' + status + '). Refresh the page; if it continues, check the panel container and proxy logs.');
        }
        let data;
        try { data = await response.json(); }
        catch { throw new Error('The panel returned unreadable JSON (' + status + '). Refresh the page and check the container/proxy logs.'); }
        if (!data || typeof data !== 'object' || Array.isArray(data)) {
            throw new Error('The panel returned an invalid API result (' + status + '). Refresh the page and check the running container version.');
        }
        if (!response.ok || data.success === false || data.status === 'error') {
            throw new Error(typeof data.error === 'string' && data.error ? data.error : 'Request failed (' + status + '). Refresh the page and try again.');
        }
        return data;
    }
    function bindForm(form, callback, messageId = 'accounts-message', onBusy = () => {}) {
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const button = form.querySelector('[type="submit"]');
            if (button.disabled) return;
            button.disabled = true;
            onBusy(true);
            try { await callback(); } catch (error) { message(error.message, true, messageId); }
            finally { button.disabled = false; onBusy(false); }
        });
    }
    const userForm = document.getElementById('user-form');
    if (userForm) {
        const fields = userForm.elements;
        const invitationProgress = 'Sending the welcome email, then waiting about 12 seconds before sending the temporary-password email. Please keep this page open until the result appears.';
        function invitationResult(status, replacing = false) {
            if (status === 'sent') return 'The welcome email and generated temporary password email were both accepted by SMTP. The temporary password expires in 24 hours and must be changed before panel access.';
            if (status === 'password_failed') return 'The welcome email was accepted by SMTP, but the separate temporary-password email failed or is unconfirmed. Check the inbox, spam folder and SMTP settings before using Resend invitation.';
            if (status === 'cancelled') return 'Invitation delivery was stopped because the account or email settings changed. Review the account and SMTP settings, then use Resend invitation if needed.';
            return (replacing ? 'The password was replaced, but email delivery failed or is unconfirmed.' : 'Invitation delivery failed or is unconfirmed.')
                + ' Check the recipient inbox and SMTP settings before resending the invitation. Do not create the account again.';
        }
        function roleChanged() {
            document.getElementById('user-permissions').disabled = fields.role.value === 'admin';
        }
        function resetForm() {
            userForm.reset(); userForm.dataset.userId = '';
            fields.email.required = true;
            document.getElementById('user-form-title').textContent = 'Create user';
            document.getElementById('save-user').textContent = 'Create user';
            roleChanged();
        }
        function editUser(user) {
            resetForm(); userForm.dataset.userId = user.id;
            ['username', 'email', 'role'].forEach(key => { fields[key].value = user[key]; });
            fields.enabled.checked = user.enabled;
            fields.email.required = user.must_change_password;
            userForm.querySelectorAll('[name="permission"]').forEach(box => { box.checked = user.permissions.includes(box.value); });
            document.getElementById('user-form-title').textContent = `Edit ${user.username}`;
            document.getElementById('save-user').textContent = 'Save user';
            roleChanged(); userForm.scrollIntoView({behavior: 'smooth', block: 'start'});
        }
        async function loadUsers() {
            const data = await api('/api/users');
            const list = document.getElementById('users-list'); list.replaceChildren();
            data.users.forEach(user => {
                const row = document.createElement('tr');
                [user.username, user.email || '—', user.role === 'custom' ? `${user.permissions.length} permissions` : user.role,
                    !user.enabled ? 'Disabled' : user.must_change_password
                        ? (user.temporary_password_expires <= Date.now() / 1000 ? 'Invitation expired' : 'Password change required')
                        : 'Enabled'].forEach(value => {
                    const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
                });
                const actions = document.createElement('td');
                const manageable = user.role !== 'owner' && user.id !== userForm.dataset.currentId &&
                    (user.role === 'admin' ? fields.role.querySelector('option[value="admin"]') : user.permissions.every(window.panelCan));
                if (manageable) {
                    const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'btn btn-outline';
                    edit.textContent = 'Edit'; edit.addEventListener('click', () => editUser(user));
                    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-outline';
                    remove.textContent = 'Delete'; remove.addEventListener('click', async () => {
                        if (!confirm(`Delete ${user.username}? Their sessions will be signed out immediately.`)) return;
                        remove.disabled = true;
                        try {
                            await api(`/api/users/${user.id}`, 'DELETE');
                            if (userForm.dataset.userId === user.id) resetForm();
                            await loadUsers(); message('Account deleted and sessions revoked.');
                        } catch (error) { message(error.message, true); remove.disabled = false; }
                    });
                    const invite = document.createElement('button'); invite.type = 'button'; invite.className = 'btn btn-outline';
                    invite.textContent = user.must_change_password ? 'Resend invitation' : 'Send temporary password';
                    invite.addEventListener('click', async () => {
                        if (invite.disabled) return;
                        if (!confirm(`Email ${user.username} a new temporary password? Their current password and sessions will stop working. The temporary password expires in 24 hours.`)) return;
                        invite.disabled = true;
                        message(invitationProgress);
                        try {
                            const result = await api(`/api/users/${user.id}/invite`, 'POST', {});
                            await loadUsers();
                            message(invitationResult(result.email_status, true), result.email_status !== 'sent');
                        } catch (error) { message(error.message, true); invite.disabled = false; }
                    });
                    actions.append(edit, invite, remove);
                } else { actions.textContent = user.id === userForm.dataset.currentId ? 'You · use My account' : 'Protected'; }
                row.appendChild(actions); list.appendChild(row);
            });
        }
        fields.role.addEventListener('change', roleChanged);
        document.getElementById('new-user').addEventListener('click', () => { resetForm(); fields.username.focus(); });
        document.getElementById('cancel-user').addEventListener('click', resetForm);
        bindForm(userForm, async () => {
            const editing = userForm.dataset.userId;
            const body = {username: fields.username.value, email: fields.email.value,
                role: fields.role.value, enabled: fields.enabled.checked,
                permissions: [...userForm.querySelectorAll('[name="permission"]:checked')].map(box => box.value)};
            if (!editing) message(invitationProgress);
            const data = await api(editing ? `/api/users/${editing}` : '/api/users', editing ? 'PUT' : 'POST', body);
            resetForm(); await loadUsers();
            const suffix = editing ? ' Existing sessions were revoked. Use Send temporary password / Resend invitation if new sign-in details are needed.'
                : ' ' + invitationResult(data.email_status);
            message((editing ? 'Account updated.' : 'Account created.') + suffix, !editing && data.email_status !== 'sent');
        });
        loadUsers().catch(error => message(error.message, true));
    }
    const passwordForm = document.getElementById('password-form');
    if (passwordForm) bindForm(passwordForm, async () => {
        const data = Object.fromEntries(new FormData(passwordForm));
        if (data.password !== data.confirm_password) throw new Error('The new passwords do not match.');
        const result = await api('/api/account/password', 'POST', data);
        passwordForm.reset(); message(result.message);
        if (result.redirect === '/overview') window.location.assign('/overview');
    });
    const smtpForm = document.getElementById('smtp-form');
    if (smtpForm) {
        const fields = smtpForm.elements;
        const testForm = document.getElementById('smtp-test-form');
        const saveButton = smtpForm.querySelector('[type="submit"]');
        const testButton = testForm.querySelector('[type="submit"]');
        const state = document.getElementById('smtp-state');
        let loaded = false;
        let loadFailed = false;
        let savedEnabled = false;
        let changed = false;
        let saving = false;
        let testing = false;
        function updateSmtpControls() {
            const busy = saving || testing;
            smtpForm.querySelectorAll('input, select, button').forEach(control => { control.disabled = !loaded || busy; });
            saveButton.disabled = !loaded || busy;
            testButton.disabled = !loaded || busy || changed || !savedEnabled || !fields.enabled.checked;
            if (state) state.textContent = loadFailed ? 'SMTP settings could not be loaded. Refresh the page before editing or testing.'
                : !loaded ? 'Loading saved SMTP settings...'
                : saving ? 'Saving SMTP settings...'
                : testing ? 'Sending a test email using the saved SMTP settings...'
                : changed ? 'Unsaved SMTP changes. Click Save SMTP settings before testing.'
                : !savedEnabled ? 'SMTP delivery is disabled. Check Enable SMTP delivery, then click Save SMTP settings.'
                : 'SMTP delivery is enabled. Test emails use the saved settings.';
        }
        function populate(config) {
            if (!config || typeof config.enabled !== 'boolean') {
                throw new Error('The panel returned incomplete SMTP settings. Refresh and check the running container version.');
            }
            Object.entries(config).forEach(([key, value]) => {
                if (!fields[key]) return;
                if (fields[key].type === 'checkbox') fields[key].checked = value;
                else fields[key].value = value;
            });
            fields.password.value = ''; fields.clear_password.checked = false;
            document.getElementById('smtp-password-status').textContent = config.password_set ?
                'A password is saved. Leave blank to keep it.' : 'No password saved. Use your provider’s app password if required.';
            loaded = true; loadFailed = false; savedEnabled = config.enabled; changed = false;
            updateSmtpControls();
        }
        function smtpChanged() { if (loaded) { changed = true; updateSmtpControls(); } }
        smtpForm.addEventListener('input', smtpChanged);
        smtpForm.addEventListener('change', smtpChanged);
        bindForm(smtpForm, async () => {
            // Controls are disabled while saving; FormData would omit them.
            const data = {};
            ['host', 'port', 'security', 'username', 'password', 'from_email', 'from_name', 'panel_url']
                .forEach(key => { data[key] = fields[key].value; });
            ['enabled', 'notify_new_users', 'clear_password'].forEach(key => { data[key] = fields[key].checked; });
            const result = await api('/api/settings/smtp', 'PUT', data);
            populate(result.smtp);
            message(savedEnabled ? 'SMTP settings saved. Delivery is enabled; you can now send a test email.'
                : 'SMTP settings saved. Delivery is disabled; no emails will be sent.', false, 'smtp-message');
        }, 'smtp-message', value => { saving = value; updateSmtpControls(); });
        bindForm(testForm, async () => {
            if (!loaded || changed || !savedEnabled || !fields.enabled.checked) {
                throw new Error('Enable SMTP delivery and save the settings before sending a test.');
            }
            message('Sending a test email using the saved SMTP settings...', false, 'smtp-message');
            const result = await api('/api/settings/smtp/test', 'POST', {recipient: testForm.elements.recipient.value});
            if (result.success !== true || typeof result.message !== 'string' || !result.message.trim()) {
                throw new Error('The panel returned no delivery confirmation. Refresh to load the latest panel files, then check the inbox before retrying.');
            }
            message(result.message, false, 'smtp-message');
        }, 'smtp-message', value => { testing = value; updateSmtpControls(); });
        updateSmtpControls();
        api('/api/settings/smtp').then(result => {
            populate(result.smtp);
        }).catch(error => { loadFailed = true; updateSmtpControls(); message(error.message, true, 'smtp-message'); });
    }
})();
