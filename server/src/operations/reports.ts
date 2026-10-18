import { access } from 'node:fs/promises';
import { z } from 'zod';
import type { Executor, SqlValue } from '../db/database.js';
import { countRow } from '../db/database.js';
import { ImageCache, processedImagePath } from '../image-cache.js';
import { assetRecord } from '../records.js';

const integer = z.coerce.number().int().nonnegative();
const stageSchema = z.object({ stage: z.string(), runs: integer, model_calls: integer, web_search_calls: integer, input_tokens: integer, output_tokens: integer, estimated_cost_microusd: integer, failed_runs: integer });
export interface ReportOptions { baselineCalls: number | null; baselineCost: number | null; assertTargets: boolean; }
export interface ProcessingReport { processed_messages: number; model_calls: number; web_search_calls: number; billed_calls: number; estimated_cost_microusd: number; call_reduction_percent: number | null; cost_reduction_percent: number | null; stages: z.infer<typeof stageSchema>[]; }
function reduction(current: number, baseline: number | null): number | null { return baseline === null ? null : Math.round((baseline - current) / baseline * 10000) / 100; }
export async function processingReport(connection: Executor, options: ReportOptions): Promise<ProcessingReport> {
  for (const baseline of [options.baselineCalls, options.baselineCost]) if (baseline !== null) z.number().int().positive().parse(baseline);
  if (options.assertTargets && (options.baselineCalls === null || options.baselineCost === null)) throw new Error('Target checks require both baseline values');
  const stages = await connection.query("SELECT stage,COUNT(*) AS runs,SUM(CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) AS model_calls,SUM(web_search_calls) AS web_search_calls,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(estimated_cost_microusd) AS estimated_cost_microusd,SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_runs FROM processing_runs GROUP BY stage ORDER BY stage", stageSchema);
  const calls = stages.reduce((sum, row) => sum + row.model_calls, 0); const searches = stages.reduce((sum, row) => sum + row.web_search_calls, 0); const cost = stages.reduce((sum, row) => sum + row.estimated_cost_microusd, 0);
  const processed = (await connection.query("SELECT COUNT(*) AS count FROM messages WHERE processing_status IN ('succeeded','ignored')", countRow))[0]?.count ?? 0;
  return { processed_messages: processed, model_calls: calls, web_search_calls: searches, billed_calls: calls + searches, estimated_cost_microusd: cost, call_reduction_percent: reduction(calls + searches, options.baselineCalls), cost_reduction_percent: reduction(cost, options.baselineCost), stages };
}
export function reportTargetFailures(report: ProcessingReport): string[] {
  const failures: string[] = [];
  if (report.processed_messages !== 260) failures.push(`processed_messages=${report.processed_messages}, expected=260`);
  if ((report.call_reduction_percent ?? 0) < 40) failures.push(`call_reduction_percent=${report.call_reduction_percent}, expected>=40`);
  if ((report.cost_reduction_percent ?? 0) < 30) failures.push(`cost_reduction_percent=${report.cost_reduction_percent}, expected>=30`);
  return failures;
}
export interface CacheOptions { apply: boolean; force: boolean; limit: number | null; messageIds: readonly string[]; }
export interface CacheCounts { candidates: number; created: number; already_cached: number; failed: number; }
async function exists(path: string): Promise<boolean> { try { await access(path); return true; } catch (error) { if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return false; throw error; } }
export async function cacheShopImages(connection: Executor, directory: string, options: CacheOptions): Promise<CacheCounts> {
  if (options.limit !== null) z.number().int().positive().parse(options.limit);
  if (options.limit !== null && options.messageIds.length) throw new Error('Limit and message IDs cannot be combined');
  for (const id of options.messageIds) z.string().regex(/^\d{17,20}$/).parse(id);
  const values: SqlValue[] = []; let sql = "SELECT a.* FROM source_assets a WHERE a.kind='image' AND a.fetch_status<>'unavailable' AND EXISTS(SELECT 1 FROM shop_mentions m WHERE m.message_id=a.message_id AND m.review_status='approved' AND m.metadata_review_status='approved')";
  if (options.messageIds.length) { values.push(...options.messageIds); sql += ' AND a.message_id IN (' + values.map((_, index) => '$' + (index + 1)).join(',') + ')'; }
  sql += ' ORDER BY a.id'; if (options.limit !== null) { values.push(options.limit); sql += ' LIMIT $' + values.length; }
  const assets = await connection.query(sql, assetRecord, values); const counts: CacheCounts = { candidates: assets.length, created: 0, already_cached: 0, failed: 0 }; const failures: Error[] = []; const cache = new ImageCache(directory);
  for (const asset of assets) {
    try {
      const cached = await exists(processedImagePath(asset.url, directory));
      if (!options.apply) { if (cached) counts.already_cached++; continue; }
      await cache.processed(asset.url, options.force); if (cached && !options.force) counts.already_cached++; else counts.created++;
    } catch (error) { counts.failed++; failures.push(new Error(`Image processing failed: asset_id=${asset.id}; message_id=${asset.message_id}; host=${new URL(asset.url).hostname}`, { cause: error })); }
  }
  if (failures.length) throw new AggregateError(failures, `Image processing failed: failed=${counts.failed}; candidates=${counts.candidates}`);
  return counts;
}
