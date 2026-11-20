/* Focused settings panels, with all fields retained in their original forms. */
(function () {
    'use strict';
    const tabs = Array.from(document.querySelectorAll('[data-settings-tab]'));
    const panels = Array.from(document.querySelectorAll('[data-settings-panel]'));
    const form = document.getElementById('settingsForm');
    if (!tabs.length || !form) return;
    function select(key, focus) {
        if (!tabs.some(tab => tab.dataset.settingsTab === key)) key = 'general';
        tabs.forEach(tab => {
            const active = tab.dataset.settingsTab === key;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            if (active && focus) tab.focus();
        });
        panels.forEach(panel => { panel.hidden = panel.dataset.settingsPanel !== key; });
        form.hidden = ['email', 'operations'].includes(key);
        document.getElementById('settings-main-card').hidden = key === 'email';
        try { sessionStorage.setItem('byte-panel-settings-section', key); } catch (_) {}
    }
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => select(tab.dataset.settingsTab));
        tab.addEventListener('keydown', event => {
            let target;
            if (event.key === 'ArrowRight') target = (index + 1) % tabs.length;
            if (event.key === 'ArrowLeft') target = (index + tabs.length - 1) % tabs.length;
            if (event.key === 'Home') target = 0;
            if (event.key === 'End') target = tabs.length - 1;
            if (target !== undefined) { event.preventDefault(); select(tabs[target].dataset.settingsTab, true); }
        });
    });
    form.addEventListener('invalid', event => {
        const panel = event.target.closest('[data-settings-panel]');
        if (panel) select(panel.dataset.settingsPanel);
    }, true);
    // Accessible names for the existing controls whose labels predate explicit IDs.
    document.querySelectorAll('.settings-section input, .settings-section select').forEach((control, index) => {
        if (!control.id) control.id = 'setting-control-' + index;
        if (control.closest('label')) {
            if (!control.getAttribute('aria-label')) {
                const group = control.closest('label').parentElement.parentElement;
                const label = group.querySelector('label');
                if (label && label.textContent.trim()) control.setAttribute('aria-label', label.textContent.trim());
            }
            return;
        }
        const label = control.parentElement.querySelector('label');
        if (label && !label.htmlFor) label.htmlFor = control.id;
    });
    let selected = 'general';
    try { selected = sessionStorage.getItem('byte-panel-settings-section') || selected; } catch (_) {}
    select(selected);
}());

