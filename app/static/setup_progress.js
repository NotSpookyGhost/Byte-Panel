/* Only a successful progress response can declare the Minecraft build failed. */
(() => {
    'use strict';
    let stopPrevious = () => {};

    window.startSetupProgress = function () {
        stopPrevious();
        let stopped = false;
        let timer;
        let request;
        const title = document.getElementById('progress-title');
        const message = document.getElementById('progress-msg');
        const fill = document.getElementById('progress-fill');
        const percent = document.getElementById('progress-percent');

        function stop() {
            stopped = true;
            clearTimeout(timer);
            if (request) request.abort();
        }
        stopPrevious = stop;

        async function poll() {
            if (stopped) return;
            const controller = new AbortController();
            request = controller;
            const timeout = setTimeout(() => controller.abort(), 15000);
            let data;
            let accessMessage = '';
            try {
                const response = await fetch('/api/setup/progress', {
                    cache: 'no-store', headers: {Accept: 'application/json'},
                    signal: controller.signal
                });
                if (response.redirected || response.status === 401) {
                    accessMessage = 'Your session expired. Sign in again to check the server. The build may still be running.';
                    throw new Error('Sign-in required');
                }
                if (response.status === 403) {
                    accessMessage = 'Progress access denied. Sign in as the original setup owner to check the server.';
                    throw new Error('Access denied');
                }
                if (!response.ok) throw new Error('Progress request failed');
                if (!(response.headers.get('content-type') || '').includes('application/json')) {
                    throw new Error('Unexpected progress response');
                }
                data = await response.json();
                if (!data || !['idle', 'running', 'done', 'error'].includes(data.status) ||
                        !Number.isFinite(data.percent) || data.percent < 0 || data.percent > 100 ||
                        typeof data.msg !== 'string') {
                    throw new Error('Invalid progress response');
                }
            } catch (error) {
                if (stopped) return;
                title.innerText = 'Progress unavailable';
                if (accessMessage) {
                    message.innerText = accessMessage;
                    stop();
                } else {
                    message.innerText = 'Unable to check progress. Retrying automatically; this does not mean the server build failed.';
                    timer = setTimeout(poll, 3000);
                }
                return;
            } finally {
                clearTimeout(timeout);
            }
            if (stopped) return;
            message.innerText = data.msg;
            fill.style.width = data.percent + '%';
            percent.innerText = Math.round(data.percent) + '%';
            if (data.status === 'done') {
                stop();
                title.innerText = 'Server ready';
                fill.style.width = '100%';
                percent.innerText = '100%';
                location.href = '/overview';
            } else if (data.status === 'error') {
                stop();
                title.innerText = 'Server Build Failed';
                fill.style.backgroundColor = '#dc2626';
                showModal('Server Build Failed', data.msg || 'The server could not be installed.');
            } else {
                title.innerText = 'Building Server';
                // Schedule only after this request finishes: never overlap polls.
                timer = setTimeout(poll, 1000);
            }
        }

        timer = setTimeout(poll, 1000);
    };
})();
