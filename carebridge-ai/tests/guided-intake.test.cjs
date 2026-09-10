const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        value: '', textContent: '', innerHTML: '', disabled: false,
        classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c), toggle(c, on) { on ? classes.add(c) : classes.delete(c); } },
        setAttribute() {}, removeAttribute() {}, focus() {}, setCustomValidity() {}, reportValidity() { return Boolean(this.value.trim()); }, addEventListener() {},
      });
    }
    return elements.get(id);
  }
  const requests = [], cases = [], errors = [];
  const context = vm.createContext({
    document: { getElementById: element, addEventListener() {}, querySelector: element, querySelectorAll: () => [] },
    state: { pendingPreview: null },
    escapeHtml: text => String(text ?? ''), renderMarkdownLite: text => text,
    addCaseToFeed: record => cases.push(record), showToast: error => errors.push(error), triggerTextDownload() {},
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return { ok: true, json: async () => url.endsWith('/preview') ? { preview_id: 'preview-1', urgency: 'Low', matched_schemes: [], gaps: [] } : { case_id: 'case-1' } };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/guided-intake.js'), 'utf8'), context);
  const run = code => vm.runInContext(code, context);
  return { element, requests, cases, errors, run, context };
}

test('collects each answer and preserves it while navigating back', async () => {
  const app = setup();
  app.run('changeIntakeStep(1)');
  assert.equal(app.run('intakeStep'), 0);
  app.element('intake-input').value = 'Lost my job';
  app.run('changeIntakeStep(1)');
  app.element('household-input').value = 'Two dependents, rental flat';
  app.run('changeIntakeStep(-1)');
  assert.equal(app.element('household-input').value, 'Two dependents, rental flat');
  await app.run('runAnalysis()');
  assert.equal(app.requests.length, 0);
});

test('review creates recommendations without a saved case; submission requires help choice', async () => {
  const app = setup();
  for (const [id, answer] of [['intake-input', 'Lost my job'], ['household-input', 'Two dependents'], ['needs-input', 'Food support']]) app.element(id).value = answer;
  app.run('showIntakeStep(3)');
  await app.run('runAnalysis()');
  assert.equal(app.run('intakeStep'), 4);
  assert.match(app.requests[0].body.message, /Lost my job[\s\S]*Two dependents[\s\S]*Food support/);
  assert.equal(app.cases.length, 0);
  app.element('support-choice').value = 'self';
  await app.run('confirmSubmitCase()');
  assert.equal(app.requests.length, 1);
  app.element('support-choice').value = 'help';
  await app.run('confirmSubmitCase()');
  await app.run('confirmSubmitCase()');
  assert.equal(app.cases.length, 1);
  assert.equal(app.requests.length, 2);
  assert.equal(app.requests[1].body.preview_id, 'preview-1');
});

test('editing invalidates old recommendations and failures retain answers for retry', async () => {
  const app = setup();
  app.context.state.pendingPreview = { preview_id: 'old' };
  app.run('editIntake()');
  assert.equal(app.context.state.pendingPreview, null);
  ['intake-input', 'household-input', 'needs-input'].forEach(id => app.element(id).value = 'My answer');
  app.context.fetch = async () => { throw new Error('Connection failed'); };
  app.run('showIntakeStep(3)');
  await app.run('runAnalysis()');
  assert.equal(app.run('intakeStep'), 3);
  assert.equal(app.run('intakeBusy'), false);
  assert.equal(app.element('household-input').value, 'My answer');
  assert.equal(app.element('triage-error').textContent, 'Connection failed');
});
