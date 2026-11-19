/* Native textarea editing with an inert, bounded syntax-coloring layer.
 * This is a highlighter, not a parser or validator. No source is treated as HTML.
 */
(function (root, factory) {
    'use strict';
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.PanelConfigEditor = api;
})(typeof window === 'undefined' ? null : window, function () {
    'use strict';

    const MAX_LENGTH = 1024 * 1024;
    const MAX_LINE = 16 * 1024;
    const MAX_TOKENS = 16384;
    const DEBOUNCE_MS = 80;
    const instances = new WeakMap();
    const languages = {
        yaml: 'yaml', yml: 'yaml', json: 'json', json5: 'json5',
        properties: 'properties', toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini', xml: 'xml'
    };
    const number = /^[+-]?(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)(?:[eE][+-]?[0-9_]+)?|Infinity|NaN)$/;
    const literal = /^(?:true|false|null|nil|none|yes|no|on|off|~)$/i;
    const wordCharacter = /[A-Za-z0-9_.$/@%+~\\-]/;
    const whitespace = /\s/;

    function languageFor(path) {
        const filename = String(path || '').replace(/\\/g, '/').split('/').pop();
        const dot = filename.lastIndexOf('.');
        return dot < 0 ? 'plain' : languages[filename.slice(dot + 1).toLowerCase()] || 'plain';
    }

    function plain(text, language, reason) {
        return {language, tokens: text ? [{type: null, text}] : [], highlighted: false, reason};
    }

    function quoteEnd(text, start, language) {
        const quote = text[start];
        const triple = language === 'toml' && text.slice(start, start + 3) === quote.repeat(3);
        const delimiter = triple ? quote.repeat(3) : quote;
        let index = start + delimiter.length;
        while (index < text.length) {
            if (text[index] === '\\' && !(quote === "'" && (language === 'yaml' || language === 'toml'))) {
                index += Math.min(2, text.length - index);
            } else if (language === 'yaml' && quote === "'" && text.slice(index, index + 2) === "''") {
                index += 2;
            } else if (text.slice(index, index + delimiter.length) === delimiter) {
                return index + delimiter.length;
            } else {
                index++;
            }
        }
        return text.length;
    }

    function nextNonSpace(text, index) {
        // Keys cannot acquire a delimiter from a different line.
        while (index < text.length && (text[index] === ' ' || text[index] === '\t')) index++;
        return text[index];
    }

    function lineEnd(text, index) {
        while (index < text.length && text[index] !== '\n' && text[index] !== '\r') index++;
        return index;
    }

    function tokenize(source, path) {
        const text = typeof source === 'string' ? source : String(source == null ? '' : source);
        const language = languageFor(path);
        if (language === 'plain') return plain(text, language, 'plain-text');
        if (text.length > MAX_LENGTH) return plain(text, language, 'large-file');
        let width = 0;
        let bytes = 0;
        for (let index = 0; index < text.length; index++) {
            width = text[index] === '\n' || text[index] === '\r' ? 0 : width + 1;
            if (width > MAX_LINE) return plain(text, language, 'long-line');
            const point = text.charCodeAt(index);
            if (point < 0x80) bytes++;
            else if (point < 0x800) bytes += 2;
            else if (point >= 0xd800 && point <= 0xdbff &&
                     text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) {
                bytes += 4;
                width++;
                index++;
            } else bytes += 3;
            if (bytes > MAX_LENGTH) return plain(text, language, 'large-file');
        }

        const tokens = [];
        let exhausted = false;
        function emit(start, end, type) {
            if (end <= start) return;
            const previous = tokens[tokens.length - 1];
            if (previous && previous.type === type) previous.end = end;
            else if (tokens.length < MAX_TOKENS) tokens.push({type, start, end});
            else exhausted = true;
        }

        let index = 0;
        let firstOnLine = true;
        let propertyValue = false;
        let insideTag = false;
        let tagName = false;
        while (index < text.length && !exhausted) {
            const start = index;
            const char = text[index];
            if (whitespace.test(char)) {
                do {
                    if (text[index] === '\n' || text[index] === '\r') {
                        firstOnLine = true;
                        propertyValue = false;
                    }
                    index++;
                } while (index < text.length && whitespace.test(text[index]));
                emit(start, index, null);
                continue;
            }

            if (language === 'xml') {
                if (text.startsWith('<!--', index)) {
                    const end = text.indexOf('-->', index + 4);
                    index = end < 0 ? text.length : end + 3;
                    emit(start, index, 'comment');
                } else if (text.startsWith('<![CDATA[', index)) {
                    const end = text.indexOf(']]>', index + 9);
                    index = end < 0 ? text.length : end + 3;
                    emit(start, index, 'string');
                } else if (char === '<') {
                    index += text[index + 1] === '/' || text[index + 1] === '?' || text[index + 1] === '!' ? 2 : 1;
                    insideTag = true;
                    tagName = true;
                    emit(start, index, 'punctuation');
                } else if (insideTag && (char === '>' || (char === '/' || char === '?') && text[index + 1] === '>')) {
                    index += char === '>' ? 1 : 2;
                    insideTag = false;
                    emit(start, index, 'punctuation');
                } else if (insideTag && (char === '"' || char === "'")) {
                    index = quoteEnd(text, index, language);
                    emit(start, index, 'string');
                } else if (insideTag && char === '=') {
                    index++;
                    emit(start, index, 'punctuation');
                } else if (insideTag && /[A-Za-z_:]/.test(char)) {
                    index++;
                    while (index < text.length && /[A-Za-z0-9_.:-]/.test(text[index])) index++;
                    emit(start, index, tagName ? 'tag' : 'attribute');
                    tagName = false;
                } else if (char === '&') {
                    index++;
                    while (index < text.length && /[A-Za-z0-9#]/.test(text[index])) index++;
                    if (text[index] === ';') index++;
                    emit(start, index, 'entity');
                } else {
                    index++;
                    emit(start, index, null);
                }
                firstOnLine = false;
                continue;
            }

            const json = language === 'json' || language === 'json5';
            const hashComment = char === '#' && (language === 'toml' ||
                language === 'yaml' && (firstOnLine || whitespace.test(text[index - 1] || '')) ||
                language === 'ini' && (firstOnLine || whitespace.test(text[index - 1] || '')));
            const propertyComment = language === 'properties' && firstOnLine && (char === '#' || char === '!');
            const iniComment = language === 'ini' && char === ';' && (firstOnLine || whitespace.test(text[index - 1] || ''));
            const slashComment = (json || language === 'ini') && text.startsWith('//', index);
            if (hashComment || propertyComment || iniComment || slashComment) {
                index = lineEnd(text, index);
                emit(start, index, 'comment');
                firstOnLine = false;
                continue;
            }
            if (json && text.startsWith('/*', index)) {
                const end = text.indexOf('*/', index + 2);
                index = end < 0 ? text.length : end + 2;
                emit(start, index, 'comment');
                firstOnLine = false;
                continue;
            }

            if (language === 'properties' && firstOnLine) {
                while (index < text.length && !whitespace.test(text[index]) && text[index] !== '=' && text[index] !== ':') {
                    if (text[index] === '\\' && index + 1 < text.length) index += 2;
                    else index++;
                }
                if (index > start) {
                    emit(start, index, 'key');
                    firstOnLine = false;
                    propertyValue = true;
                    continue;
                }
            }
            if ((language === 'ini' || language === 'toml') && firstOnLine && char === '[') {
                index++;
                while (index < text.length && text[index] !== '\r' && text[index] !== '\n') {
                    if (text[index++] === ']') {
                        if (text[index] === ']') index++;
                        break;
                    }
                }
                emit(start, index, 'section');
                firstOnLine = false;
                continue;
            }
            if (char === '"' || char === "'") {
                index = quoteEnd(text, index, language);
                const separator = nextNonSpace(text, index);
                emit(start, index, separator === ':' || separator === '=' ? 'key' : 'string');
                firstOnLine = false;
                continue;
            }
            if ('{}[],:=()'.includes(char)) {
                index++;
                emit(start, index, 'punctuation');
                firstOnLine = false;
                continue;
            }
            if (language === 'yaml' && firstOnLine && char === '-' && whitespace.test(text[index + 1] || '')) {
                index++;
                emit(start, index, 'punctuation');
                continue;
            }
            if (language === 'yaml' && '&*!'.includes(char)) {
                index++;
                while (index < text.length && /[A-Za-z0-9_.:/-]/.test(text[index])) index++;
                emit(start, index, 'anchor');
                firstOnLine = false;
                continue;
            }
            if (wordCharacter.test(char)) {
                index++;
                while (index < text.length && wordCharacter.test(text[index])) index++;
                const word = text.slice(start, index);
                const separator = nextNonSpace(text, index);
                const isKey = (separator === ':' || separator === '=') && !(language === 'properties' && propertyValue);
                const type = isKey ? 'key' : literal.test(word) ? 'literal' :
                    number.test(word) ? 'number' : language === 'properties' && propertyValue ? 'string' : null;
                emit(start, index, type);
            } else {
                index++;
                emit(start, index, null);
            }
            firstOnLine = false;
        }
        if (exhausted) return plain(text, language, 'token-limit');
        return {language, tokens: tokens.map(token => ({type: token.type,
            text: text.slice(token.start, token.end)})), highlighted: true, reason: ''};
    }

    function attach(textarea) {
        if (!textarea || String(textarea.tagName).toLowerCase() !== 'textarea' || !textarea.parentNode) {
            throw new TypeError('PanelConfigEditor requires a textarea with a parent.');
        }
        if (instances.has(textarea)) return instances.get(textarea);
        const document = textarea.ownerDocument;
        const view = document.defaultView || globalThis;
        const wrapper = document.createElement('div');
        const overlay = document.createElement('pre');
        const code = document.createElement('code');
        const status = document.createElement('div');
        wrapper.className = 'panel-config-editor';
        overlay.className = 'panel-config-editor-highlight';
        code.className = 'panel-config-editor-code';
        status.className = 'panel-config-editor-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.hidden = true;
        overlay.setAttribute('aria-hidden', 'true');
        overlay.setAttribute('inert', '');
        code.setAttribute('aria-hidden', 'true');
        overlay.appendChild(code);
        const original = view.getComputedStyle ? view.getComputedStyle(textarea) : {};
        wrapper.style.setProperty('--config-editor-text', original.color || '#e6edf3');
        wrapper.style.setProperty('--config-editor-background', original.backgroundColor || '#0d1117');
        textarea.parentNode.insertBefore(wrapper, textarea);
        wrapper.appendChild(textarea);
        wrapper.appendChild(overlay);
        wrapper.appendChild(status);
        textarea.classList.add('panel-config-editor-input');
        textarea.wrap = 'off';

        let path = '';
        let timer = null;
        let composing = false;
        let active = false;
        let fallback = 'plain-text';
        const setTimer = view.setTimeout ? view.setTimeout.bind(view) : setTimeout;
        const clearTimer = view.clearTimeout ? view.clearTimeout.bind(view) : clearTimeout;

        function paintMode(enabled, reason) {
            wrapper.classList.toggle('is-highlighted', enabled);
            wrapper.dataset.highlighted = enabled ? 'true' : 'false';
            wrapper.dataset.fallback = enabled ? '' : reason || fallback;
        }

        function syncScroll() {
            overlay.scrollTop = textarea.scrollTop;
            overlay.scrollLeft = textarea.scrollLeft;
        }

        function fit() {
            const style = view.getComputedStyle ? view.getComputedStyle(textarea) : {};
            for (const property of ['fontFamily', 'fontSize', 'fontStyle', 'fontWeight', 'fontVariant',
                'fontVariantLigatures', 'fontFeatureSettings', 'fontKerning', 'lineHeight', 'letterSpacing',
                'wordSpacing', 'textIndent', 'textTransform', 'textRendering', 'tabSize', 'direction',
                'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft']) {
                if (style[property] != null) overlay.style[property] = style[property];
            }
            overlay.style.top = (textarea.offsetTop + textarea.clientTop) + 'px';
            overlay.style.left = (textarea.offsetLeft + textarea.clientLeft) + 'px';
            overlay.style.width = textarea.clientWidth + 'px';
            overlay.style.height = textarea.clientHeight + 'px';
            const visible = textarea.clientWidth > 0 && textarea.clientHeight > 0;
            paintMode(active && visible && !composing && timer === null, visible ? fallback : 'hidden');
            syncScroll();
        }

        function cancelPaint() {
            if (timer !== null) {
                clearTimer(timer);
                timer = null;
            }
        }

        function refresh() {
            cancelPaint();
            const result = tokenize(textarea.value, path);
            active = result.highlighted;
            fallback = result.reason;
            wrapper.dataset.language = result.language;
            const notices = {
                'plain-text': 'Plain-text editing: syntax colors are not available for this file type.',
                'large-file': 'Plain-text editing: syntax colors are disabled for files larger than 1 MiB to keep editing responsive.',
                'long-line': 'Plain-text editing: syntax colors are disabled because this file contains a very long line.',
                'token-limit': 'Plain-text editing: syntax colors are disabled because this file is especially complex.'
            };
            const message = path ? notices[fallback] || '' : '';
            if (status.textContent !== message) status.textContent = message;
            status.hidden = !message;
            wrapper.title = message;
            if (active) {
                const fragment = document.createDocumentFragment();
                for (const token of result.tokens) {
                    if (token.type) {
                        const span = document.createElement('span');
                        span.className = 'config-token-' + token.type;
                        span.appendChild(document.createTextNode(token.text));
                        fragment.appendChild(span);
                    } else {
                        fragment.appendChild(document.createTextNode(token.text));
                    }
                }
                code.replaceChildren(fragment);
            } else {
                // Large/plain files need no duplicate DOM text or layout.
                code.replaceChildren();
            }
            fit();
        }

        function schedule() {
            cancelPaint();
            // Show the up-to-date native text while rendering is debounced.
            // This also preserves IME composition and never leaves typing invisible.
            paintMode(false, 'editing');
            if (!composing) {
                timer = setTimer(function () {
                    timer = null;
                    refresh();
                }, DEBOUNCE_MS);
            }
        }

        function resetPosition(options) {
            if (options && options.focus) {
                try { textarea.focus({preventScroll: true}); } catch (error) { textarea.focus(); }
            }
            textarea.setSelectionRange(0, 0);
            textarea.scrollTop = 0;
            textarea.scrollLeft = 0;
            syncScroll();
        }

        const api = {
            setFile: function (filename) {
                path = String(filename || '');
                refresh();
                resetPosition();
            },
            refresh,
            resetPosition,
            syncScroll
        };
        instances.set(textarea, api);
        textarea.addEventListener('input', schedule);
        textarea.addEventListener('compositionstart', function () { composing = true; schedule(); });
        textarea.addEventListener('compositionend', function () { composing = false; schedule(); });
        textarea.addEventListener('scroll', syncScroll, {passive: true});
        if (view.ResizeObserver) {
            const observer = new view.ResizeObserver(fit);
            observer.observe(textarea);
        } else if (view.addEventListener) {
            view.addEventListener('resize', fit, {passive: true});
            // Native drag-resize support on older browsers without ResizeObserver.
            textarea.addEventListener('mouseup', fit);
        }
        refresh();
        return api;
    }

    return Object.freeze({attach, tokenize, languageFor,
        limits: Object.freeze({maxLength: MAX_LENGTH, maxLine: MAX_LINE, maxTokens: MAX_TOKENS})});
});
