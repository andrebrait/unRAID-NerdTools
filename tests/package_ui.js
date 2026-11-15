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
context.confirmingPlanId = 'new-plan';
assert.equal(context.canCancelConfirmation(), false, 'confirmation cannot be dismissed in flight');
context.confirmingPlanId = null;
assert.equal(context.canCancelConfirmation(), true, 'idle confirmation dialog may be cancelled');
console.log('Package UI race contracts passed');
