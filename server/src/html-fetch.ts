import { lookup } from 'node:dns/promises';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import type { LookupFunction } from 'node:net';
import { publicAddress } from './image-cache.js';
import { safeHttpUrl } from './links.js';

export interface HtmlDocument { html: string; final_url: string; }
export class HtmlFetchError extends Error { constructor(message: string, options?: ErrorOptions) { super(message, options); this.name = 'HtmlFetchError'; } }
export async function fetchHtmlDocument(value: string, signal?: AbortSignal): Promise<HtmlDocument> {
  try { return await fetchDocument(value, signal); }
  catch (error) {
    if (error instanceof HtmlFetchError) throw error;
    throw new HtmlFetchError(`HTML request failed: host=${safeHttpUrl(value) ? new URL(value).hostname : '(invalid)'}; reason=${error instanceof Error ? error.message : String(error)}`, { cause: error });
  }
}
async function fetchDocument(value: string, signal?: AbortSignal): Promise<HtmlDocument> {
  let current = value;
  for (let redirects = 0; redirects < 6; redirects++) {
    const safe = safeHttpUrl(current); if (!safe) throw new HtmlFetchError('HTML URL must use HTTP or HTTPS without credentials');
    const url = new URL(safe); let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new HtmlFetchError(`HTML DNS lookup timed out: host=${url.hostname}`)), 5000); });
    const resolved = await (async () => { try { return await Promise.race([lookup(url.hostname.replace(/^\[|\]$/g, ''), { all: true, verbatim: true }), timeout]); } finally { clearTimeout(timer); } })();
    if (!resolved.length || resolved.some(item => !publicAddress(item.address))) throw new HtmlFetchError(`HTML URL resolved outside public networks: host=${url.hostname}`);
    const selected = [...resolved].sort((a, b) => a.family - b.family || (a.address < b.address ? -1 : a.address > b.address ? 1 : 0))[0];
    if (!selected) throw new HtmlFetchError(`HTML URL has no resolved address: host=${url.hostname}`);
    const pinned: LookupFunction = (_host, options, callback) => { if (options.all) callback(null, [selected]); else callback(null, selected.address, selected.family); };
    const activeSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000);
    const result = await new Promise<{ html: string; location: string | null }>((resolve, reject) => {
      const request = url.protocol === 'https:' ? httpsRequest : httpRequest;
      const outgoing = request(url, { lookup: pinned, agent: false, signal: activeSignal, headers: { 'User-Agent': 'MeshiArchive/2.0 (+restaurant metadata fetch)', Accept: 'text/html,application/xhtml+xml' } }, response => {
        const status = response.statusCode ?? 0;
        if ([301, 302, 303, 307, 308].includes(status)) {
          response.resume(); if (!response.headers.location) { reject(new HtmlFetchError(`HTML redirect has no Location: host=${url.hostname} status=${status}`)); return; }
          resolve({ html: '', location: response.headers.location }); return;
        }
        const type = response.headers['content-type'] ?? '';
        if (status >= 400 || (!type.toLowerCase().includes('text/html') && !type.toLowerCase().includes('application/xhtml+xml'))) { response.resume(); reject(new HtmlFetchError(`HTML response rejected: host=${url.hostname} status=${status} type=${type}`)); return; }
        const collect = async (): Promise<void> => {
          const chunks: Buffer[] = []; let size = 0;
          for await (const chunk of response) {
            if (!Buffer.isBuffer(chunk)) throw new HtmlFetchError(`HTML stream was not binary: host=${url.hostname}`);
            size += chunk.length; if (size > 1000000) throw new HtmlFetchError(`HTML content exceeds 1 MB: host=${url.hostname} bytes=${size}`); chunks.push(chunk);
          }
          const charset = /charset\s*=\s*["']?([^;\s"']+)/i.exec(type)?.[1] ?? 'utf-8';
          resolve({ html: new TextDecoder(charset).decode(Buffer.concat(chunks, size)), location: null });
        };
        void collect().catch(reject);
      });
      outgoing.on('error', reject); outgoing.end();
    });
    if (result.location !== null) { current = new URL(result.location, current).href; continue; }
    return { html: result.html, final_url: current };
  }
  throw new HtmlFetchError(`HTML redirect limit exceeded: host=${new URL(value).hostname}`);
}
