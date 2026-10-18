import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { openDatabase, timestampColumn } from '../src/db/database.js';
import { migrateDatabase } from '../src/db/migrate.js';
import { processingReport, reportTargetFailures } from '../src/operations/reports.js';
import { transferDatabase, validateTransferUrls } from '../src/operations/transfer.js';
import { fixture } from './helpers.js';

test('database transfer verifies every application table and refuses an occupied target', async () => {
  const { database } = await fixture(); const target = openDatabase('sqlite://');
  try {
    await migrateDatabase(target);
    await database.execute("UPDATE shops SET created_at='2026-10-18 01:02:03.123456' WHERE id=1");
    const copied = await transferDatabase(database, target, 'copy'); assert.equal(copied.status, 'transferred'); assert.equal(copied.tables.find(row => row.table === 'shops')?.rows, 31);
    assert.equal(copied.tables.find(row => row.table === 'web_sessions')?.rows, 2);
    assert.equal((await transferDatabase(database, target, 'verify')).status, 'verified');
    await assert.rejects(transferDatabase(database, target, 'copy'), /not empty/);
    await target.execute("UPDATE shops SET memo='changed' WHERE id=1");
    await assert.rejects(transferDatabase(database, target, 'verify'), /shops/);
    assert.equal((await target.query('SELECT created_at FROM shops WHERE id=1', z.object({ created_at: z.string() })))[0]?.created_at, '2026-10-18T01:02:03.123456Z');
  } finally { await database.close(); await target.close(); }
});

test('database transfer rolls back all tables when a later table rejects a row', async () => {
  const { database } = await fixture(); const target = openDatabase('sqlite://');
  try {
    await migrateDatabase(target);
    await target.script("CREATE TRIGGER fail_transfer BEFORE INSERT ON shop_mentions BEGIN SELECT RAISE(ABORT,'transfer test failure'); END;");
    await assert.rejects(transferDatabase(database, target, 'copy'), /transfer test failure/);
    assert.equal((await target.query('SELECT COUNT(*) AS count FROM shops', z.object({ count: z.number() })))[0]?.count, 0);
    assert.equal((await target.query('SELECT COUNT(*) AS count FROM messages', z.object({ count: z.number() })))[0]?.count, 0);
  } finally { await database.close(); await target.close(); }
});

test('processing report accounts for retries and preserves acceptance thresholds', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    await db.execute("INSERT INTO processing_runs(stage,model,status,input_tokens,output_tokens,web_search_calls,image_count,latency_ms,estimated_cost_microusd,created_at) VALUES('candidate_search','synthetic','succeeded',10,2,1,0,100,123,$1)", [new Date().toISOString()]);
    const report = await processingReport(db, { baselineCalls: 10, baselineCost: 1000, assertTargets: true });
    assert.equal(report.billed_calls, 2); assert.equal(report.estimated_cost_microusd, 123); assert.equal(report.call_reduction_percent, 80); assert.equal(report.cost_reduction_percent, 87.7);
    assert.deepEqual(reportTargetFailures(report), ['processed_messages=0, expected=260']);
    await assert.rejects(processingReport(db, { baselineCalls: null, baselineCost: null, assertTargets: true }), /both baseline/);
  } finally { await db.close(); }
});

test('transfer confirmation and PostgreSQL timestamp parsing preserve precision and offsets', () => {
  assert.throws(() => validateTransferUrls('sqlite:///a', 'postgresql://host/db', 'postgresql://host/other'), /exactly match/);
  assert.equal(timestampColumn.parse('2026-10-18 01:02:03.123456+00'), '2026-10-18T01:02:03.123456+00:00');
  assert.equal(timestampColumn.parse('2026-10-18 01:02:03.123456+0900'), '2026-10-18T01:02:03.123456+09:00');
  assert.equal(timestampColumn.parse('2026-10-18'), '2026-10-18Z');
});
