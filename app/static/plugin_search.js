/* Literal config search shared by the browser editor and its Node tests. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.PluginConfigSearch = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    function findMatches(text, query, matchCase = false) {
        if (!query) return [];
        // Treat config values such as "[world]" literally, not as regex patterns.
        // Regex offsets stay aligned with textarea selections even for Unicode.
        const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), matchCase ? 'gu' : 'giu');
        return Array.from(text.matchAll(pattern), match => ({
            start: match.index, end: match.index + match[0].length,
        }));
    }

    function nextIndex(matches, current, direction, start, end) {
        if (!matches.length) return -1;
        if (current >= 0 && matches[current]
            && matches[current].start === start && matches[current].end === end) {
            return (current + direction + matches.length) % matches.length;
        }
        if (direction > 0) {
            const found = matches.findIndex(match => match.start >= end);
            return found < 0 ? 0 : found;
        }
        for (let index = matches.length - 1; index >= 0; index--) {
            if (matches[index].end <= start) return index;
        }
        return matches.length - 1;
    }

    function attach(editor, controls) {
        const {input, previous, next, matchCase, status} = controls;
        let enabled = false;
        let matches = [];
        let current = -1;

        function update() {
            input.disabled = matchCase.disabled = !enabled;
            previous.disabled = next.disabled = !enabled || !matches.length;
            status.textContent = !enabled ? 'Open a config to search.'
                : !input.value ? 'Type to search.'
                : !matches.length ? 'No matches'
                : current < 0 ? matches.length + (matches.length === 1 ? ' match' : ' matches')
                : (current + 1) + ' of ' + matches.length;
        }
        function refresh() {
            matches = findMatches(editor.value, input.value, matchCase.checked);
            current = -1;
            update();
        }
        function move(direction) {
            if (!enabled || !matches.length) return;
            current = nextIndex(matches, current, direction, editor.selectionStart, editor.selectionEnd);
            const match = matches[current];
            editor.focus({preventScroll: true});
            editor.setSelectionRange(match.start, match.end);
            // A textarea does not reliably scroll to a programmatic selection.
            const style = editor.ownerDocument.defaultView.getComputedStyle(editor);
            const lineHeight = parseFloat(style.lineHeight) || 22.4;
            const before = editor.value.slice(0, match.start);
            const line = before.split('\n').length - 1;
            const columnText = before.slice(before.lastIndexOf('\n') + 1);
            let column = 0;
            for (const char of columnText) column += char === '\t' ? 4 - column % 4 : 1;
            editor.scrollTop = Math.max(0, line * lineHeight - editor.clientHeight / 2 + lineHeight);
            editor.scrollLeft = Math.max(0, column * (parseFloat(style.fontSize) || 14) * .6 - editor.clientWidth / 2);
            update();
        }
        function shortcut(event) {
            if (!enabled) return;
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
                event.preventDefault();
                input.focus();
                input.select();
            } else if (event.key === 'F3' || (event.target === input && event.key === 'Enter')) {
                event.preventDefault();
                move(event.shiftKey ? -1 : 1);
            } else if (event.target === input && event.key === 'Escape') {
                event.preventDefault();
                editor.focus({preventScroll: true});
            }
        }
        input.addEventListener('input', refresh);
        matchCase.addEventListener('change', refresh);
        editor.addEventListener('input', refresh);
        input.addEventListener('keydown', shortcut);
        editor.addEventListener('keydown', shortcut);
        previous.addEventListener('click', () => move(-1));
        next.addEventListener('click', () => move(1));
        update();
        return {refresh, setEnabled(value) { enabled = value; update(); }};
    }

    return {findMatches, nextIndex, attach};
});
