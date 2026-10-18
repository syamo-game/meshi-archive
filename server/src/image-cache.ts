import { createHash } from 'node:crypto';
import { lookup } from 'node:dns/promises';
import { readFile } from 'node:fs/promises';
import { request } from 'node:https';
import { BlockList, isIP } from 'node:net';
import type { LookupFunction } from 'node:net';
import { join } from 'node:path';
import sharp from 'sharp';
import { maxImageBytes, maxImagePixels, validateImageSourceUrl, writeImageFile } from './images.js';
import { quotePlus } from './text.js';

const blocked = new BlockList();
for (const [address, prefix] of [['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8], ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24], ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24], ['224.0.0.0', 4], ['240.0.0.0', 4]] as const) blocked.addSubnet(address, prefix, 'ipv4');
for (const [address, prefix] of [['2001::', 23], ['2001:db8::', 32], ['2002::', 16], ['3fff::', 20]] as const) blocked.addSubnet(address, prefix, 'ipv6');
export function publicAddress(address: string): boolean {
  const family = isIP(address); if (family === 4) return !blocked.check(address, 'ipv4');
  return family === 6 && /^[23]/i.test(address) && !blocked.check(address, 'ipv6');
}
export function sourceImagePath(value: string, directory: string): string {
  let source = validateImageSourceUrl(value); const url = new URL(source);
  if (['cdn.discordapp.com', 'media.discordapp.net'].includes(url.hostname) && url.pathname.startsWith('/attachments/')) {
    const query = [...url.searchParams].filter(([key]) => !['ex', 'is', 'hm'].includes(key)).map(([key, item]) => quotePlus(key) + '=' + quotePlus(item)).join('&');
    source = `${url.protocol}//cdn.discordapp.com${url.pathname}${query ? '?' + query : ''}`;
  }
  return join(directory, 'sources', createHash('sha256').update(source).digest('hex') + '.image');
}
export function processedImagePath(value: string, directory: string): string { return join(directory, createHash('sha256').update(validateImageSourceUrl(value)).digest('hex') + '.webp'); }
interface Download { status: number; location: string | null; bytes: Buffer; }
async function downloadRequest(value: string): Promise<Download> {
  const url = new URL(validateImageSourceUrl(value));
  const addresses = await lookup(url.hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some(item => !publicAddress(item.address))) throw new Error(`Image host resolved outside public networks: host=${url.hostname}`);
  const selected = addresses[0]; if (!selected) throw new Error(`Image host has no address: host=${url.hostname}`);
  // Pin the checked address to this TLS connection, including redirects.
  const pinned: LookupFunction = (_host, options, callback) => { if (options.all) callback(null, [selected]); else callback(null, selected.address, selected.family); };
  return new Promise<Download>((resolve, reject) => {
    const outgoing = request(url, { lookup: pinned, agent: false, signal: AbortSignal.timeout(30000), headers: { Accept: 'image/*', 'User-Agent': 'meshi-archive/1.0' } }, response => {
      const status = response.statusCode ?? 0;
      if ([301, 302, 303, 307, 308].includes(status)) { response.resume(); resolve({ status, location: response.headers.location ?? null, bytes: Buffer.alloc(0) }); return; }
      if (status < 200 || status >= 300 || !response.headers['content-type']?.toLowerCase().startsWith('image/')) { response.resume(); reject(new Error(`Image response rejected: host=${url.hostname} status=${status} type=${response.headers['content-type'] ?? '(missing)'}`)); return; }
      const contentLength = Number(response.headers['content-length'] ?? 0);
      if (contentLength > maxImageBytes) { response.destroy(); reject(new Error(`Image content length exceeds limit: host=${url.hostname} bytes=${contentLength}`)); return; }
      const collect = async (): Promise<void> => {
        const chunks: Buffer[] = []; let size = 0;
        for await (const chunk of response) {
          if (!Buffer.isBuffer(chunk)) throw new Error(`Image stream returned a non-binary chunk: host=${url.hostname}`);
          size += chunk.length;
          if (size > maxImageBytes) throw new Error(`Image download exceeded limit: host=${url.hostname} bytes=${size}`);
          chunks.push(chunk);
        }
        resolve({ status, location: null, bytes: Buffer.concat(chunks, size) });
      };
      void collect().catch(reject);
    });
    outgoing.on('error', reject); outgoing.end();
  });
}
export async function downloadImage(value: string): Promise<Buffer> {
  let current = validateImageSourceUrl(value);
  for (let redirects = 0; redirects <= 5; redirects++) {
    const result = await downloadRequest(current);
    if (!result.location) {
      if ([301, 302, 303, 307, 308].includes(result.status)) throw new Error(`Image redirect has no location: host=${new URL(current).hostname} status=${result.status}`);
      return result.bytes;
    }
    if (redirects === 5) throw new Error(`Image redirect limit exceeded: host=${new URL(current).hostname}`);
    current = validateImageSourceUrl(new URL(result.location, current).href);
  }
  throw new Error('Image redirect loop did not terminate');
}
async function validateSource(bytes: Uint8Array): Promise<string> {
  if (!bytes.length || bytes.length > maxImageBytes) throw new Error(`Cached source image has invalid size: bytes=${bytes.length}`);
  const image = sharp(bytes, { limitInputPixels: maxImagePixels, animated: false }); const metadata = await image.metadata();
  if (!metadata.width || !metadata.height || metadata.width * metadata.height > maxImagePixels || !['jpeg', 'png', 'webp', 'gif', 'tiff', 'heif'].includes(metadata.format)) throw new Error(`Source image format or dimensions rejected: format=${metadata.format} width=${metadata.width} height=${metadata.height}`);
  // Decode pixels so a valid header cannot hide a truncated file.
  await image.stats(); return metadata.format === 'heif' ? metadata.compression === 'av1' ? 'image/avif' : 'image/heif' : 'image/' + metadata.format;
}
async function cachedFile(path: string): Promise<Buffer | null> {
  try { return await readFile(path); }
  catch (error) { if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return null; throw error; }
}
export class ImageCache {
  constructor(private readonly directory: string) {}
  async original(source: string, force = false): Promise<{ bytes: Buffer; contentType: string }> {
    const path = sourceImagePath(source, this.directory); const cached = force ? null : await cachedFile(path);
    const bytes = cached ?? await downloadImage(source); const contentType = await validateSource(bytes);
    if (!cached) await writeImageFile(path, bytes);
    return { bytes, contentType };
  }
  async processed(source: string, force = false): Promise<Buffer> {
    const path = processedImagePath(source, this.directory); const cached = force ? null : await cachedFile(path);
    if (cached) return cached;
    const { bytes } = await this.original(source, force);
    const output = await sharp(bytes, { limitInputPixels: maxImagePixels, animated: false }).rotate().resize(960, 540, { fit: 'cover', position: 'centre' }).flatten({ background: '#ffffff' }).webp({ quality: 84, effort: 6 }).toBuffer();
    await writeImageFile(path, output); return output;
  }
  async aiImages(sources: readonly string[]): Promise<readonly string[]> {
    const images: string[] = [];
    for (const source of sources.slice(0, 2)) {
      const bytes = await readFile(sourceImagePath(source, this.directory)); await validateSource(bytes);
      const output = await sharp(bytes, { limitInputPixels: maxImagePixels, animated: false }).rotate().resize(2048, 2048, { fit: 'inside', withoutEnlargement: true }).flatten({ background: '#ffffff' }).jpeg({ quality: 90 }).toBuffer();
      images.push('data:image/jpeg;base64,' + output.toString('base64'));
    }
    return images;
  }
}
