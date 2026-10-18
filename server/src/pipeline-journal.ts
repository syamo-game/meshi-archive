import { createHash } from 'node:crypto';
import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import type { AISettings } from './ai.js';
import type { ModelCallMetrics } from './ai-models.js';
import type { Executor } from './db/database.js';
import { timestampColumn } from './db/database.js';

interface RunSnapshot { messageId: string; stage: string; model: string | null; status: 'succeeded' | 'failed'; input: number; output: number; searches: number; images: number; latency: number; cost: number; error: string | null; }
interface CacheSnapshot { kind: string; value: string; payload: string; }
const cacheRow = z.object({ payload: z.string(), expires_at: timestampColumn });

export function lookupCacheKey(kind: string, value: string, settings: AISettings): string {
  let namespace = `${kind}:${contract.versions.extraction}`;
  if (kind === 'source_discovery' || kind === 'source_mention_evidence_v2') namespace += `:${settings.EXTRACTION_MODEL}:${settings.EXTRACTION_REASONING_EFFORT}`;
  else if (kind === 'web_search') namespace += `:${settings.RESOLUTION_MODEL}:${settings.RESOLUTION_REASONING_EFFORT}`;
  return createHash('sha256').update(`${namespace}:${value}`).digest('hex');
}

export class PipelineJournal {
  private readonly runs: RunSnapshot[] = [];
  private readonly caches = new Map<string, CacheSnapshot>();
  private readonly invalidated = new Set<string>();
  constructor(private readonly settings: AISettings, private readonly cacheDays: number = 30) { z.number().int().min(1).parse(cacheDays); }
  metrics(messageId: string, stage: string, metrics: ModelCallMetrics): void {
    this.runs.push({ messageId, stage, model: metrics.model, status: 'succeeded', input: metrics.input_tokens, output: metrics.output_tokens, searches: metrics.web_search_calls, images: metrics.image_count, latency: metrics.latency_ms, cost: metrics.estimated_cost_microusd, error: null });
    for (let attempt = 2; attempt <= metrics.api_attempts; attempt++) this.runs.push({ messageId, stage: stage + '_retry_attempt', model: metrics.model, status: 'failed', input: 0, output: 0, searches: 0, images: 0, latency: 0, cost: 0, error: `attempt=${attempt}; cost included in aggregate metrics` });
  }
  failure(messageId: string, stage: string, error: Error): void { this.runs.push({ messageId, stage, model: null, status: 'failed', input: 0, output: 0, searches: 0, images: 0, latency: 0, cost: 0, error: `${error.name}: ${error.message}` }); }
  async get(connection: Executor, kind: string, value: string, now: Date = new Date()): Promise<string | null> {
    const key = lookupCacheKey(kind, value, this.settings); const staged = this.caches.get(key);
    if (staged) return staged.payload;
    if (this.invalidated.has(key)) return null;
    const cached = (await connection.query('SELECT payload,expires_at FROM lookup_cache WHERE cache_key=$1', cacheRow, [key]))[0];
    return cached && Date.parse(cached.expires_at) > now.getTime() ? cached.payload : null;
  }
  put(kind: string, value: string, payload: string): void { this.caches.set(lookupCacheKey(kind, value, this.settings), { kind, value, payload }); }
  invalidate(kind: string, value: string): void { const key = lookupCacheKey(kind, value, this.settings); this.invalidated.add(key); this.caches.delete(key); }
  async persist(connection: Executor, now: Date = new Date()): Promise<void> {
    const created = now.toISOString(); const expires = new Date(now.getTime() + this.cacheDays * 86400000).toISOString();
    for (const run of this.runs) await connection.execute('INSERT INTO processing_runs(message_id,stage,model,prompt_version,status,input_tokens,output_tokens,web_search_calls,image_count,latency_ms,estimated_cost_microusd,error,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)', [run.messageId, run.stage, run.model, contract.versions.extraction, run.status, run.input, run.output, run.searches, run.images, run.latency, run.cost, run.error, created]);
    for (const key of this.invalidated) await connection.execute('DELETE FROM lookup_cache WHERE cache_key=$1', [key]);
    for (const [key, cache] of this.caches) await connection.execute('INSERT INTO lookup_cache(cache_key,kind,payload,expires_at,created_at) VALUES($1,$2,$3,$4,$5) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at,created_at=excluded.created_at', [key, cache.kind, cache.payload, expires, created]);
  }
}

