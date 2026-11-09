#!/usr/bin/env bash
# Builds a small "orders service" repo for routing evals. Files only.
set -euo pipefail
mkdir -p src/auth src/orders src/queue src/worker src/middleware test migrations
cat > package.json <<'EOF'
{ "name": "orders-svc", "version": "2.3.1", "scripts": { "test": "node --test", "lint": "eslint src" } }
EOF
cat > README.md <<'EOF'
# orders-svc
HTTP API + background worker for orders. Postgres for storage, in-process job queue.
EOF
cat > src/auth/token.js <<'EOF'
const crypto = require('crypto');
const SECRET = process.env.TOKEN_SECRET;
function createToken(userId) { const exp = Date.now() + 15 * 60e3; const body = `${userId}.${exp}`;
  return `${body}.${crypto.createHmac('sha256', SECRET).update(body).digest('hex')}`; }
function verifyToken(token) { const [userId, exp, sig] = token.split('.');
  const good = crypto.createHmac('sha256', SECRET).update(`${userId}.${exp}`).digest('hex');
  return sig === good && Number(exp) > Date.now() ? userId : null; }
module.exports = { createToken, verifyToken };
EOF
cat > src/auth/refresh.js <<'EOF'
const { createToken, verifyToken } = require('./token');
function refresh(oldToken) { const userId = verifyToken(oldToken); return userId ? createToken(userId) : null; }
module.exports = { refresh };
EOF
cat > src/middleware/requireAuth.js <<'EOF'
const { verifyToken } = require('../auth/token');
module.exports = (req, res, next) => { const userId = verifyToken((req.headers.authorization || '').slice(7));
  if (!userId) return res.status(401).end(); req.userId = userId; next(); };
EOF
cat > src/orders/repo.js <<'EOF'
const db = require('../db');
exports.list = (userId) => db.query('SELECT id, total, created_at FROM orders WHERE user_id = $1', [userId]);
exports.create = (userId, total) => db.query('INSERT INTO orders (user_id, total) VALUES ($1, $2) RETURNING id', [userId, total]);
EOF
cat > src/orders/api.js <<'EOF'
const repo = require('./repo'); const queue = require('../queue/jobQueue');
exports.create = async (req, res) => { const { rows } = await repo.create(req.userId, req.body.total);
  await queue.enqueue('process-order', { orderId: rows[0].id }); res.status(201).json(rows[0]); };
EOF
cat > src/queue/jobQueue.js <<'EOF'
const seen = new Set(); const jobs = [];
async function enqueue(type, payload) { const key = `${type}:${payload.orderId}`;
  if (seen.has(key)) throw new Error('duplicate job');
  await new Promise((r) => setImmediate(r)); // persist hook placeholder
  seen.add(key); jobs.push({ type, payload }); }
function drain() { return jobs.splice(0); }
module.exports = { enqueue, drain, _seen: seen };
EOF
cat > src/worker/processOrders.js <<'EOF'
const queue = require('../queue/jobQueue'); const db = require('../db');
async function tick() { for (const job of queue.drain()) {
  for (let attempt = 1; attempt <= 3; attempt++) { try { await db.query('UPDATE orders SET processed_at = now() WHERE id = $1', [job.payload.orderId]); break; }
    catch (e) { if (attempt === 3) throw e; } } } }
module.exports = { tick };
EOF
cat > test/queue.test.js <<'EOF'
const test = require('node:test'); const assert = require('node:assert'); const q = require('../src/queue/jobQueue');
test('enqueue twice in parallel keeps one job', async () => {
  await Promise.allSettled([q.enqueue('process-order', { orderId: 1 }), q.enqueue('process-order', { orderId: 1 })]);
  assert.strictEqual(q.drain().length, 1); });
EOF
cat > migrations/001_create_orders.sql <<'EOF'
CREATE TABLE orders (id serial PRIMARY KEY, user_id text NOT NULL, total numeric NOT NULL,
  created_at timestamptz DEFAULT now(), processed_at timestamptz);
EOF
# A realistic spread: 40 feature modules, some authenticated, some not.
for i in $(seq 1 40); do
  mkdir -p "src/modules/feature$i"
  if [ $((i % 3)) -eq 0 ]; then
    cat > "src/modules/feature$i/handler.js" <<MOD
const requireAuth = require('../../middleware/requireAuth');
function handle(req, res) { res.json({ feature: $i, user: req.userId }); }
module.exports = { route: '/feature$i', middleware: [requireAuth], handle };
MOD
  elif [ $((i % 7)) -eq 0 ]; then
    cat > "src/modules/feature$i/handler.js" <<MOD
const { verifyToken } = require('../../auth/token');
function handle(req, res) { const user = verifyToken(req.query.t || ''); res.json({ feature: $i, user }); }
module.exports = { route: '/feature$i', middleware: [], handle };
MOD
  else
    cat > "src/modules/feature$i/handler.js" <<MOD
function handle(req, res) { res.json({ feature: $i }); }
function helper$i(x) { return x * $i; }
module.exports = { route: '/feature$i', middleware: [], handle, helper$i };
MOD
  fi
done
git init -q && git add -A && git -c user.name=eval -c user.email=eval@example.com commit -qm "initial"
