import { isIP } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import Anthropic from '@anthropic-ai/sdk';
import type { ContentBlock, ImageBlockParam, Message, MessageCreateParamsNonStreaming, TextBlockParam, WebSearchResultBlock, WebSearchTool20250305 } from '@anthropic-ai/sdk/resources/messages/messages';
import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import { candidateSearchContextSchema, clueSearchSchema, extractedMessageSchema, imageCluesSchema, requireExtractionEvidence, searchCandidateSetSchema } from './ai-models.js';
import type { CandidateSearchContext, CandidateSearchResult, ClueSearchResult, ExtractedMention, ExtractionCallResult, ImageAnalysisResult, ModelCallMetrics } from './ai-models.js';
import { canonicalArea } from './geography.js';
import { categories, categoryInstructions } from './categories.js';
import { ImageCache, publicAddress } from './image-cache.js';
import { safeHttpUrl } from './links.js';

const effort = z.enum(['low', 'medium', 'high', 'xhigh', 'max']);
const settings = z.object({ ANTHROPIC_API_KEY: z.string().default(''), EXTRACTION_MODEL: z.string().min(1).default('claude-sonnet-5-5'), RESOLUTION_MODEL: z.string().min(1).default('claude-opus-5-5'), EXTRACTION_REASONING_EFFORT: effort.default('high'), RESOLUTION_REASONING_EFFORT: effort.default('high'), CLUE_SEARCH_REASONING_EFFORT: effort.default('medium'), AI_MAX_CONCURRENCY: z.coerce.number().int().min(1).max(100).default(2) });
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
type SearchView = Pick<Message, 'content' | 'usage'>;
function publicSource(value: string, stage: string): string {
  if (!safeHttpUrl(value)) throw new ExtractionError(`Web search returned an invalid source URL: stage=${stage}`);
  const host = new URL(value).hostname.replace(/^\[|\]$/g, '');
  if (isIP(host) && !publicAddress(host)) throw new ExtractionError(`Web search returned a non-public source: stage=${stage}`);
  return value;
}
// Result lists from searches that succeeded. An error result has no list.
function successfulResults(response: SearchView): WebSearchResultBlock[][] {
  return response.content.flatMap(block => block.type === 'web_search_tool_result' && Array.isArray(block.content) ? [block.content] : []);
}
// For calls allowed exactly one search: it must have run once, been billed once, and returned public URLs.
export function completedSearchSources(response: SearchView, stage: string): readonly string[] {
  const uses = response.content.filter(block => block.type === 'server_tool_use'); const results = response.content.filter(block => block.type === 'web_search_tool_result');
  const succeeded = successfulResults(response); const errors = results.flatMap(block => Array.isArray(block.content) ? [] : [block.content.error_code]);
  // A second attempt past max_uses only returns max_uses_exceeded; it is not a second search.
  if (uses.some(block => block.name !== 'web_search') || uses.length !== results.length || succeeded.length !== 1 || errors.some(code => code !== 'max_uses_exceeded')) throw new ExtractionError(`Web search did not complete exactly once: stage=${stage} tool_uses=${uses.length} results=${results.length} succeeded=${succeeded.length} errors=${errors.join(',') || '(none)'}`);
  const billed = response.usage.server_tool_use?.web_search_requests ?? 0;
  if (billed !== 1) throw new ExtractionError(`Web search usage count is unexpected: stage=${stage} billed=${billed}`);
  return [...new Set((succeeded[0] ?? []).map(result => publicSource(result.url, stage)))];
}
// Every URL the successful searches returned, for calls that may search more than once.
export function searchSources(response: SearchView, stage: string): readonly string[] {
  return [...new Set(successfulResults(response).flat().map(result => publicSource(result.url, stage)))];
}
function roundEven(value: number): number { const floor = Math.floor(value); return value - floor === 0.5 ? floor % 2 === 0 ? floor : floor + 1 : Math.round(value); }
export function estimateCost(model: string, input: number, output: number, searches = 0, environment: NodeJS.ProcessEnv = process.env): number {
  // List prices per million tokens; operators can supply actual account prices.
  const defaults: Readonly<Record<string, readonly [number, number]>> = { 'claude-sonnet-5-5': [2, 10], 'claude-opus-5-5': [4, 20] };
  const prefix = model.toUpperCase().replace(/[-.]/g, '_'); const prices = defaults[model] ?? [0, 0];
  const price = (name: string, initial: number): number => z.coerce.number().finite().nonnegative().parse(environment[name] ?? initial);
  return roundEven(input * price(prefix + '_INPUT_USD_PER_M', prices[0]) + output * price(prefix + '_OUTPUT_USD_PER_M', prices[1]) + searches * price('WEB_SEARCH_USD_PER_1K_CALLS', 10) * 1000);
}
export function callMetrics(response: Pick<Message, 'usage'>, model: string, elapsed: number, attempts: number, images = 0, search = false, reserve = 500000): ModelCallMetrics {
  const usage = response.usage; const input = usage.input_tokens + (usage.cache_creation_input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0); const output = usage.output_tokens;
  const searches = usage.server_tool_use?.web_search_requests ?? 0;
  return { model, input_tokens: input, output_tokens: output, web_search_calls: searches + Math.max(0, attempts - 1) * Number(search), image_count: images, latency_ms: Math.floor(elapsed), estimated_cost_microusd: estimateCost(model, input, output, searches) + Math.max(0, attempts - 1) * reserve, api_attempts: attempts };
}
type MessageInput = string | (TextBlockParam | ImageBlockParam)[];
const imageTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'] as const;
function imageBlock(image: string): ImageBlockParam {
  // Saved images arrive as data URLs; anything else is sent by URL.
  const match = /^data:([^;,]+);base64,(.*)$/s.exec(image);
  if (!match) return { type: 'image', source: { type: 'url', url: image } };
  const mediaType = imageTypes.find(type => type === match[1]);
  if (!mediaType) throw new ExtractionError(`Unsupported image data URL: media_type=${match[1]}`);
  return { type: 'image', source: { type: 'base64', media_type: mediaType, data: match[2] ?? '' } };
}
function messageInput(text: string, images: readonly string[]): MessageInput { return images.length ? [{ type: 'text', text }, ...images.map(imageBlock)] : text; }
// Each photo gets a "写真N" label so the answer can point to one by number.
function numberedPhotos(text: string, images: readonly string[]): MessageInput {
  return images.length ? [{ type: 'text', text }, ...images.flatMap((image, index): (TextBlockParam | ImageBlockParam)[] => [{ type: 'text', text: `写真${index + 1}` }, imageBlock(image)])] : text;
}
function webSearch(maxUses: number): WebSearchTool20250305 { return { type: 'web_search_20250305', name: 'web_search', max_uses: maxUses }; }
// The model may only answer a category with a master value or null.
const categoryChoice = { anyOf: [{ type: 'string', enum: [...categories] }, { type: 'null' }], title: 'Category' };
const messageMention = contract.schemas.message.$defs.ExtractedMention;
const searchCandidate = contract.schemas.candidates.$defs.SearchCandidate;
export const outputSchemas = {
  ...contract.schemas,
  message: { ...contract.schemas.message, $defs: { ...contract.schemas.message.$defs, ExtractedMention: { ...messageMention, properties: { ...messageMention.properties, category: categoryChoice } } } },
  candidates: { ...contract.schemas.candidates, $defs: { SearchCandidate: { ...searchCandidate, properties: { ...searchCandidate.properties, category: categoryChoice } } } },
};
type SchemaNode = { [key: string]: SchemaValue };
type SchemaValue = string | number | boolean | null | SchemaValue[] | SchemaNode;
function isNode(value: SchemaValue | undefined): value is SchemaNode { return typeof value === 'object' && value !== null && !Array.isArray(value); }
// The API compiles at most 16 union-typed fields per request. A nullable enum that already has
// "unknown" means the same without null, so send only the enum; zod still checks the answer.
function collapseUnknownEnums(node: SchemaValue): SchemaValue {
  if (Array.isArray(node)) return node.map(collapseUnknownEnums);
  if (!isNode(node)) return node;
  const branches = node.anyOf; const first = Array.isArray(branches) ? branches[0] : undefined; const second = Array.isArray(branches) ? branches[1] : undefined;
  if (Array.isArray(branches) && branches.length === 2 && isNode(first) && isNode(second) && second.type === 'null' && Object.keys(second).length === 1 && Array.isArray(first.enum) && first.enum.includes('unknown')) {
    const { anyOf: _union, ...rest } = node; return collapseUnknownEnums({ ...rest, ...first });
  }
  return Object.fromEntries(Object.entries(node).map(([key, value]) => [key, collapseUnknownEnums(value)]));
}
// Claude rejects length and count limits in JSON Schema. Move them into the description; zod still enforces them.
// (The SDK's transformJSONSchema also turns enum into text, which would drop the category limit, so it is not used.)
const unsupportedKeywords = ['maxLength', 'minLength', 'maxItems'] as const;
function stripUnsupported(node: SchemaValue): SchemaValue {
  if (Array.isArray(node)) return node.map(stripUnsupported);
  if (!isNode(node)) return node;
  const moved: string[] = []; const kept: SchemaNode = {};
  for (const [key, value] of Object.entries(node)) {
    const unsupported = unsupportedKeywords.some(keyword => keyword === key) || (key === 'minItems' && typeof value === 'number' && value > 1);
    if (unsupported) moved.push(`${key}: ${JSON.stringify(value)}`); else kept[key] = stripUnsupported(value);
  }
  if (moved.length) kept.description = [typeof kept.description === 'string' ? kept.description : '', `{${moved.join(', ')}}`].filter(Boolean).join('\n\n');
  return kept;
}
function schemaValue(value: object): SchemaValue {
  // JSON round trip gives a plain, typed copy of the imported contract.
  const copy: SchemaValue = JSON.parse(JSON.stringify(value)); return copy;
}
export function wireSchema(schema: keyof typeof outputSchemas): SchemaNode {
  const collapsed = collapseUnknownEnums(stripUnsupported(schemaValue(outputSchemas[schema])));
  if (!isNode(collapsed)) throw new ExtractionError(`Output schema is not an object: schema=${schema}`);
  return collapsed;
}
function requestId(response: Message): string { return (response as Message & { _request_id?: string | null })._request_id ?? '(none)'; }
// Text after the last search block is the answer; text before it is preamble.
function answerText(content: readonly ContentBlock[]): string {
  let start = 0; content.forEach((block, index) => { if (block.type === 'server_tool_use' || block.type === 'web_search_tool_result') start = index + 1; });
  return content.slice(start).flatMap(block => block.type === 'text' ? [block.text] : []).join('');
}
export class RestaurantAI {
  private client: Anthropic | null = null;
  private readonly semaphore: AICallLimiter;
  private readonly images: ImageCache;
  constructor(private readonly config: AISettings, imageDirectory: string) { this.semaphore = new AICallLimiter(config.AI_MAX_CONCURRENCY); this.images = new ImageCache(imageDirectory); }
  private getClient(): Anthropic {
    if (!this.config.ANTHROPIC_API_KEY) throw new ExtractionError('ANTHROPIC_API_KEY is not configured');
    this.client ??= new Anthropic({ apiKey: this.config.ANTHROPIC_API_KEY, maxRetries: 0 }); return this.client;
  }
  private async request(params: MessageCreateParamsNonStreaming, stage: string, signal?: AbortSignal): Promise<{ response: Message; attempts: number }> {
    const client = this.getClient();
    for (let attempt = 1; attempt <= 3; attempt++) {
      try { return { response: await this.semaphore.run(() => client.messages.create(params, signal ? { signal } : {}), signal), attempts: attempt }; }
      catch (error) {
        // Retry only rate limits and server errors (529 means overloaded). A low credit balance is a 400 and fails at once.
        if (!(error instanceof Anthropic.APIError) || !error.status || (error.status !== 429 && (error.status < 500 || error.status >= 600)) || attempt === 3) throw new ExtractionError(`Anthropic request failed: stage=${stage} attempt=${attempt} status=${error instanceof Anthropic.APIError ? error.status ?? 'transport' : 'transport'} request_id=${error instanceof Anthropic.APIError ? error.requestID ?? '(none)' : '(none)'} error=${error instanceof Error ? error.message.slice(0, 500) : String(error)}`, { cause: error });
        const header = Number(error.headers?.get('retry-after')); const seconds = Number.isFinite(header) && header > 0 ? header : 2 ** (attempt - 1);
        console.warn('Retrying Anthropic request', { stage, attempt, status: error.status, delay: Math.min(seconds, 30) }); await delay(Math.min(seconds, 30) * 1000, undefined, signal ? { signal } : {});
      }
    }
    throw new ExtractionError(`Anthropic retry loop did not terminate: stage=${stage}`);
  }
  private parameters(model: string, reasoning: z.infer<typeof effort>, prompt: string, input: MessageInput, schema: keyof typeof contract.schemas, searches = 0): MessageCreateParamsNonStreaming {
    const system = schema === 'message' || schema === 'candidates' ? `${prompt}\n\n${categoryInstructions}` : prompt;
    // Thinking stays at the model default; depth is set by effort. 16000 keeps a non-streaming call within the SDK time limit.
    const params: MessageCreateParamsNonStreaming = { model, system, messages: [{ role: 'user', content: input }], max_tokens: 16000, output_config: { effort: reasoning, format: { type: 'json_schema', schema: wireSchema(schema) } } };
    return searches ? { ...params, tools: [webSearch(searches)] } : params;
  }
  private parse<Output>(response: Message, schema: z.ZodType<Output>, stage: string): Output {
    if (response.stop_reason !== 'end_turn') throw new ExtractionError(`Anthropic did not finish its structured output: stage=${stage} stop_reason=${response.stop_reason} request_id=${requestId(response)}`);
    const text = answerText(response.content);
    if (!text) throw new ExtractionError(`Anthropic returned no structured output: stage=${stage} request_id=${requestId(response)}`);
    try { return schema.parse(JSON.parse(text)); }
    catch (error) {
      // Keep which field failed. The stored message is the only record after logs rotate.
      const issues = error instanceof z.ZodError ? error.issues.map(issue => `${issue.path.join('.') || '(root)'}: ${issue.message}`).join('; ') : error instanceof Error ? error.message : String(error);
      throw new ExtractionError(`Anthropic output did not match its contract: stage=${stage} response_id=${response.id} issues=${[...issues].slice(0, 1000).join('')}`, { cause: error });
    }
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
    const params = this.parameters(this.config.EXTRACTION_MODEL, this.config.EXTRACTION_REASONING_EFFORT, images.length ? contract.prompts.image_extraction : contract.prompts.extraction, numberedPhotos(text.trim(), images), 'message');
    const { response, attempts } = await this.request(params, 'message_extraction', signal);
    const message = requireExtractionEvidence(this.parse(response, extractedMessageSchema, 'message_extraction'));
    // A photo number must point to a photo that was sent.
    const outOfRange = message.mentions.flatMap((mention, index) => [[`mentions.${index}.name_photo`, mention.name_photo], [`mentions.${index}.card_photo`, mention.card_photo]] as const).filter(([, number]) => number > images.length);
    if (outOfRange.length) throw new ExtractionError(`Model output did not match the contract: stage=message_extraction photos=${images.length} fields=${outOfRange.map(([field, number]) => `${field}=${number}`).join(',')}`);
    return { message, metrics: callMetrics(response, this.config.EXTRACTION_MODEL, performance.now() - started, attempts, images.length), source_urls: [] };
  }
  async discover(text: string, signal?: AbortSignal): Promise<ExtractionCallResult> {
    if (!text.trim()) throw new ExtractionError('Cannot discover restaurants from empty source evidence'); const started = performance.now();
    const params = this.parameters(this.config.EXTRACTION_MODEL, this.config.EXTRACTION_REASONING_EFFORT, contract.prompts.source_discovery, text.trim(), 'message', 1);
    const { response, attempts } = await this.request(params, 'source_discovery', signal); const message = requireExtractionEvidence(this.parse(response, extractedMessageSchema, 'source_discovery')); const sources = completedSearchSources(response, 'source_discovery');
    if (message.mentions.length && !sources.length) throw new ExtractionError('Source discovery returned mentions without web sources');
    return { message, metrics: callMetrics(response, this.config.EXTRACTION_MODEL, performance.now() - started, attempts, 0, true), source_urls: sources };
  }
  async search(mention: ExtractedMention, context?: CandidateSearchContext, signal?: AbortSignal): Promise<CandidateSearchResult> {
    const lines = ['店舗名: ' + mention.shop_name]; if (mention.branch_name) lines.push('支店名: ' + mention.branch_name); if (mention.area) lines.push('エリア: ' + (canonicalArea(mention.area) ?? mention.area));
    if (context) { const parsed = candidateSearchContextSchema.parse(context); const { image_inputs: _images, ...visible } = parsed; lines.push('管理者が入力した補足情報（未検証の検索手掛かり）: ' + JSON.stringify(Object.fromEntries(Object.entries(visible).filter(([, value]) => value !== null)))); }
    const images = context?.image_inputs ?? []; const started = performance.now();
    const params = this.parameters(this.config.RESOLUTION_MODEL, this.config.RESOLUTION_REASONING_EFFORT, images.length ? contract.prompts.image_search : contract.prompts.search, messageInput(lines.join('\n'), images), 'candidates', 1);
    const { response, attempts } = await this.request(params, 'candidate_search', signal); const candidates = this.parse(response, searchCandidateSetSchema, 'candidate_search'); const sources = completedSearchSources(response, 'candidate_search');
    if (candidates.candidates.length && !sources.length) throw new ExtractionError('Candidate search returned candidates without web sources');
    return { candidates, metrics: callMetrics(response, this.config.RESOLUTION_MODEL, performance.now() - started, attempts, images.length, true), source_urls: sources };
  }
  // Find the shop of a post that names none, from its text and up to four photos, with up to three web searches.
  async clueSearch(text: string, imageSources: readonly string[], signal?: AbortSignal): Promise<ClueSearchResult> {
    if (!text.trim()) throw new ExtractionError('Cannot search clues for an empty message'); const started = performance.now();
    const images = await this.images.aiImages(imageSources, 4);
    const params = this.parameters(this.config.RESOLUTION_MODEL, this.config.CLUE_SEARCH_REASONING_EFFORT, contract.prompts.clue_search, messageInput(`${text.trim()}\n\n添付写真: ${images.length}枚`, images), 'clue_search', 3);
    const { response, attempts } = await this.request(params, 'clue_search', signal);
    const search = this.parse(response, clueSearchSchema, 'clue_search');
    return { search, metrics: callMetrics(response, this.config.RESOLUTION_MODEL, performance.now() - started, attempts, images.length, true), source_urls: searchSources(response, 'clue_search') };
  }
  async analyzeImages(mention: ExtractedMention | null, urls: readonly string[], signal?: AbortSignal): Promise<ImageAnalysisResult> {
    const selected = urls.slice(0, 2); if (!selected.length) throw new ExtractionError('Image analysis requires at least one image');
    const images = await this.images.aiImages(selected); const started = performance.now(); const text = (mention ? `候補店名: ${mention.shop_name}\n` : '') + `添付画像: ${selected.length}枚`;
    const { response, attempts } = await this.request(this.parameters(this.config.RESOLUTION_MODEL, this.config.RESOLUTION_REASONING_EFFORT, contract.prompts.image_analysis, messageInput(text, images), 'images'), 'image_analysis', signal);
    return { clues: this.parse(response, imageCluesSchema, 'image_analysis'), metrics: callMetrics(response, this.config.RESOLUTION_MODEL, performance.now() - started, attempts, selected.length) };
  }
}
