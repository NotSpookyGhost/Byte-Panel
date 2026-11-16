/* Rules come from the server's validator. Only new-password fields opt in. */
(() => {
    'use strict';
    document.querySelectorAll('[data-password-rules]').forEach(field => {
        const rules = JSON.parse(field.dataset.passwordRules).map(([pattern, label]) => [new RegExp(pattern), label]);
        const checklist = field.dataset.passwordChecklist ? document.getElementById(field.dataset.passwordChecklist) : null;
        const checks = checklist ? [...checklist.querySelectorAll('[data-password-check]')] : [];
        function updateChecklist(value) {
            const length = Array.from(value).length;
            checks.forEach(item => {
                const key = item.dataset.passwordCheck;
                const met = key === 'length' ? length >= 12 && length <= 256
                    : Boolean(value && rules[Number(key)]?.[0].test(value));
                // Announce only state changes, not every keystroke. Never put the password in the DOM.
                if (item.dataset.met === String(met)) return;
                item.dataset.met = String(met);
                item.querySelector('.password-check-icon').textContent = met ? '\u2713' : '\u25cb';
                item.querySelector('.password-check-state').textContent = met ? 'Met' : 'Not yet met';
            });
        }
        function validate() {
            const value = field.value;
            const length = Array.from(value).length;
            let error = '';
            if (value && (length < 12 || length > 256)) {
                error = 'Use a password between 12 and 256 characters.';
            } else if (value) {
                const missing = rules.filter(([pattern]) => !pattern.test(value)).map(([, label]) => label);
                if (missing.length) error = 'Password must include ' + missing.join(', ') + '.';
            }
            // Empty fields use the browser's built-in "required" message.
            field.setCustomValidity(error);
            updateChecklist(value);
        }
        ['input', 'change', 'invalid'].forEach(event => field.addEventListener(event, validate));
        if (field.form) field.form.addEventListener('reset', () => {
            field.setCustomValidity('');
            updateChecklist('');
        });
        validate();
    });
})();
