(() => {
    'use strict';
    const manager = document.getElementById('plugin-manager');
    const editor = document.getElementById('plugin-content');
    const list = document.getElementById('plugin-file-list');
    const notice = document.getElementById('plugins-message');
    const stateLabel = document.getElementById('editor-state');
    const saveButton = document.getElementById('save-config');
    const reloadButton = document.getElementById('reload-config');
    const search = window.PluginConfigSearch.attach(editor, {
        input: document.getElementById('config-search'),
        previous: document.getElementById('search-previous'),
        next: document.getElementById('search-next'),
        matchCase: document.getElementById('search-match-case'),
        status: document.getElementById('search-status'),
    });
    let folder = '';
    let selected = null;
    let original = '';
    let revision = '';
    let lineEnding = '\n';
    let busy = false;

    function dirty() { return selected !== null && editor.value !== original; }
    function message(text, error = false) {
        notice.hidden = false;
        notice.dataset.error = String(error);
        notice.textContent = text;
    }
    function updateControls() {
        manager.querySelectorAll('.plugin-interactive').forEach(control => { control.disabled = busy; });
        editor.disabled = busy || selected === null;
        editor.readOnly = Boolean(window.panelCan && !window.panelCan('plugins.manage'));
        saveButton.disabled = busy || selected === null || !dirty() || editor.readOnly;
        reloadButton.disabled = busy || selected === null;
        search.setEnabled(!busy && selected !== null);
        stateLabel.textContent = selected === null ? 'No file selected' : dirty() ? 'Unsaved changes' : 'Saved';
        list.querySelectorAll('tr').forEach(row => { row.classList.toggle('selected', row.dataset.path === selected); });
    }
    async function run(action) {
        if (busy) return;
        busy = true;
        updateControls();
        try { await action(); }
        catch (error) { message(error.message || 'The operation failed. Please try again.', true); }
        finally { busy = false; updateControls(); }
    }
    async function api(url, options = {}) {
        const response = await fetch(url, {
            credentials: 'same-origin', cache: 'no-store', ...options,
            headers: { 'X-Plugin-CSRF-Token': manager.dataset.csrfToken, ...options.headers },
        });
        if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) {
            throw new Error('Your session expired or the request failed. Refresh this page and log in again.');
        }
        const result = await response.json();
        if (!response.ok) {
            const error = new Error(result.error || 'The request failed.');
            error.status = response.status;
            error.data = result;
            throw error;
        }
        return result;
    }
    function confirmDiscard() {
        return !dirty() || window.confirm('Discard your unsaved config changes?');
    }
    function resetEditor() {
        selected = null;
        original = '';
        revision = '';
        editor.value = '';
        search.refresh();
        document.getElementById('editor-path').textContent = 'Select a text config from the file list.';
    }
    function sizeLabel(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KiB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MiB';
    }
    function renderBreadcrumbs() {
        const breadcrumbs = document.getElementById('plugin-breadcrumbs');
        breadcrumbs.replaceChildren();
        const parts = folder ? folder.split('/') : [];
        ['plugins', ...parts].forEach((label, index) => {
            if (index) breadcrumbs.append(document.createTextNode(' / '));
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'plugin-interactive';
            button.textContent = label;
            if (index === parts.length) button.setAttribute('aria-current', 'location');
            button.addEventListener('click', () => navigate(parts.slice(0, index).join('/')));
            breadcrumbs.append(button);
        });
    }
    async function loadFolder(path) {
        const result = await api('/api/plugins/files?path=' + encodeURIComponent(path));
        folder = result.path;
        renderBreadcrumbs();
        document.getElementById('plugin-upload-destination').textContent = 'Data/config destination: plugins/'
            + (folder ? folder + '/' : ' (open a plugin subfolder for data files).');
        list.replaceChildren();
        document.getElementById('plugin-list-status').textContent = result.entries.length
            ? result.entries.length + ' items. Select a folder or config to open it.'
            : folder ? 'This folder is empty. Upload config or data files here.'
                : 'This folder is empty. Upload a plugin, then restart the server to generate its folders.';
        result.entries.forEach(entry => {
            const row = document.createElement('tr');
            row.dataset.path = entry.path;
            const nameCell = document.createElement('td');
            const name = document.createElement('button');
            name.type = 'button';
            name.className = 'file-name plugin-interactive';
            if (entry.kind === 'folder') {
                const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                icon.classList.add('folder-icon');
                icon.setAttribute('viewBox', '0 0 24 24');
                icon.setAttribute('aria-hidden', 'true');
                icon.setAttribute('focusable', 'false');
                const outline = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                outline.setAttribute('d', 'M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z M3 8h18');
                icon.append(outline);
                name.append(icon);
            }
            const label = document.createElement('span');
            label.textContent = entry.name;
            name.append(label);
            name.title = entry.kind === 'folder' ? 'Open folder' : entry.editable ? 'Open config editor' : 'View file details';
            name.addEventListener('click', () => {
                if (entry.kind === 'folder') navigate(entry.path);
                else if (entry.editable) openFile(entry.path);
                else message(entry.name + ' (' + sizeLabel(entry.size) + ') cannot be edited here.'
                    + (entry.name.toLowerCase().endsWith('.jar') ? ' Stop the server before deleting a plugin.' : ' Use Download to view it locally.'));
            });
            const kind = document.createElement('span');
            kind.className = 'file-kind';
            kind.textContent = entry.kind === 'folder' ? 'Folder' : entry.name.toLowerCase().endsWith('.jar') ? 'Plugin JAR' : entry.editable ? 'Text config' : 'Data file (download only)';
            nameCell.append(name, kind);
            const size = document.createElement('td');
            size.textContent = entry.kind === 'folder' ? '\u2014' : sizeLabel(entry.size);
            const actions = document.createElement('td');
            if (entry.kind === 'file' && entry.name.toLowerCase().endsWith('.jar')) {
                if (!window.panelCan || window.panelCan('plugins.manage')) {
                    const remove = document.createElement('button');
                    remove.type = 'button';
                    remove.className = 'plugin-delete plugin-interactive';
                    remove.dataset.permission = 'plugins.manage';
                    remove.textContent = 'Delete';
                    remove.setAttribute('aria-label', 'Delete ' + entry.name);
                    remove.addEventListener('click', () => deletePlugin(entry.path));
                    actions.append(remove);
                }
            } else if (entry.kind === 'file') {
                const download = document.createElement('a');
                download.href = '/api/plugins/download?path=' + encodeURIComponent(entry.path);
                download.textContent = 'Download';
                download.setAttribute('download', entry.name);
                actions.append(download);
            }
            row.append(nameCell, size, actions);
            list.append(row);
        });
    }
    function deletePlugin(path) {
        run(async () => {
            const preview = await api('/api/plugins/delete?path=' + encodeURIComponent(path));
            if (!window.confirm('Delete plugins/' + preview.path + '?\n\nStop the server first. '
                + 'Only this JAR will be removed; config folders will be kept. '
                + 'A recoverable backup will be saved. Restart afterward to unload the plugin.')) return;
            const result = await api('/api/plugins/delete', {
                method: 'DELETE', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: preview.path, revision: preview.revision }),
            });
            message(result.message + '\nBackup: /data/plugin-config-backups/' + result.backup);
            await loadFolder(folder);
        });
    }
    function navigate(path) {
        if (busy || !confirmDiscard()) return;
        run(async () => { await loadFolder(path); resetEditor(); });
    }
    async function loadFile(path) {
        const result = await api('/api/plugins/file?path=' + encodeURIComponent(path));
        selected = result.path;
        revision = result.revision;
        lineEnding = result.content.includes('\r\n') ? '\r\n' : '\n';
        editor.value = result.content;
        original = editor.value;
        search.refresh();
        document.getElementById('editor-path').textContent = 'plugins/' + selected;
    }
    function openFile(path) {
        if (busy || !confirmDiscard()) return;
        run(async () => {
            await loadFile(path);
            notice.hidden = true;
        });
    }
    document.getElementById('refresh-files').addEventListener('click', () => run(() => loadFolder(folder)));
    reloadButton.addEventListener('click', () => { if (selected) openFile(selected); });
    editor.addEventListener('input', updateControls);
    editor.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
            event.preventDefault();
            saveButton.click();
        }
    });
    saveButton.addEventListener('click', () => run(async () => {
        const submitted = editor.value;
        const result = await api('/api/plugins/file', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: selected, content: submitted.replace(/\r?\n/g, lineEnding), revision }),
        });
        original = submitted;
        revision = result.revision;
        message(result.message + ' A backup of the previous file was saved.');
        await loadFolder(folder);
    }));
    document.getElementById('plugin-upload-form').addEventListener('submit', event => {
        event.preventDefault();
        if (window.panelCan && !window.panelCan('plugins.manage')) return;
        if (busy || !confirmDiscard()) return;
        const input = document.getElementById('plugin-upload');
        const files = Array.from(input.files);
        if (!files.length) return;
        run(async () => {
            const destination = folder;
            let uploaded = 0;
            const failures = [];
            resetEditor();
            for (const file of files) {
                message('Uploading ' + file.name + ' (' + (uploaded + failures.length + 1) + '/' + files.length + ')...');
                const data = new FormData();
                data.append('file', file);
                data.append('path', file.name.toLowerCase().endsWith('.jar') ? '' : destination);
                try {
                    try { await api('/api/plugins/upload', { method: 'POST', body: data }); }
                    catch (error) {
                        if (error.status !== 409 || !error.data.conflict) throw error;
                        const target = data.get('path');
                        if (!window.confirm('Replace plugins/' + (target ? target + '/' : '') + file.name
                                + '? Stop the server before replacing files in use. A backup of the existing file will be kept.')) {
                            failures.push(file.name + ': skipped (existing file kept).');
                            continue;
                        }
                        data.append('replace_revision', error.data.revision);
                        await api('/api/plugins/upload', { method: 'POST', body: data });
                    }
                    uploaded += 1;
                } catch (error) { failures.push(file.name + ': ' + error.message); }
            }
            input.value = '';
            await loadFolder(destination);
            message(uploaded + ' file(s) uploaded. Plugin JARs require a restart. For config/data files, follow the plugin import/reload instructions; uploads do not import data automatically.'
                + (failures.length ? '\n' + failures.join('\n') : ''), failures.length > 0);
        });
    });
    window.addEventListener('beforeunload', event => {
        if (dirty() || busy) { event.preventDefault(); event.returnValue = ''; }
    });
    run(() => loadFolder(''));
})();
