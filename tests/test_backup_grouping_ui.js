'use strict';
// Execute the actual template renderer; no network requests or backup mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../app/templates/backups.html'), 'utf8');
const renderer = template.slice(template.indexOf('function formatBytesMbGb('),
    template.indexOf('function renderBackupTable('));
assert(template.includes('const currentServerKey = {{ current_backup_key | tojson }};'));

function render(name, key, backups) {
    const nodes = {'backups-container':{}, 'total-storage-label':{}};
    const tables = [];
    const context = {
        currentServerName:name, currentServerKey:key,
        document:{getElementById:id => nodes[id]},
        renderBackupTable(server, files, manual) {
            tables.push({server, names:Array.from(files, file => file.name), manual});
            return files.map(file => '<p>' + file.name + '</p>').join('');
        }
    };
    vm.runInNewContext(renderer, context);
    context.renderBackups(backups);
    return {html:nodes['backups-container'].innerHTML, total:nodes['total-storage-label'].innerText, tables};
}
const manual = {name:'active-manual.zip', type:'manual', size:1048576};
const auto = {name:'active-auto.zip', type:'auto', size:1048576};
const other = {name:'other.zip', type:'manual', size:1048576};
const map = {Friends_SMP:[other], JRock_Bottom:[manual, auto]};
const original = JSON.stringify(map);
const result = render('JRock Bottom', 'JRock_Bottom', map);
assert(result.html.indexOf('Current Server') < result.html.indexOf('Other Server Backups'));
assert(result.html.indexOf('active-auto.zip') < result.html.indexOf('<details'));
assert(result.html.indexOf('other.zip') > result.html.indexOf('<details'));
assert(result.html.includes('JRock Bottom'));
assert.equal((result.html.match(/Current Server/g) || []).length, 1);
assert.deepEqual(result.tables.map(table => table.server), ['JRock_Bottom','JRock_Bottom','Friends_SMP']);
assert.equal(result.total, '(Total Space Used: 3.00 MB)');
assert.equal(JSON.stringify(map), original, 'Rendering never moves or rewrites archives');

// Do not merge a separately stored, similarly named folder into the active one.
const distinct = render('JRock Bottom', 'JRock_Bottom', {JRock_Bottom:[manual], 'JRock Bottom':[other]});
assert(distinct.html.indexOf('other.zip') > distinct.html.indexOf('<details'));
assert.deepEqual(distinct.tables.map(table => table.server), ['JRock_Bottom','JRock Bottom']);
const missing = render('New Realm', 'New_Realm', {Friends_SMP:[other]});
assert(missing.html.includes('Current Server'));
assert(missing.html.includes('No manual backups found.'));
assert.equal(missing.tables.length, 1);
const escaped = render('<b>Realm & "Friends"</b>', 'Realm_Friends', {Realm_Friends:[manual]});
assert(escaped.html.includes('&lt;b&gt;Realm &amp; &quot;Friends&quot;&lt;/b&gt;'));
assert(!escaped.html.includes('<b>Realm'));
const empty = render('Empty Realm', 'Empty_Realm', {});
assert(empty.html.includes('No backups found.'));
assert.equal(empty.total, '(Total Space Used: 0 MB)');
console.log('Backup grouping checks passed (normalized keys, separate folders, empty groups, safe labels and totals).');
