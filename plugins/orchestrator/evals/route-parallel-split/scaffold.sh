#!/usr/bin/env bash
# Builds two independent services in one repo for the parallel-split eval. Files only.
set -euo pipefail
mkdir -p billing/src billing/test notify/src notify/test
cat > README.md <<'MD'
# platform
Two services that share no code: billing/ (invoices) and notify/ (outbound email).
MD

cat > billing/package.json <<'JSON'
{ "name": "billing", "version": "0.4.0", "scripts": { "test": "node --test" } }
JSON
cat > billing/README.md <<'MD'
# billing
Invoice totals and money formatting.
MD
cat > billing/src/format.js <<'JS'
function formatAmount(cents) {
  const units = (cents / 100).toFixed(2);
  return `USD ${units}`;
}
module.exports = { formatAmount };
JS
cat > billing/src/invoice.js <<'JS'
const { formatAmount } = require('./format');
function total(lines) { return lines.reduce((sum, line) => sum + line.qty * line.unitCents, 0); }
function render(invoice) { return `${invoice.id}: ${formatAmount(total(invoice.lines))}`; }
module.exports = { total, render };
JS
cat > billing/test/format.test.js <<'JS'
const test = require('node:test'); const assert = require('node:assert');
const { formatAmount } = require('../src/format');
test('formats cents as USD', () => { assert.strictEqual(formatAmount(1234), 'USD 12.34'); });
JS

cat > notify/package.json <<'JSON'
{ "name": "notify", "version": "1.1.0", "scripts": { "test": "node --test" } }
JSON
cat > notify/README.md <<'MD'
# notify
Sends transactional email through a pluggable transport, with retries.
MD
cat > notify/src/send.js <<'JS'
async function send(transport, message) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try { return await transport.deliver(message); } catch (err) { lastError = err; }
  }
  throw lastError;
}
module.exports = { send };
JS
cat > notify/src/templates.js <<'JS'
exports.receipt = (name, amount) => ({ subject: 'Your receipt', body: `Hi ${name}, we received ${amount}.` });
JS
cat > notify/test/send.test.js <<'JS'
const test = require('node:test'); const assert = require('node:assert');
const { send } = require('../src/send');
test('gives up after 3 attempts', async () => {
  let calls = 0; const transport = { deliver: async () => { calls++; throw new Error('down'); } };
  await assert.rejects(send(transport, {})); assert.strictEqual(calls, 3);
});
JS
git init -q && git add -A && git -c user.name=eval -c user.email=eval@example.com commit -qm "initial"
