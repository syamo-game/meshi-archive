import 'dotenv/config';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { z } from 'zod';
import { readAISettings } from '../ai.js';
import { openDatabase } from '../db/database.js';
import { verifyDatabase } from '../db/migrate.js';
import { MessageEvaluator } from '../evaluation.js';
import { duplicateReport } from './duplicates.js';
import { cacheShopImages, processingReport, reportTargetFailures } from './reports.js';
import { restoreSources } from './restore.js';
import { transferDatabase, validateTransferUrls } from './transfer.js';

const commandSchema = z.enum(['report', 'duplicates', 'cache-images', 'restore-sources', 'evaluate', 'transfer']);
const positive = (value: string | undefined): number | null => value === undefined ? null : z.coerce.number().int().positive().parse(value);
export async function runOperation(args: string[], environment: NodeJS.ProcessEnv = process.env): Promise<void> {
  const [raw, ...rest] = args;
  if (!raw || raw === '--help') { console.info('Usage: npm run ops -- <report|duplicates|cache-images|restore-sources|evaluate|transfer> [options]\nMutating duplicate, image-cache and source-restore operations require --apply.\nEvaluation updates review differences. Transfer requires --apply and CONFIRM_TARGET_DATABASE_URL; --verify-only compares existing data.'); return; }
  const command = commandSchema.parse(raw);
  const supported: Readonly<Record<z.infer<typeof commandSchema>, readonly string[]>> = {
    report: ['baseline-calls', 'baseline-cost', 'assert-targets'], duplicates: ['apply'],
    'cache-images': ['apply', 'force', 'limit', 'message-id'], 'restore-sources': ['apply', 'limit', 'message-id', 'channel-id'],
    evaluate: ['limit', 'concurrency'], transfer: ['apply', 'verify-only'],
  };
  const { values, tokens } = parseArgs({ args: rest, strict: true, tokens: true, options: { apply: { type: 'boolean', default: false }, force: { type: 'boolean', default: false }, limit: { type: 'string' }, 'message-id': { type: 'string', multiple: true }, 'channel-id': { type: 'string' }, concurrency: { type: 'string' }, 'baseline-calls': { type: 'string' }, 'baseline-cost': { type: 'string' }, 'assert-targets': { type: 'boolean', default: false }, 'verify-only': { type: 'boolean', default: false } } });
  for (const token of tokens) if (token.kind === 'option' && !supported[command].includes(token.name)) throw new Error(`Option --${token.name} does not apply to ${command}`);
  if (command === 'transfer') {
    if (values.apply === values['verify-only']) throw new Error('Transfer requires exactly one of --apply or --verify-only');
    const sourceUrl = z.string().min(1).parse(environment.SOURCE_DATABASE_URL); const targetUrl = z.string().min(1).parse(environment.TARGET_DATABASE_URL);
    validateTransferUrls(sourceUrl, targetUrl, environment.CONFIRM_TARGET_DATABASE_URL ?? '');
    const source = openDatabase(sourceUrl); const target = openDatabase(targetUrl);
    try { await verifyDatabase(target); console.info(JSON.stringify(await transferDatabase(source, target, values.apply ? 'copy' : 'verify'), null, 2)); }
    finally { await Promise.all([source.close(), target.close()]); }
    return;
  }
  const database = openDatabase(environment.DATABASE_URL ?? 'sqlite:///meshi.db');
  try {
    await verifyDatabase(database);
    if (command === 'report') {
      const report = await processingReport(database, { baselineCalls: positive(values['baseline-calls']), baselineCost: positive(values['baseline-cost']), assertTargets: values['assert-targets'] });
      console.info(JSON.stringify(report, null, 2)); const failures = values['assert-targets'] ? reportTargetFailures(report) : [];
      if (failures.length) throw new Error('Processing targets were not met: ' + failures.join('; ')); return;
    }
    if (command === 'duplicates') { console.info(JSON.stringify(await duplicateReport(database, values.apply), null, 2)); return; }
    const directory = resolve(environment.SHOP_IMAGE_CACHE_DIR ?? '.shop-image-cache'); const limit = positive(values.limit);
    if (command === 'cache-images') { console.info(JSON.stringify(await cacheShopImages(database, directory, { apply: values.apply, force: values.force, limit, messageIds: values['message-id'] ?? [] }), null, 2)); return; }
    if (command === 'restore-sources') {
      const result = await restoreSources(database, environment.DISCORD_TOKEN ?? '', { apply: values.apply, limit, messageIds: values['message-id'] ?? [], channelId: values['channel-id'] ?? environment.DISCORD_CHANNEL_ID ?? null });
      console.info(JSON.stringify(result, null, 2)); if (result.failed) throw new Error(`Source restoration failed: failed=${result.failed}`); return;
    }
    const concurrency = z.coerce.number().int().min(1).max(8).parse(values.concurrency ?? 2);
    const evaluator = new MessageEvaluator(database, readAISettings(environment), directory, z.coerce.number().int().positive().parse(environment.LOOKUP_CACHE_DAYS ?? 30));
    const messages = await database.query('SELECT message_id FROM messages ORDER BY message_id' + (limit === null ? '' : ' LIMIT $1'), z.object({ message_id: z.string() }), limit === null ? [] : [limit]);
    await evaluator.preflightModels(); const failures: Error[] = []; let cursor = 0;
    await Promise.all(Array.from({ length: concurrency }, async () => {
      for (;;) {
        const message = messages[cursor++]; if (!message) return;
        try { console.info(JSON.stringify(await evaluator.evaluate(message.message_id))); }
        catch (error) { console.error('Evaluation failed', { messageId: message.message_id, error }); failures.push(new Error(`Evaluation failed: message_id=${message.message_id}`, { cause: error })); }
      }
    }));
    if (failures.length) throw new AggregateError(failures, `Evaluation failed for ${failures.length} messages`);
  } finally { await database.close(); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { await runOperation(process.argv.slice(2)); }
  catch (error) { console.error('Operation failed', error); process.exitCode = 1; }
}
