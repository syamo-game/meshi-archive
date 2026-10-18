import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { duplicateReport } from '../src/operations/duplicates.js';
import { fixture } from './helpers.js';

async function duplicateFixture(): Promise<Awaited<ReturnType<typeof fixture>>> {
  const context = await fixture();
  await context.database.execute("UPDATE shops SET shop_name='共通食堂',phone='03-1234-5678',address='東京都江東区木場1-2-3',is_visited=$1,rating=NULL WHERE id IN (3,4)", [false]);
  return context;
}

test('duplicate merging preserves mentions, multiline notes, redirects and an audit snapshot', async () => {
  const { database } = await duplicateFixture();
  try {
    await database.execute('UPDATE shops SET memo=$1 WHERE id=4', ['一行目\n\n二行目']);
    const preview = await duplicateReport(database, false);
    assert.equal(preview.safe_merge_group_count, 1);
    assert.equal(preview.merged_shop_count, 0);
    const result = await duplicateReport(database, true);
    assert.equal(result.merged_shop_count, 1);
    assert.equal(result.moved_mention_count, 1);
    assert.deepEqual((await database.query('SELECT id,memo FROM shops WHERE id IN (3,4)', z.object({ id: z.number(), memo: z.string() }))), [{ id: 3, memo: '一行目\n\n二行目' }]);
    assert.equal((await database.query('SELECT target_shop_id FROM shop_redirects WHERE source_shop_id=4', z.object({ target_shop_id: z.number() })))[0]?.target_shop_id, 3);
    const audit = await database.query("SELECT action,note FROM review_events WHERE previous_shop_id=4", z.object({ action: z.string(), note: z.string() }));
    assert.equal(audit[0]?.action, 'automatic_merge');
    assert.match(audit[0]?.note ?? '', /safe duplicate merge/);
    assert.equal((await duplicateReport(database, true)).merged_shop_count, 0);
  } finally { await database.close(); }
});

test('duplicate merging refuses conflicting ratings and distinct branches', async () => {
  const { database } = await duplicateFixture();
  try {
    await database.execute('UPDATE shops SET rating=id,is_visited=$1 WHERE id IN (3,4)', [true]);
    let report = await duplicateReport(database, true);
    assert.equal(report.merged_shop_count, 0);
    assert.ok(report.safe_merge_plan.groups[0]?.user_data_conflicts.includes('rating'));
    await database.execute("UPDATE shops SET rating=NULL,is_visited=$1,branch_name=CASE id WHEN 3 THEN '木場店' ELSE '銀座店' END WHERE id IN (3,4)", [false]);
    report = await duplicateReport(database, true);
    assert.equal(report.merged_shop_count, 0);
    assert.ok(report.safe_merge_plan.groups[0]?.identity_conflicts.includes('branch_name'));
  } finally { await database.close(); }
});

test('duplicate merging rolls back metadata, mentions, redirects and audit events on failure', async () => {
  const { database } = await duplicateFixture();
  try {
    await database.script("CREATE TRIGGER prevent_shop_delete BEFORE DELETE ON shops WHEN OLD.id=4 BEGIN SELECT RAISE(ABORT,'duplicate deletion blocked'); END;");
    await assert.rejects(duplicateReport(database, true), /duplicate deletion blocked/);
    assert.equal((await database.query('SELECT shop_id FROM shop_mentions WHERE shop_id=4', z.object({ shop_id: z.number() }))).length, 1);
    assert.equal((await database.query('SELECT source_shop_id FROM shop_redirects', z.object({ source_shop_id: z.number() }))).length, 0);
    assert.equal((await database.query('SELECT id FROM review_events', z.object({ id: z.number() }))).length, 0);
  } finally { await database.close(); }
});
