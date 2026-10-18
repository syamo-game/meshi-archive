import { isIP } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import OpenAI from 'openai';
import type { Response, ResponseCreateParamsNonStreaming } from 'openai/resources/responses/responses';
import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import { candidateSearchContextSchema, extractedMessageSchema, imageCluesSchema, requireExtractionEvidence, searchCandidateSetSchema } from './ai-models.js';
import type { CandidateSearchContext, CandidateSearchResult, ExtractedMention, ExtractionCallResult, ImageAnalysisResult, ModelCallMetrics } from './ai-models.js';
import { canonicalArea } from './geography.js';
import { categoryInstructions } from './categories.js';
import { ImageCache, publicAddress } from './image-cache.js';
import { safeHttpUrl } from './links.js';

const effort = z.enum(['low', 'medium', 'high', 'xhigh', 'max']);
const settings = z.object({ OPENAI_API_KEY: z.string().default(''), EXTRACTION_MODEL: z.string().min(1).default('gpt-6.1-sol'), RESOLUTION_MODEL: z.string().min(1).default('gpt-6.1-sol'), EXTRACTION_REASONING_EFFORT: effort.default('high'), RESOLUTION_REASONING_EFFORT: effort.default('high'), AI_MAX_CONCURRENCY: z.coerce.number().int().min(1).max(100).default(2) });
export type AISettings = z.infer<typeof settings>;
export class ExtractionError extends Error { constructor(message: string, options?: ErrorOptions) { super(message, options); this.name = 'ExtractionError'; } }
export function readAISettings(environment: NodeJS.ProcessEnv = process.env): AISettings {
  const result = settings.safeParse(environment);
  if (!result.success) throw new Error('Invalid AI settings: ' + result.error.issues.map(issue => issue.path.join('.') + ': ' + issue.message).join('; '));
  return result.data;
}
export class AICallLimiter {
  private active = 0;
  private readonly pending: (() => void)[] = [];
  constructor(private readonly maximum: number) {}
  async run<Result>(operation: () => Promise<Result>, signal?: AbortSignal): Promise<Result> {
    signal?.throwIfAborted();
    if (this.active >= this.maximum) await new Promise<void>((resolve, reject) => {
      const start = (): void => { signal?.removeEventListener('abort', abort); resolve(); };
      const abort = (): void => { const index = this.pending.indexOf(start); if (index >= 0) this.pending.splice(index, 1); reject(new ExtractionError('AI request was cancelled while queued')); };
      this.pending.push(start); signal?.addEventListener('abort', abort, { once: true });
    });
    else this.active++;
    try { signal?.throwIfAborted(); return await operation(); }
    finally { const next = this.pending.shift(); if (next) next(); else this.active--; }
  }
}
export function completedSearchSources(response: Pick<Response, 'status' | 'parallel_tool_calls' | 'output'>, stage: string): readonly string[] {
  const config = z.object({ max_tool_calls: z.literal(1) }).safeParse(response);
  if (response.status !== 'completed' || !config.success || response.parallel_tool_calls !== false) throw new ExtractionError(`Web search configuration or status changed: stage=${stage} status=${response.status} confirmed_tool_limit=${config.success} parallel=${response.parallel_tool_calls}`);
  const calls = response.output.filter(item => item.type === 'web_search_call'); const completed = calls.filter(item => item.status === 'completed'); const searching = calls.filter(item => item.status === 'searching');
  if (completed.length !== 1 || searching.length > 1 || completed.length + searching.length !== calls.length || calls.some(item => !item.id)) throw new ExtractionError(`Web search did not complete exactly once: stage=${stage} statuses=${calls.map(item => item.status).join(',')}`);
  for (const pending of searching) if (pending.action.type === 'search' && pending.action.sources?.length) throw new ExtractionError(`Pending web search contains sources: stage=${stage}`);
  const action = completed[0]?.action; if (!action) throw new ExtractionError(`Completed web search has no action: stage=${stage}`);
  const urls = action.type === 'search' ? (action.sources ?? []).map(source => source.url) : [action.url]; const result = new Set<string>();
  for (const value of urls) {
    if (typeof value !== 'string' || !safeHttpUrl(value)) throw new ExtractionError(`Web search returned an invalid source URL: stage=${stage}`);
    const host = new URL(value).hostname.replace(/^\[|\]$/g, '');
    if (isIP(host) && !publicAddress(host)) throw new ExtractionError(`Web search returned a non-public source: stage=${stage}`);
    result.add(value);
  }
  return [...result];
}
function roundEven(value: number): number { const floor = Math.floor(value); return value - floor === 0.5 ? floor % 2 === 0 ? floor : floor + 1 : Math.round(value); }
export function estimateCost(model: string, input: number, output: number, searches = 0, environment: NodeJS.ProcessEnv = process.env): number {
  // Preserve the existing estimates; operators can supply actual account prices.
  const defaults: Readonly<Record<string, readonly [number, number]>> = { 'gpt-6.1-sol': [2, 10], 'gpt-5.6-luna': [1, 6], 'gpt-5.6-terra': [2.5, 15] };
  const prefix = model.toUpperCase().replace(/[-.]/g, '_'); const prices = defaults[model] ?? [0, 0];
  const price = (name: string, initial: number): number => z.coerce.number().finite().nonnegative().parse(environment[name] ?? initial);
  return roundEven(input * price(prefix + '_INPUT_USD_PER_M', prices[0]) + output * price(prefix + '_OUTPUT_USD_PER_M', prices[1]) + searches * price('WEB_SEARCH_USD_PER_1K_CALLS', 10) * 1000);
}
export function callMetrics(response: Pick<Response, 'usage' | 'output'>, model: string, elapsed: number, attempts: number, images = 0, search = false, reserve = 500000): ModelCallMetrics {
  const calls = response.output.filter(item => item.type === 'web_search_call'); const input = response.usage?.input_tokens ?? 0; const output = response.usage?.output_tokens ?? 0;
  return { model, input_tokens: input, output_tokens: output, web_search_calls: calls.filter(item => item.status === 'completed').length + Math.max(0, attempts - 1) * Number(search), image_count: images, latency_ms: Math.floor(elapsed), estimated_cost_microusd: estimateCost(model, input, output, calls.length) + Math.max(0, attempts - 1) * reserve, api_attempts: attempts };
}
type ResponseInput = NonNullable<ResponseCreateParamsNonStreaming['input']>;
type SearchParameters = ResponseCreateParamsNonStreaming & { max_tool_calls: 1 };
function responseInput(text: string, images: readonly string[]): ResponseInput { return images.length ? [{ role: 'user', content: [{ type: 'input_text', text }, ...images.map(image => ({ type: 'input_image' as const, image_url: image, detail: 'auto' as const }))] }] : text; }
function withSearch(params: ResponseCreateParamsNonStreaming, cacheKey: string): SearchParameters { return { ...params, tools: [{ type: 'web_search' }], include: ['web_search_call.action.sources'], tool_choice: 'required', max_tool_calls: 1, parallel_tool_calls: false, prompt_cache_key: cacheKey }; }
export class RestaurantAI {
  private client: OpenAI | null = null;
  private readonly semaphore: AICallLimiter;
  private readonly images: ImageCache;
  constructor(private readonly config: AISettings, imageDirectory: string) { this.semaphore = new AICallLimiter(config.AI_MAX_CONCURRENCY); this.images = new ImageCache(imageDirectory); }
  private getClient(): OpenAI {
    if (!this.config.OPENAI_API_KEY) throw new ExtractionError('OPENAI_API_KEY is not configured');
    this.client ??= new OpenAI({ apiKey: this.config.OPENAI_API_KEY, maxRetries: 0 }); return this.client;
  }
  private async request(params: ResponseCreateParamsNonStreaming, stage: string, signal?: AbortSignal): Promise<{ response: Response; attempts: number }> {
    const client = this.getClient();
    for (let attempt = 1; attempt <= 3; attempt++) {
      try { return { response: await this.semaphore.run(() => client.responses.create(params, signal ? { signal } : {}), signal), attempts: attempt }; }
      catch (error) {
        if (!(error instanceof OpenAI.APIError) || !error.status || (error.status !== 429 && (error.status < 500 || error.status >= 600)) || attempt === 3) throw new ExtractionError(`OpenAI request failed: stage=${stage} attempt=${attempt} status=${error instanceof OpenAI.APIError ? error.status : 'transport'} request_id=${error instanceof OpenAI.APIError ? error.requestID : '(none)'}`, { cause: error });
        const header = Number(error.headers?.get('retry-after')); const seconds = Number.isFinite(header) && header > 0 ? header : 2 ** (attempt - 1);
        console.warn('Retrying OpenAI request', { stage, attempt, status: error.status, delay: Math.min(seconds, 30) }); await delay(Math.min(seconds, 30) * 1000, undefined, signal ? { signal } : {});
      }
    }
    throw new ExtractionError(`OpenAI retry loop did not terminate: stage=${stage}`);
  }
  private parameters(model: string, reasoning: z.infer<typeof effort>, prompt: string, input: ResponseInput, schema: keyof typeof contract.schemas): ResponseCreateParamsNonStreaming {
    const instructions = schema === 'message' || schema === 'candidates' ? `${prompt}\n\n${categoryInstructions}` : prompt;
    return { model, instructions, input, text: { format: { type: 'json_schema', name: schema, strict: true, schema: contract.schemas[schema] } }, max_output_tokens: 25000, reasoning: { effort: reasoning }, store: false };
  }
  private parse<Output>(response: Response, schema: z.ZodType<Output>, stage: string): Output {
    if (response.status !== 'completed' || !response.output_text) throw new ExtractionError(`OpenAI returned no complete structured output: stage=${stage} status=${response.status}`);
    try { return schema.parse(JSON.parse(response.output_text)); }
    catch (error) { throw new ExtractionError(`OpenAI output did not match its contract: stage=${stage} response_id=${response.id}`, { cause: error }); }
  }
  async preflightModels(signal?: AbortSignal): Promise<void> {
    const seen = new Set<string>();
    for (const [model, reasoning] of [[this.config.EXTRACTION_MODEL, this.config.EXTRACTION_REASONING_EFFORT], [this.config.RESOLUTION_MODEL, this.config.RESOLUTION_REASONING_EFFORT]] as const) {
      const key = model + ':' + reasoning; if (seen.has(key)) continue; seen.add(key);
      const { response } = await this.request(this.parameters(model, reasoning, 'For this response-format check, return exactly one JSON object: {"ok": true}. Do not add Markdown or other text.', 'preflight', 'preflight'), 'model_preflight', signal);
      if (!this.parse(response, z.strictObject({ ok: z.boolean() }), 'model_preflight').ok) throw new ExtractionError(`Model preflight failed: model=${model}`);
    }
  }
  async extract(text: string, images: readonly string[] = [], signal?: AbortSignal): Promise<ExtractionCallResult> {
    if (!text.trim()) throw new ExtractionError('Cannot extract an empty message'); const started = performance.now();
    const params = this.parameters(this.config.EXTRACTION_MODEL, this.config.EXTRACTION_REASONING_EFFORT, images.length ? contract.prompts.image_extraction : contract.prompts.extraction, responseInput(text.trim(), images), 'message'); params.prompt_cache_key = `meshi:${contract.versions.extraction}:extract`;
    const { response, attempts } = await this.request(params, 'message_extraction', signal);
    return { message: requireExtractionEvidence(this.parse(response, extractedMessageSchema, 'message_extraction')), metrics: callMetrics(response, this.config.EXTRACTION_MODEL, performance.now() - started, attempts, images.length), source_urls: [] };
  }
  async discover(text: string, signal?: AbortSignal): Promise<ExtractionCallResult> {
    if (!text.trim()) throw new ExtractionError('Cannot discover restaurants from empty source evidence'); const started = performance.now();
    const params = withSearch(this.parameters(this.config.EXTRACTION_MODEL, this.config.EXTRACTION_REASONING_EFFORT, contract.prompts.source_discovery, text.trim(), 'message'), `meshi:${contract.versions.discovery}:discover`);
    const { response, attempts } = await this.request(params, 'source_discovery', signal); const sources = completedSearchSources(response, 'source_discovery'); const message = requireExtractionEvidence(this.parse(response, extractedMessageSchema, 'source_discovery'));
    if (message.mentions.length && !sources.length) throw new ExtractionError('Source discovery returned mentions without web sources');
    return { message, metrics: callMetrics(response, this.config.EXTRACTION_MODEL, performance.now() - started, attempts, 0, true), source_urls: sources };
  }
  async search(mention: ExtractedMention, context?: CandidateSearchContext, signal?: AbortSignal): Promise<CandidateSearchResult> {
    const lines = ['店舗名: ' + mention.shop_name]; if (mention.branch_name) lines.push('支店名: ' + mention.branch_name); if (mention.area) lines.push('エリア: ' + (canonicalArea(mention.area) ?? mention.area));
    if (context) { const parsed = candidateSearchContextSchema.parse(context); const { image_inputs: _images, ...visible } = parsed; lines.push('管理者が入力した補足情報（未検証の検索手掛かり）: ' + JSON.stringify(Object.fromEntries(Object.entries(visible).filter(([, value]) => value !== null)))); }
    const images = context?.image_inputs ?? []; const started = performance.now();
    const params = withSearch(this.parameters(this.config.RESOLUTION_MODEL, this.config.RESOLUTION_REASONING_EFFORT, images.length ? contract.prompts.image_search : contract.prompts.search, responseInput(lines.join('\n'), images), 'candidates'), `meshi:${contract.versions.search}:search`);
    const { response, attempts } = await this.request(params, 'candidate_search', signal); const sources = completedSearchSources(response, 'candidate_search'); const candidates = this.parse(response, searchCandidateSetSchema, 'candidate_search');
    if (candidates.candidates.length && !sources.length) throw new ExtractionError('Candidate search returned candidates without web sources');
    return { candidates, metrics: callMetrics(response, this.config.RESOLUTION_MODEL, performance.now() - started, attempts, images.length, true), source_urls: sources };
  }
  async analyzeImages(mention: ExtractedMention | null, urls: readonly string[], signal?: AbortSignal): Promise<ImageAnalysisResult> {
    const selected = urls.slice(0, 2); if (!selected.length) throw new ExtractionError('Image analysis requires at least one image');
    const images = await this.images.aiImages(selected); const started = performance.now(); const text = (mention ? `候補店名: ${mention.shop_name}\n` : '') + `添付画像: ${selected.length}枚`;
    const { response, attempts } = await this.request(this.parameters(this.config.RESOLUTION_MODEL, this.config.RESOLUTION_REASONING_EFFORT, contract.prompts.image_analysis, responseInput(text, images), 'images'), 'image_analysis', signal);
    return { clues: this.parse(response, imageCluesSchema, 'image_analysis'), metrics: callMetrics(response, this.config.RESOLUTION_MODEL, performance.now() - started, attempts, selected.length) };
  }
}
