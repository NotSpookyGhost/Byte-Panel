/* Server-scoped files. No HTML from filenames or API messages is interpreted. */
(() => {
    'use strict';
    const byId = id => document.getElementById(id);
    const manager = byId('file-manager');
    const editor = byId('files-content');
    const syntax = window.PanelConfigEditor ? window.PanelConfigEditor.attach(editor) : null;
    const notice = byId('files-message');
    const list = byId('files-list');
    const canManage = !window.panelCan || window.panelCan('files.manage');
    const search = window.PluginConfigSearch.attach(editor, {
        input: byId('files-search'), previous: byId('files-search-previous'),
        next: byId('files-search-next'), matchCase: byId('files-search-case'),
        status: byId('files-search-status')
    });
    let folder = '', entries = [], selected = null, original = '', revision = '', lineEnding = '\n', busy = false;
    const dirty = () => selected !== null && editor.value !== original;
    function message(text, error = false) {
        notice.textContent = text; notice.dataset.error = String(error); notice.hidden = false;
    }
    function controls() {
        manager.querySelectorAll('.file-interactive').forEach(node => {
            node.disabled = busy || (node.dataset.permission === 'files.manage' && !canManage);
        });
        editor.disabled = busy || selected === null;
        editor.readOnly = !canManage;
        byId('files-save').disabled = busy || !canManage || !dirty();
        byId('files-reload').disabled = busy || selected === null;
        byId('files-close').disabled = busy || selected === null;
        byId('files-editor-state').textContent = selected === null ? 'No file selected' : dirty() ? 'Unsaved changes' : 'Saved';
        search.setEnabled(!busy && selected !== null);
    }
    async function run(action) {
        if (busy) return;
        busy = true; controls();
        try { await action(); }
        catch (error) { message(error.message || 'Unable to complete this file operation.', true); }
        finally { busy = false; controls(); }
    }
    async function api(url, options = {}) {
        const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
        if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) {
            throw new Error('The panel returned an unexpected response. Refresh the page and sign in if needed.');
        }
        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.error || data.message || 'The file request failed.');
            error.data = data; error.status = response.status; throw error;
        }
        return data;
    }
    const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    function discard() { return !dirty() || window.confirm('Discard unsaved file changes?'); }
    function clearEditor() {
        selected = null; original = ''; revision = ''; editor.value = '';
        if (syntax) syntax.setFile('');
        resetPosition();
        byId('files-editor-path').textContent = ''; search.refresh(); controls();
    }
    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KiB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MiB';
    }
    function button(label, action, danger = false) {
        const node = document.createElement('button');
        node.type = 'button'; node.className = 'btn ' + (danger ? 'btn-stop' : 'btn-outline') + ' file-interactive';
        node.textContent = label; node.addEventListener('click', () => run(action)); return node;
    }
    function folderIcon() {
        const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        icon.setAttribute('class', 'folder-icon'); icon.setAttribute('viewBox', '0 0 24 24');
        icon.setAttribute('aria-hidden', 'true');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z M3 8h18');
        icon.append(path); return icon;
    }
    function render() {
        list.replaceChildren();
        const filter = byId('files-filter').value.toLocaleLowerCase();
        const shown = entries.filter(item => item.name.toLocaleLowerCase().includes(filter));
        byId('files-list-status').textContent = shown.length + ' of ' + entries.length + ' entries';
        for (const entry of shown) {
            const row = document.createElement('tr'); row.dataset.path = entry.path;
            const nameCell = document.createElement('td');
            const name = button('', async () => {
                if (entry.kind === 'folder') await navigate(entry.path);
                else if (entry.editable) await openFile(entry.path);
                else message('Use Download for this binary or large file: ' + entry.path);
            });
            name.className = 'file-name file-interactive';
            if (entry.kind === 'folder') name.append(folderIcon());
            const label = document.createElement('span'); label.textContent = entry.name; name.append(label);
            nameCell.append(name);
            const size = document.createElement('td'); size.textContent = entry.kind === 'folder' ? 'Folder' : formatSize(entry.size);
            const cell = document.createElement('td'), actions = document.createElement('div'); actions.className = 'files-actions';
            if (entry.kind === 'file') {
                const link = document.createElement('a');
                link.textContent = 'Download'; link.href = '/api/files/download?path=' + encodeURIComponent(entry.path);
                actions.append(link);
            }
            if (canManage) {
                actions.append(button('Rename / move', () => moveEntry(entry)));
                actions.append(button('Delete', () => deleteEntry(entry), true));
            }
            cell.append(actions); row.append(nameCell, size, cell); list.append(row);
        }
        controls();
    }
    function breadcrumbs() {
        const nav = byId('files-breadcrumbs'); nav.replaceChildren();
        nav.append(button('server/', () => navigate('')));
        const parts = folder ? folder.split('/') : [];
        for (let index = 0; index < parts.length; index += 1) {
            const path = parts.slice(0, index + 1).join('/');
            const separator = document.createElement('span'); separator.textContent = '/'; nav.append(separator);
            nav.append(button(parts[index], () => navigate(path)));
        }
    }
    async function loadFolder(path = folder) {
        const data = await api('/api/files/list?path=' + encodeURIComponent(path));
        folder = data.path; entries = data.entries; breadcrumbs(); render();
    }
    async function navigate(path) {
        if (!discard()) return;
        await loadFolder(path); clearEditor(); byId('files-filter').value = ''; render();
        const url = new URL(window.location.href);
        if (folder) url.searchParams.set('path', folder); else url.searchParams.delete('path');
        window.history.replaceState(null, '', url.pathname + url.search);
    }
    function resetPosition() {
        editor.setSelectionRange(0, 0);
        editor.scrollTop = 0;
        editor.scrollLeft = 0;
        if (syntax) syntax.syncScroll();
    }
    async function openFile(path) {
        if (!discard()) return;
        const data = await api('/api/files/file?path=' + encodeURIComponent(path));
        selected = data.path; lineEnding = data.content.includes('\r\n') ? '\r\n' : '\n';
        editor.value = data.content; original = editor.value; revision = data.revision;
        if (syntax) syntax.setFile(selected);
        resetPosition();
        byId('files-editor-path').textContent = 'server/' + selected; search.refresh(); controls();
    }
    function recovery(data) {
        return data.recovery_id ? ' Recovery copy: /data/file-manager-' + (data.recovery_kind === 'trash' ? 'trash' : 'backups') + '/' + data.recovery_id : '';
    }
    async function createEntry(kind) {
        if (!canManage) return;
        const name = window.prompt(kind === 'mkdir' ? 'New folder name:' : 'New text filename (for example notes.txt):');
        if (!name) return;
        const data = await api('/api/files/' + kind, json('POST', {path: folder, name, ...(kind === 'create' ? {content: ''} : {})}));
        await loadFolder(); message(data.message);
    }
    function affectsSelected(path) { return selected !== null && (selected === path || selected.startsWith(path + '/')); }
    async function moveEntry(entry) {
        if (!canManage || (affectsSelected(entry.path) && !discard())) return;
        const preview = await api('/api/files/entry?path=' + encodeURIComponent(entry.path));
        const destination = window.prompt('New path relative to server/ (the destination folder must exist):', entry.path);
        if (!destination || destination === entry.path) return;
        const data = await api('/api/files/move', json('POST', {path: entry.path, destination, revision: preview.revision}));
        if (affectsSelected(entry.path)) clearEditor();
        await loadFolder(); message(data.message);
    }
    async function deleteEntry(entry) {
        if (!canManage || (affectsSelected(entry.path) && !discard())) return;
        const preview = await api('/api/files/entry?path=' + encodeURIComponent(entry.path));
        const confirmation = window.prompt('Move ' + (entry.kind === 'folder' ? 'this folder and ALL its contents' : 'this file') +
            ' to the recovery folder?\nType the exact path to confirm:\n' + entry.path);
        if (confirmation === null) return;
        if (confirmation !== entry.path) { message('The path did not match. Nothing was deleted.', true); return; }
        const data = await api('/api/files/entry', json('DELETE', {path: entry.path, revision: preview.revision, confirmation}));
        if (affectsSelected(entry.path)) clearEditor();
        await loadFolder(); message(data.message + recovery(data));
    }
    async function upload() {
        if (!canManage || !discard()) return;
        const files = Array.from(byId('files-upload').files);
        let completed = 0;
        let failure = '';
        try {
            for (const file of files) {
                if (file.size > 256 * 1024 * 1024) throw new Error(file.name + ' exceeds the 256 MiB upload limit.');
                message('Uploading ' + file.name + ' (' + (completed + 1) + '/' + files.length + ')...');
                const body = new FormData(); body.append('file', file); body.append('path', folder);
                let result;
                try { result = await api('/api/files/upload', {method: 'POST', body}); }
                catch (error) {
                    if (!error.data || !error.data.conflict || !error.data.revision) throw error;
                    if (!window.confirm('Replace ' + file.name + '? The old file will be backed up.')) continue;
                    body.append('replace_revision', error.data.revision);
                    result = await api('/api/files/upload', {method: 'POST', body});
                }
                completed += 1; if (selected === result.path) clearEditor();
            }
            byId('files-upload').value = '';
        } catch (error) {
            failure = error.message;
        }
        try { await loadFolder(); }
        catch (error) {
            failure += (failure ? ' ' : '') + 'The file list could not be refreshed: ' + error.message;
        }
        if (failure) throw new Error(completed + ' file(s) uploaded. ' + failure);
        message(completed + ' file(s) uploaded.');
    }
    byId('files-upload-form').addEventListener('submit', event => { event.preventDefault(); run(upload); });
    byId('files-new-folder').addEventListener('click', () => run(() => createEntry('mkdir')));
    byId('files-new-file').addEventListener('click', () => run(() => createEntry('create')));
    byId('files-refresh').addEventListener('click', () => run(() => loadFolder()));
    byId('files-filter').addEventListener('input', render);
    byId('files-content').addEventListener('input', controls);
    byId('files-close').addEventListener('click', () => { if (!busy && discard()) clearEditor(); });
    byId('files-reload').addEventListener('click', () => run(() => openFile(selected)));
    byId('files-save').addEventListener('click', () => run(async () => {
        if (!canManage || !dirty()) return;
        const content = editor.value.replace(/\r?\n/g, lineEnding);
        const data = await api('/api/files/file', json('PUT', {path: selected, content, revision}));
        original = editor.value; revision = data.revision;
        message(data.message + recovery(data)); await loadFolder();
    }));
    window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
    run(() => loadFolder(new URLSearchParams(window.location.search).get('path') || ''));
})();
