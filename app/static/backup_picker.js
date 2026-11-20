(() => {
    'use strict';
    const byId = id => document.getElementById(id);
    const dialog = byId('backupPicker');
    const saved = byId('backupExclusions');
    const draft = byId('backupPathDraft');
    const list = byId('backupPickerEntries');
    const status = byId('backupPickerStatus');
    const preset = byId('addBlueMapExclusion');
    let folder = '';
    let request;
    let listingMessage = '';

    function paths(value) {
        const result = new Set();
        for (const line of value.split('\n')) {
            const path = line.trim().replace(/\\/g, '/');
            if (path) result.add(path.replace(/\/+$/, '') || path);
        }
        return result;
    }

    function isExcluded(path, excluded) {
        while (path) {
            if (excluded.has(path)) return true;
            const slash = path.lastIndexOf('/');
            if (slash < 0) break;
            path = path.slice(0, slash);
        }
        return false;
    }

    function summary() {
        const count = paths(saved.value).size;
        byId('backupSelectionSummary').textContent = count
            ? count + (count === 1 ? ' path excluded.' : ' paths excluded.')
            : 'All server files are included.';
    }

    function updateChecks() {
        const excluded = paths(draft.value);
        const mixed = new Set();
        for (const path of excluded) {
            for (let end = path.lastIndexOf('/'); end > 0; end = path.lastIndexOf('/', end - 1)) {
                mixed.add(path.slice(0, end));
            }
        }
        const inherited = isExcluded(folder, excluded);
        for (const box of list.querySelectorAll('input')) {
            const path = box.dataset.path;
            box.checked = !inherited && !excluded.has(path);
            box.indeterminate = box.checked && box.dataset.kind === 'folder' && mixed.has(path);
            box.disabled = inherited || box.dataset.selectable === 'false';
        }
        status.textContent = [listingMessage, inherited && 'This folder is excluded. Include it from its parent folder to select individual items.'].filter(Boolean).join(' ');
        byId('backupPickerCount').textContent = '(' + excluded.size + ')';
        preset.disabled = isExcluded(preset.value, excluded);
        preset.textContent = preset.disabled ? 'BlueMap renders excluded' : 'Exclude BlueMap renders';
    }

    function setIncluded(path, include) {
        const excluded = paths(draft.value);
        for (const item of excluded) {
            if (item === path || item.startsWith(path + '/')) excluded.delete(item);
        }
        if (!include) excluded.add(path);
        draft.value = [...excluded].join('\n');
        updateChecks();
    }

    function row(entry, index) {
        const item = document.createElement('li');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.id = 'backupPickerItem-' + index;
        box.dataset.path = entry.path;
        box.dataset.kind = entry.kind;
        box.dataset.selectable = String(entry.selectable);
        box.setAttribute('aria-label', 'Include ' + entry.path);
        box.addEventListener('change', () => setIncluded(entry.path, box.checked));
        const name = document.createElement(entry.kind === 'folder' ? 'button' : 'label');
        name.className = 'picker-entry';
        if (entry.kind === 'folder') {
            name.type = 'button';
            name.append(byId('backupFolderIcon').content.cloneNode(true));
            name.addEventListener('click', () => openFolder(entry.path));
        } else {
            name.htmlFor = box.id;
        }
        name.append(document.createTextNode(entry.name));
        item.append(box, name);
        if (!entry.selectable) {
            const note = document.createElement('small');
            note.textContent = 'Cannot be excluded individually';
            item.append(note);
        }
        return item;
    }

    async function openFolder(path, focusPath = true) {
        request?.abort();
        const current = new AbortController();
        request = current;
        folder = path;
        const heading = byId('backupPickerPath');
        heading.textContent = path ? 'server/' + path + '/' : 'server/';
        if (focusPath) heading.focus();
        byId('backupPickerUp').disabled = !path;
        byId('backupPickerRetry').hidden = true;
        list.replaceChildren();
        listingMessage = 'Loading folder…';
        updateChecks();
        try {
            const response = await fetch('/api/backup/exclusions/directory?path=' + encodeURIComponent(path),
                {signal: current.signal, headers: {Accept: 'application/json'}});
            const data = await response.json().catch(() => ({}));
            if (!response.ok || !Array.isArray(data.entries)) {
                throw new Error(data.error || 'Could not list this folder. Refresh and sign in again if needed.');
            }
            if (current.signal.aborted) return;
            list.replaceChildren(...data.entries.map(row));
            listingMessage = data.skipped ? 'Some entries could not be listed.' : '';
            if (!data.entries.length && !data.skipped) listingMessage = 'This folder is empty.';
        } catch (error) {
            if (current.signal.aborted) return;
            listingMessage = error.message || 'Could not load this folder.';
            byId('backupPickerRetry').hidden = false;
        }
        updateChecks();
    }

    byId('openBackupPicker').addEventListener('click', () => {
        draft.value = saved.value;
        byId('backupPathDetails').open = false;
        dialog.showModal();
        openFolder('', false);
    });
    byId('backupPickerUp').addEventListener('click', () => openFolder(folder.split('/').slice(0, -1).join('/')));
    byId('backupPickerRetry').addEventListener('click', () => openFolder(folder));
    preset.addEventListener('click', () => setIncluded(preset.value, false));
    draft.addEventListener('input', updateChecks);
    byId('backupPickerCancel').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => request?.abort());
    byId('backupPickerApply').addEventListener('click', () => {
        saved.value = [...paths(draft.value)].join('\n');
        saved.dispatchEvent(new Event('input', {bubbles: true}));
        summary();
        dialog.close();
    });
    byId('btnRevertChanges').addEventListener('click', () => setTimeout(summary, 0));
    summary();
})();
