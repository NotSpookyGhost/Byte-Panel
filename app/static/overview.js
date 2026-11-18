(function () {
    'use strict';
    const charts = {};
    const accent = getComputedStyle(document.body).getPropertyValue('--accent').trim();
    function chart(id, maximum) {
        if (!window.Chart) return;
        charts[id] = new Chart(document.getElementById(id), {
            type:'line', data:{labels:[], datasets:[{data:[], borderColor:accent, backgroundColor:accent + '12', fill:true}]},
            options:{responsive:true, maintainAspectRatio:false, animation:false,
                plugins:{legend:{display:false}, tooltip:{enabled:false}},
                scales:{x:{display:false}, y:{display:false, min:0, suggestedMax:maximum}},
                elements:{point:{radius:0}, line:{tension:.3, borderWidth:1.5}}}
        });
    }
    chart('cpuChart', 100);
    chart('ramChart', Number(document.getElementById('ramChart').dataset.max) * 1024);
    function update(id, value) {
        const item = charts[id];
        if (!item || !Number.isFinite(value)) return;
        item.data.labels.push('');
        item.data.datasets[0].data.push(value);
        if (item.data.labels.length > 30) { item.data.labels.shift(); item.data.datasets[0].data.shift(); }
        item.update();
    }
    document.addEventListener('panel:stats', event => {
        document.getElementById('stat-cpu').textContent = event.detail.cpu;
        document.getElementById('stat-ram').textContent = event.detail.ram;
        update('cpuChart', parseFloat(event.detail.cpu));
        update('ramChart', parseFloat(event.detail.ram));
        // The legacy TPS field is a fixed placeholder, not a measurement.
    });
    document.addEventListener('panel:stats-unavailable', () => {
        document.getElementById('stat-cpu').textContent = '—';
        document.getElementById('stat-ram').textContent = '—';
    });
    document.querySelectorAll('[data-server-action]').forEach(button => button.addEventListener('click', async () => {
        const message = document.getElementById('overview-message');
        button.disabled = true;
        try {
            const response = await fetch('/api/server/' + button.dataset.serverAction, {method:'POST'});
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || data.error || 'The server action could not be completed.');
            message.textContent = 'Server response: ' + data.status;
        } catch (error) { message.textContent = error.message || 'Unable to reach the server.'; }
        finally { message.hidden = false; button.disabled = false; }
    }));
}());

