'use strict';
// Exercise Settings' backup/purge flow with in-memory doubles only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../app/templates/settings.html'), 'utf8');
const helper = template.slice(template.indexOf('async function createSettingsBackup('),
    template.indexOf('async function uploadImport('));
const purge = template.slice(template.indexOf('async function startPurgeWizard('),
    template.indexOf('</script>', template.indexOf('async function startPurgeWizard(')));
assert(!purge.includes("'/api/backup'"), 'Removed endpoint must not return');
assert(!helper.includes('setInterval'), 'Backup completion cannot be simulated');

async function scenario({failure = false, choice = 'backup', confirmPurge = false, serverKey = 'QA_Realm'} = {}) {
    const calls = [], messages = [], downloads = [];
    const nodes = new Map();
    const document = {
        getElementById(id) { if (!nodes.has(id)) nodes.set(id, {style:{}, textContent:''}); return nodes.get(id); },
        createElement() {
            const anchor = {click() { downloads.push(anchor.href); }, remove() {}};
            return anchor;
        },
        body:{appendChild() {}}
    };
    const modal = {
        finish:async message => messages.push(message),
        error:message => messages.push(message)
    };
    const context = {
        document, location:{href:''},
        window:{runBackupJob: async (restore, progress) => {
            calls.push('backup');
            assert.equal(restore, false);
            progress('Writing archive');
            if (failure) throw new Error('Disk full');
            return {status:'success', file:'QA_1.zip', server_key:serverKey};
        }},
        showModal: async (title, message) => {
            calls.push(title);
            if (title === 'Backup Ecosystem') return choice;
            if (title === 'Critical Warning') return confirmPurge;
            if (title === 'Creating Backup') return modal;
            return true;
        },
        fetch: async url => {
            calls.push(url);
            return {ok:true, json:async () => url === '/api/stats' ? {status:'Offline'} : {status:'purged'}};
        }
    };
    vm.runInNewContext(helper + '\n' + purge, context);
    await context.startPurgeWizard();
    return {calls, messages, downloads, context};
}
(async () => {
    const failed = await scenario({failure:true, confirmPurge:true});
    assert(failed.messages.includes('Disk full'));
    assert(!failed.calls.includes('Critical Warning'));
    assert(!failed.calls.includes('/api/purge'), 'Backup failure must abort purge');
    const completed = await scenario();
    assert(completed.calls.indexOf('backup') < completed.calls.indexOf('Critical Warning'));
    assert(!completed.calls.includes('/api/purge'), 'Final confirmation still required');
    const download = await scenario({choice:'download'});
    assert.deepEqual(download.downloads, ['/api/backup/download/QA_Realm/QA_1.zip']);
    assert(download.messages.some(message => message.includes('Check that the download completed')));
    const renamed = await scenario({choice:'download', serverKey:'Original_Realm'});
    assert.deepEqual(renamed.downloads, ['/api/backup/download/Original_Realm/QA_1.zip']);
    const missingKey = await scenario({choice:'download', serverKey:null, confirmPurge:true});
    assert.equal(missingKey.downloads.length, 0);
    assert(!missingKey.calls.includes('/api/purge'));
    assert(missingKey.messages.some(message => message.includes('download location is unavailable')));
    const canceled = await scenario({choice:'cancel', confirmPurge:true});
    assert(!canceled.calls.includes('backup'));
    assert(!canceled.calls.includes('/api/purge'));
    const confirmed = await scenario({choice:'none', confirmPurge:true});
    assert(!confirmed.calls.includes('backup'));
    assert(confirmed.calls.includes('/api/purge'));
    console.log('Settings backup checks passed (verified completion, error abort, download path, cancellation and final confirmation).');
})().catch(error => { console.error(error); process.exitCode = 1; });

