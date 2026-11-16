'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('source/NerdTools/usr/local/emhttp/plugins/NerdTools/js/jquery.NerdTools.js', 'utf8');
const context = {console, $: callback => { if (typeof callback !== 'function') throw new Error('Unexpected jQuery call during load'); }};
vm.createContext(context);
vm.runInContext(source, context);

context.catalogRevision = 2;
assert.equal(context.isCatalogResponseCurrent(1), false, 'older catalog response must be ignored');
assert.equal(context.isCatalogResponseCurrent(2), true, 'latest catalog response must be accepted');
context.planRevision = 4;
context.nerdPlan = {id: 'new-plan'};
assert.equal(context.isConfirmationCurrent(3, 'old-plan'), false, 'older confirmation revision must be ignored');
assert.equal(context.isConfirmationCurrent(4, 'old-plan'), false, 'superseded plan ID must be ignored');
assert.equal(context.isConfirmationCurrent(4, 'new-plan'), true, 'current confirmation may execute');
context.displayedPlanId = 'old-plan';
context.displayedPlanRevision = 3;
assert.equal(context.isDisplayedPlanCurrent(), false, 'replacement plan cannot confirm an older dialog');
context.displayedPlanId = 'new-plan';
context.displayedPlanRevision = 4;
assert.equal(context.isDisplayedPlanCurrent(), true, 'displayed current plan may be confirmed');
context.confirmingPlanId = 'new-plan';
assert.equal(context.canCancelConfirmation(), false, 'confirmation cannot be dismissed in flight');
context.confirmingPlanId = null;
assert.equal(context.canCancelConfirmation(), true, 'idle confirmation dialog may be cancelled');
assert.equal(context.actionLabel('keep', false), '-', 'uninstalled Keep displays as -');
assert.equal(context.actionLabel('keep', true), 'Keep', 'installed Keep keeps its label');
assert.equal(context.packageDisplayName('nerdtools-vim'), 'vim', 'modal strips internal package prefix');
assert.equal(context.packageDisplayName('vim'), 'vim', 'plain package names remain unchanged');
assert.equal(context.actionIsEmphasized('keep'), false, 'Keep action is not emphasized');
assert.equal(context.actionIsEmphasized('install'), true, 'non-keep action is emphasized');
assert.deepEqual(
    JSON.parse(JSON.stringify(context.catalogStatusRows(
        {plugin_version: '2026.11.16.10', release: 'packages-test', hours: 24},
        {last_success: 100, next_check: 200, error: ''},
        value => `time:${value}`
    ))),
    [
        {label: 'NerdTools version', value: '2026.11.16.10'},
        {label: 'Package release', value: 'packages-test'},
        {label: 'Last successful check', value: 'time:100'},
        {label: 'Next eligible check', value: 'time:200'},
    ],
    'catalog metadata is returned as label-value rows'
);
assert.deepEqual(
    JSON.parse(JSON.stringify(context.catalogStatusRows(
        {plugin_version: '', release: '', hours: 0},
        {error: 'rate limited'},
        value => `time:${value}`
    ))),
    [
        {label: 'NerdTools version', value: 'Unknown'},
        {label: 'Package release', value: 'None'},
        {label: 'Last successful check', value: 'Never'},
        {label: 'Last check error', value: 'rate limited'},
        {label: 'Catalog in use', value: 'Last successful cache'},
        {label: 'Next eligible check', value: 'Disabled'},
    ],
    'catalog error and disabled states remain structured'
);
assert.equal(context.actionClass('remove', true), 'package-remove', 'Remove class overrides update availability');
assert.equal(context.actionClass('install', false), 'package-install', 'Install uses install class');
assert.equal(context.actionClass('upgrade', true), 'package-upgrade', 'Upgrade uses upgrade class');
assert.equal(context.actionClass('keep', true), 'package-update-available', 'Available Keep uses update class');
assert.equal(context.actionClass('keep', false), '', 'Current Keep has no action class');
assert.equal(context.versionHasUpdate('keep', true), true, 'available Keep marks version cells');
assert.equal(context.versionHasUpdate('upgrade', true), true, 'Upgrade marks version cells');
assert.equal(context.versionHasUpdate('remove', true), false, 'Remove clears version-cell update class');
const visual = {selected: null, actionCell: null, versionCell: null, plannedCell: null};
const rowClasses = new Set(['package-has-update']);
const cellClasses = new Set();
const fakeCells = {
    removeClass: names => { names.split(' ').forEach(name => cellClasses.delete(name)); return fakeCells; },
    addClass: names => { names.split(' ').forEach(name => cellClasses.add(name)); return fakeCells; },
};
const fakeActionCell = {toggleClass: (_name, value) => { visual.actionCell = value; return fakeActionCell; }};
const fakeVersion = {toggleClass: (_name, value) => { visual.versionCell = value; return fakeVersion; }};
const fakePlanned = {toggleClass: (_name, value) => { visual.plannedCell = value; return fakePlanned; }};
const fakeSelect = {
    val: value => { visual.selected = value; return fakeSelect; },
    closest: () => fakeActionCell,
};
const fakeRow = {
    hasClass: name => rowClasses.has(name),
    children: () => fakeCells,
    find: selector => selector === '.pkgaction' ? fakeSelect : (selector === '.version-value' ? fakeVersion : fakePlanned),
};
context.syncActionDisplay(fakeRow, 'remove');
assert.deepEqual(visual, {selected: 'remove', actionCell: true, versionCell: false, plannedCell: true},
    'Remove updates row and cell classes atomically');
