import { createHash, randomBytes } from 'node:crypto';
import { mkdir, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import sharp from 'sharp';
import { AppError } from './errors.js';
import { safeHttpUrl } from './links.js';

export const maxImageBytes = 20 * 1024 * 1024;
export const maxImagePixels = 40_000_000;
export interface UploadedImage { key: string; path: string; }
export const imageHosts: ReadonlySet<string> = new Set(['pbs.twimg.com', 'media.discordapp.net', 'cdn.discordapp.com', 'images-ext-1.discordapp.net', 'images-ext-2.discordapp.net', 'www.oimachi-tracks.com']);
export function validateImageSourceUrl(value: string): string {
  const safe = safeHttpUrl(value);
  if (!safe) throw new AppError(422, '画像URLが不正です。');
  const parsed = new URL(safe);
  if (parsed.protocol !== 'https:' || (parsed.port && parsed.port !== '443') || !imageHosts.has(parsed.hostname.toLowerCase().replace(/\.$/, ''))) throw new AppError(422, '画像の取得元は許可されていません。');
  return safe;
}
export async function storeUploadedImage(source: Uint8Array, directory: string): Promise<UploadedImage> {
  if (!source.byteLength || source.byteLength > maxImageBytes) throw new AppError(422, '写真は空でない20MiB以下のファイルを選択してください。');
  const bytes = Buffer.from(source);
  const jpeg = bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  const png = bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  const webp = bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP';
  if (!jpeg && !png && !webp) throw new AppError(422, '写真はJPEG・PNG・WebP形式を選択してください。');
  let output: Buffer;
  try {
    const image = sharp(bytes, { limitInputPixels: maxImagePixels, animated: false });
    const metadata = await image.metadata();
    if (!metadata.width || !metadata.height || metadata.width * metadata.height > maxImagePixels || !['jpeg', 'png', 'webp'].includes(metadata.format)) throw new AppError(422, '写真は4,000万画素以下のJPEG・PNG・WebPを選択してください。');
    output = await image.rotate().resize(960, 540, { fit: 'cover', position: 'centre' }).flatten({ background: '#ffffff' }).webp({ quality: 84, effort: 6 }).toBuffer();
  } catch (error) {
    if (error instanceof AppError) throw error;
    console.warn('Shop image validation failed', { size: source.byteLength, error });
    throw new AppError(422, '写真を読み取れませんでした。正常なJPEG・PNG・WebPを選択してください。');
  }
  const key = createHash('sha256').update(output).digest('hex');
  const uploads = join(directory, 'uploads');
  const path = join(uploads, key + '.webp');
  await writeImageFile(path, output);
  return { key, path };
}
export async function writeImageFile(path: string, bytes: Uint8Array): Promise<void> {
  const directory = dirname(path);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = path + '.' + randomBytes(8).toString('hex') + '.tmp';
  try { await writeFile(temporary, bytes, { flag: 'wx', mode: 0o600 }); await rename(temporary, path); }
  finally { await rm(temporary, { force: true }); }
}
