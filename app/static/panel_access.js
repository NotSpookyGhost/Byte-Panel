/* Shared CSRF handling for the panel's existing forms, fetch and ZIP upload XHRs. */
(() => {
    'use strict';
    const token = document.querySelector('meta[name="panel-csrf"]').content;
    const allowed = new Set(JSON.parse(document.getElementById('panel-permissions').textContent));
    window.panelCan = permission => allowed.has(permission);
    const unsafe = method => !['GET', 'HEAD', 'OPTIONS'].includes(String(method || 'GET').toUpperCase());
    const sameOrigin = url => new URL(url, location.href).origin === location.origin;
    const fetchOriginal = window.fetch;
    window.fetch = function (input, options = {}) {
        const url = input instanceof Request ? input.url : input;
        const method = options.method || (input instanceof Request ? input.method : 'GET');
        if (unsafe(method) && sameOrigin(url)) {
            const headers = new Headers(options.headers || (input instanceof Request ? input.headers : {}));
            headers.set('X-Panel-CSRF', token);
            options = {...options, headers};
        }
        return fetchOriginal.call(this, input, options);
    };
    const openOriginal = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        const result = openOriginal.call(this, method, url, ...rest);
        if (unsafe(method) && sameOrigin(url)) this.setRequestHeader('X-Panel-CSRF', token);
        return result;
    };
    function protectForm(form) {
        if (!unsafe(form.method) || !sameOrigin(form.action || location.href)) return;
        let field = form.querySelector('input[name="_csrf"]');
        if (!field) {
            field = document.createElement('input');
            field.type = 'hidden'; field.name = '_csrf'; form.appendChild(field);
        }
        field.value = token;
    }
    document.addEventListener('submit', event => protectForm(event.target), true);
    const submitOriginal = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function () { protectForm(this); return submitOriginal.call(this); };
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('form').forEach(protectForm);
        const applyPermissions = root => {
            root.querySelectorAll('[data-permission]').forEach(element => {
                if (!allowed.has(element.dataset.permission)) {
                    element.hidden = true;
                    element.style.display = 'none';
                    if ('disabled' in element) element.disabled = true;
                }
            });
        };
        applyPermissions(document);
        new MutationObserver(records => {
            if (records.some(record => record.addedNodes.length)) applyPermissions(document);
        }).observe(document.body, {childList: true, subtree: true});
    });
})();
