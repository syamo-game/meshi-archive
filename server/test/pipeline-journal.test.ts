import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings } from '../src/ai.js';
import { openDatabase } from '../src/db/database.js';
import { migrateDatabase } from '../src/db/migrate.js';
import { lookupCacheKey, PipelineJournal } from '../src/pipeline-journal.js';

test('operational records survive a rolled-back processing transaction without duplicate cost', async () => {
  const db = openDatabase('sqlite://'); const settings = readAISettings({}); const journal = new PipelineJournal(settings); const id = '90000000000000001';
  try {
    await migrateDatabase(db);
    await db.execute("INSERT INTO messages(message_id,is_target,processing_status) VALUES($1,$2,'pending')", [id, true]);
    journal.metrics(id, 'candidate_search', { model: settings.RESOLUTION_MODEL, input_tokens: 12, output_tokens: 3, web_search_calls: 2, image_count: 0, latency_ms: 25, estimated_cost_microusd: 123, api_attempts: 2 });
    journal.put('web_search', 'query', '{"candidates":[]}');
    await assert.rejects(db.transaction(async connection => { await journal.persist(connection); throw new Error('Processing failed after API call'); }), /Processing failed/);
    assert.equal((await db.query('SELECT id FROM processing_runs', z.object({ id: z.number() }))).length, 0);
    journal.failure(id, 'message_pipeline', new Error('Processing failed after API call'));
    await db.transaction(connection => journal.persist(connection));
    const runs = await db.query('SELECT stage,status,estimated_cost_microusd AS cost FROM processing_runs ORDER BY id', z.object({ stage: z.string(), status: z.string(), cost: z.number() }));
    assert.deepEqual(runs.map(row => row.cost), [123, 0, 0]); assert.equal(runs[1]?.stage, 'candidate_search_retry_attempt');
    assert.equal(await new PipelineJournal(settings).get(db, 'web_search', 'query'), '{"candidates":[]}');
  } finally { await db.close(); }
});

test('cache invalidation, replacement, expiry and model namespaces use real persisted rows', async () => {
  const db = openDatabase('sqlite://'); const settings = readAISettings({}); const journal = new PipelineJournal(settings, 1); const now = new Date('2026-10-18T00:00:00Z');
  try {
    await migrateDatabase(db); journal.put('url_metadata', 'url', 'first'); await journal.persist(db, now);
    const next = new PipelineJournal(settings, 1);
    assert.equal(await next.get(db, 'url_metadata', 'url', now), 'first');
    assert.equal(await next.get(db, 'url_metadata', 'url', new Date('2026-10-19T00:00:00Z')), null);
    next.invalidate('url_metadata', 'url'); assert.equal(await next.get(db, 'url_metadata', 'url', now), null);
    next.put('url_metadata', 'url', 'replacement'); assert.equal(await next.get(db, 'url_metadata', 'url', now), 'replacement');
    await next.persist(db, now);
    assert.equal(await new PipelineJournal(settings).get(db, 'url_metadata', 'url', now), 'replacement');
    const other = { ...settings, RESOLUTION_MODEL: 'other', EXTRACTION_MODEL: 'other' };
    assert.notEqual(lookupCacheKey('web_search', 'url', settings), lookupCacheKey('web_search', 'url', other));
    assert.notEqual(lookupCacheKey('source_discovery', 'url', settings), lookupCacheKey('source_discovery', 'url', other));
    assert.equal(lookupCacheKey('url_metadata', 'url', settings), lookupCacheKey('url_metadata', 'url', other));
  } finally { await db.close(); }
});
