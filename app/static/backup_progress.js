/* Creation is a short POST; only an explicit worker result completes the backup. */
(() => {
    'use strict';
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    function failure(message, retryable = false) {
        const error = new Error(message);
        error.retryable = retryable;
        return error;
    }
    async function request(url, options = {}) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {
                ...options, cache: 'no-store', signal: controller.signal,
                headers: {Accept: 'application/json', ...options.headers}
            });
            if (response.redirected || response.status === 401) {
                throw failure('Your session expired. Sign in again and check the backup list before retrying.');
            }
            if (response.status === 403) {
                throw failure('Backup access denied or your session expired. Refresh the page and check your backup permissions.');
            }
            let data;
            try {
                if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error();
                data = await response.json();
                if (!data || typeof data !== 'object') throw new Error();
            } catch (_) {
                throw failure('The panel or proxy returned an unreadable response.', true);
            }
            if (!response.ok) {
                throw failure(data.message || data.error || 'Backup request failed (HTTP ' + response.status + ').',
                    response.status >= 500);
            }
            return data;
        } catch (error) {
            if (typeof error.retryable === 'boolean') throw error;
            throw failure('Unable to reach the panel.', true);
        } finally {
            clearTimeout(timeout);
        }
    }

    window.runBackupJob = async function (isRestore, onProgress) {
        let data;
        try {
            data = await request('/api/backup/create', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({background: true, is_restore: Boolean(isRestore)})
            });
        } catch (error) {
            if (error.retryable) {
                throw failure(error.message + ' Backup status is unknown; check the backup list before retrying.');
            }
            throw error;
        }
        if (!['running', 'success', 'error'].includes(data.status) ||
                (data.status === 'success' && (typeof data.file !== 'string' || !data.file.endsWith('.zip'))) ||
                !/^[a-f0-9]{32}$/.test(data.job_id || '')) {
            throw failure('Invalid backup response. Refresh the page and check the backup list before retrying.');
        }
        const jobId = data.job_id;
        let errors = 0;
        while (true) {
            if (data.status === 'error') throw failure(data.message || 'Backup creation failed. Check the panel logs.');
            if (data.status === 'success' && typeof data.file === 'string' && data.file.endsWith('.zip')) return data;
            onProgress(data.message || 'Creating backup...');
            await wait(errors ? 3000 : 1000);
            try {
                const next = await request('/api/backup/status/' + jobId);
                if (next.job_id !== jobId || !['running', 'success', 'error'].includes(next.status) ||
                        (next.status === 'success' && (typeof next.file !== 'string' || !next.file.endsWith('.zip')))) {
                    throw failure('Invalid backup progress response.', true);
                }
                data = next;
                errors = 0;
            } catch (error) {
                if (!error.retryable) throw error;
                if (++errors >= 20) {
                    throw failure('Unable to check backup progress. The backup may still be running; check the backup list before retrying.');
                }
                data = {status: 'running', message: 'Progress temporarily unavailable. Retrying; this does not mean the backup failed.'};
            }
        }
    };
})();
