import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings } from '../src/ai.js';
import { MessageEvaluator } from '../src/evaluation.js';
import { evaluationSnapshot, loadEvaluation, lockEvaluation } from '../src/evaluation-state.js';
import { mentionRecord, messageRecord } from '../src/records.js';
import { fixture } from './helpers.js';

const id = '90000000000000001';
test('evaluation preserves human decisions without calling AI', async () => {
  const { database } = await fixture();
  try {
    await database.execute("UPDATE messages SET content='店舗01の紹介' WHERE message_id=$1", [id]);
    await database.execute("UPDATE shop_mentions SET resolution_method='manual' WHERE message_id=$1", [id]);
    const before = await loadEvaluation(database, id);
    const result = await new MessageEvaluator(database, readAISettings({}), '.local/no-image-fixture').evaluate(id);
    assert.equal(result.skip_reason, 'human_reviewed');
    assert.deepEqual(await loadEvaluation(database, id), before);
  } finally { await database.close(); }
});

test('evaluation records unavailable sources while retaining linked shops and legacy review flags', async () => {
  const { database } = await fixture();
  try {
    await database.execute("UPDATE shop_mentions SET difference_type='legacy_review' WHERE message_id=$1", [id]);
    const result = await new MessageEvaluator(database, readAISettings({}), '.local/no-image-fixture').evaluate(id);
    assert.equal(result.skip_reason, 'source_unavailable');
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [id]))[0];
    assert.equal(mention?.shop_id, 1); assert.equal(mention?.review_status, 'pending'); assert.equal(mention?.difference_type, 'legacy_review'); assert.equal(mention?.version, 2);
  } finally { await database.close(); }
});

test('evaluation snapshot rejects concurrent shop edits and added human review events', async () => {
  const { database } = await fixture();
  try {
    const expected = evaluationSnapshot(await loadEvaluation(database, id));
    await database.execute('UPDATE shops SET version=version+1 WHERE id=1');
    await assert.rejects(database.transaction(connection => lockEvaluation(connection, id, expected)), /concurrent_change/);
    const current = await loadEvaluation(database, id); const mention = current.mentions[0]; assert.ok(mention);
    await database.execute("INSERT INTO review_events(mention_id,scope,action,created_at) VALUES($1,'identity','approve',$2)", [mention.id, new Date().toISOString()]);
    await assert.rejects(database.transaction(connection => lockEvaluation(connection, id, evaluationSnapshot(current))), /human_reviewed/);
  } finally { await database.close(); }
});

test('failed evaluation leaves canonical shops and mentions unchanged and preserves operational errors', async () => {
  const { database } = await fixture();
  try {
    await database.execute("UPDATE messages SET content='店舗01の紹介' WHERE message_id=$1", [id]);
    const before = await loadEvaluation(database, id);
    await assert.rejects(new MessageEvaluator(database, readAISettings({}), '.local/no-image-fixture').evaluate(id), /OPENAI_API_KEY/);
    const after = await loadEvaluation(database, id);
    assert.deepEqual(after.shops, before.shops); assert.deepEqual(after.mentions, before.mentions);
    assert.equal((await database.query('SELECT * FROM messages WHERE message_id=$1', messageRecord, [id]))[0]?.processing_status, 'failed');
    assert.deepEqual((await database.query('SELECT stage FROM processing_runs WHERE message_id=$1 ORDER BY id', z.object({ stage: z.string() }), [id])).map(row => row.stage), ['evaluation_extraction', 'evaluation_pipeline']);
  } finally { await database.close(); }
});