assert.deepEqual([...cellClasses], ['package-remove'],
    'Remove clears yellow cell classes and applies red');
context.syncActionDisplay(fakeRow, 'keep');
assert.deepEqual(visual, {selected: 'keep', actionCell: false, versionCell: true, plannedCell: false},
    'available Keep updates row and cell classes atomically');
assert.deepEqual([...cellClasses], ['package-update-available'],
    'available Keep clears red cell classes and applies yellow');
assert.equal(context.automaticAction({automatic: true, change: 'remove'}), 'remove',
    'automatic removal selects Remove');
assert.equal(context.automaticAction({automatic: true, change: 'upgrade'}), 'upgrade',
    'automatic upgrade selects Upgrade');
assert.equal(context.automaticAction({automatic: false, change: 'upgrade'}), null,
    'explicit actions are not rewritten');
const postCalls = []; const openBoxCalls = []; let closeCalls = 0;
const jqueryResult = {
    val: () => '24',
    each: () => jqueryResult,
    prop: () => jqueryResult,
    text: () => jqueryResult,
};
const jquery = () => jqueryResult;
jquery.post = (url, data) => {
    postCalls.push({url, data});
    const deferred = {done: () => deferred, fail: () => deferred, always: () => deferred};
    return deferred;
};
context.$ = jquery;
context.document = {getElementById: () => ({close: () => { closeCalls++; }})};
context.openBox = (...args) => openBoxCalls.push(args);
context.planRevision = 5;
context.nerdPlan = {id: 'replacement-plan', actions: {dep: 'upgrade', consumer: 'keep'}};
context.displayedPlanId = 'displayed-plan';
context.displayedPlanRevision = 4;
context.confirmPlan();
assert.equal(closeCalls, 1, 'stale confirmation closes the dialog');
assert.equal(postCalls.length, 0, 'stale confirmation sends no request');
assert.equal(openBoxCalls.length, 0, 'stale confirmation starts no package manager');
context.displayedPlanId = 'replacement-plan';
context.displayedPlanRevision = 5;
context.confirmPlan();
assert.equal(postCalls.length, 1, 'current confirmation sends one request');
assert.equal(postCalls[0].data.plan, 'replacement-plan', 'confirmation submits the displayed plan ID');
assert.equal(postCalls[0].data['actions[dep]'], 'upgrade', 'confirmation preserves the server plan root intent');
assert.equal(postCalls[0].data['actions[consumer]'], 'keep', 'confirmation preserves automatic dependent intent');
console.log('Package UI race contracts passed');
