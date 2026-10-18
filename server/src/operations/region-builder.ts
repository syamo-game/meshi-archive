import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { crc32, inflateRawSync } from 'node:zlib';
import { parse } from 'csv-parse/sync';
import { stringify } from 'csv-stringify/sync';
import { z } from 'zod';
import { compareText } from '../text.js';

export const regionSources = [
  { filename: 'mt_city_all.csv.zip', url: 'https://data.address-br.digital.go.jp/mt_city/mt_city_all.csv.zip', sha256: 'c719e7394489907c6192f58837d7f61274c05feb72ebec2543995d0d6cb08b5e' },
  { filename: 'N02-25_GML.zip', url: 'https://nlftp.mlit.go.jp/ksj/gml/data/N02/N02-25/N02-25_GML.zip', sha256: 'aaf76af133b2e771e538fabc4646d2e443dc1d5a67b221382a28d744e706cc9f' },
  { filename: 'N03-20260101_13_GML.zip', url: 'https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2026/N03-20260101_13_GML.zip', sha256: '94f10b26256566db970dd74b09d614f059c1e8a432f9244ac9c4add76c32ff16' },
] as const;
const position = z.tuple([z.number().finite(), z.number().finite()]);
type Point = z.infer<typeof position>;
const ringSchema = z.array(position).min(4).refine(points => { const first = points[0]; const last = points.at(-1); return first && last && first[0] === last[0] && first[1] === last[1]; }, 'Boundary rings must be closed');
const polygonSchema = z.array(ringSchema).min(1);
type Polygon = z.infer<typeof polygonSchema>;
const stationsSchema = z.object({ type: z.literal('FeatureCollection'), features: z.array(z.object({ type: z.literal('Feature'), properties: z.object({ N02_005: z.string(), N02_005c: z.string() }), geometry: z.object({ type: z.literal('LineString'), coordinates: z.array(position).min(2) }) })) });
const boundariesSchema = z.object({ type: z.literal('FeatureCollection'), features: z.array(z.object({ type: z.literal('Feature'), properties: z.object({ N03_004: z.string(), N03_007: z.string() }), geometry: z.object({ type: z.literal('MultiPolygon'), coordinates: z.array(polygonSchema).min(1) }) })) });
interface Municipality { code: string; prefecture_code: string; prefecture: string; county: string; city: string; municipality: string; }
interface Station { station_name: string; municipality_code: string; source_station_id: string; }

export function archiveMember(archive: Buffer, requested: string): string {
  let end = archive.length - 22;
  while (end >= Math.max(0, archive.length - 65557) && archive.readUInt32LE(end) !== 0x06054b50) end--;
  if (end < 0 || archive.readUInt32LE(end) !== 0x06054b50) throw new Error('ZIP directory is missing');
  if (archive.readUInt16LE(end + 4) || archive.readUInt16LE(end + 6)) throw new Error('Split ZIP archives are unsupported');
  const entries = archive.readUInt16LE(end + 10); let cursor = archive.readUInt32LE(end + 16);
  for (let index = 0; index < entries; index++) {
    if (archive.readUInt32LE(cursor) !== 0x02014b50) throw new Error('Invalid ZIP central directory');
    const flags = archive.readUInt16LE(cursor + 8); const method = archive.readUInt16LE(cursor + 10); const checksum = archive.readUInt32LE(cursor + 16); const size = archive.readUInt32LE(cursor + 20); const expanded = archive.readUInt32LE(cursor + 24);
    const nameLength = archive.readUInt16LE(cursor + 28); const extra = archive.readUInt16LE(cursor + 30); const comment = archive.readUInt16LE(cursor + 32); const offset = archive.readUInt32LE(cursor + 42);
    const name = archive.subarray(cursor + 46, cursor + 46 + nameLength).toString('utf8'); cursor += 46 + nameLength + extra + comment;
    if (name !== requested) continue;
    if (flags & 1 || ![0, 8].includes(method) || expanded > 256 * 1024 * 1024) throw new Error(`Unsupported ZIP entry: ${name}`);
    if (archive.readUInt32LE(offset) !== 0x04034b50) throw new Error(`ZIP local header is invalid: ${name}`);
    const start = offset + 30 + archive.readUInt16LE(offset + 26) + archive.readUInt16LE(offset + 28);
    const compressed = archive.subarray(start, start + size); const content = method === 8 ? inflateRawSync(compressed, { maxOutputLength: 256 * 1024 * 1024 }) : compressed;
    if (content.length !== expanded || crc32(content) !== checksum) throw new Error(`ZIP entry checksum differs: ${name}`);
    return new TextDecoder('utf-8', { fatal: true }).decode(content);
  }
  throw new Error(`ZIP entry is missing: ${requested}`);
}
export function loadMunicipalities(csv: string): Municipality[] {
  const rows = z.array(z.object({ lg_code: z.string().regex(/^\d{6}$/), ablt_date: z.literal(''), pref: z.string(), county: z.string(), city: z.string().min(1), ward: z.string() })).parse(parse(csv, { columns: true, bom: true }));
  const result = rows.map(row => ({ code: row.lg_code, prefecture_code: row.lg_code.slice(0, 2), prefecture: row.pref, county: row.county, city: row.ward ? row.city : '', municipality: row.ward || row.city }));
  if (new Set(result.map(row => row.code)).size !== result.length) throw new Error('Municipality codes are duplicated');
  if (new Set(result.map(row => row.prefecture_code)).size !== 47 || result.filter(row => row.prefecture_code === '13').length !== 62) throw new Error('Municipality source must cover 47 prefectures and 62 Tokyo municipalities');
  return result.sort((a, b) => compareText(a.code, b.code));
}
const cross = (a: Point, b: Point): number => a[0] * b[1] - a[1] * b[0];
const subtract = (a: Point, b: Point): Point => [a[0] - b[0], a[1] - b[1]];
function onSegment(point: Point, a: Point, b: Point): boolean { return Math.abs(cross(subtract(point, a), subtract(b, a))) < 1e-14 && point[0] >= Math.min(a[0], b[0]) - 1e-12 && point[0] <= Math.max(a[0], b[0]) + 1e-12 && point[1] >= Math.min(a[1], b[1]) - 1e-12 && point[1] <= Math.max(a[1], b[1]) + 1e-12; }
function inRing(point: Point, ring: readonly Point[]): 'inside' | 'outside' | 'boundary' {
  let inside = false;
  for (let index = 1; index < ring.length; index++) {
    const a = ring[index - 1]; const b = ring[index]; if (!a || !b) throw new Error('Boundary point is missing');
    if (onSegment(point, a, b)) return 'boundary';
    if ((a[1] > point[1]) !== (b[1] > point[1]) && point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
  }
  return inside ? 'inside' : 'outside';
}
function covered(point: Point, polygon: Polygon): boolean {
  const outer = polygon[0]; if (!outer) throw new Error('Polygon has no outer ring'); const relation = inRing(point, outer);
  if (relation === 'outside') return false; if (relation === 'boundary') return true;
  for (const hole of polygon.slice(1)) { const inner = inRing(point, hole); if (inner === 'boundary') return true; if (inner === 'inside') return false; }
  return true;
}
export function lineOverlapsPolygon(line: readonly Point[], polygon: Polygon): boolean {
  for (let index = 1; index < line.length; index++) {
    const a = line[index - 1]; const b = line[index]; if (!a || !b) throw new Error('Station line point is missing'); const direction = subtract(b, a); if (!direction[0] && !direction[1]) continue;
    const splits = [0, 1]; const axis = Math.abs(direction[0]) >= Math.abs(direction[1]) ? 0 : 1;
    for (const ring of polygon) for (let edge = 1; edge < ring.length; edge++) {
      const c = ring[edge - 1]; const d = ring[edge]; if (!c || !d) throw new Error('Boundary edge is missing');
      const segment = subtract(d, c); const offset = subtract(c, a); const determinant = cross(direction, segment);
      if (Math.abs(determinant) < 1e-20) { if (Math.abs(cross(offset, direction)) < 1e-14) for (const point of [c, d]) { const t = (point[axis] - a[axis]) / direction[axis]; if (t > 0 && t < 1) splits.push(t); } }
      else { const t = cross(offset, segment) / determinant; const u = cross(offset, direction) / determinant; if (t > 0 && t < 1 && u >= 0 && u <= 1) splits.push(t); }
    }
    splits.sort((left, right) => left - right);
    for (let part = 1; part < splits.length; part++) { const left = splits[part - 1]; const right = splits[part]; if (left === undefined || right === undefined || right - left < 1e-12) continue; const t = (left + right) / 2; if (covered([a[0] + direction[0] * t, a[1] + direction[1] * t], polygon)) return true; }
  }
  return false;
}
export function loadStations(stationJson: string, boundaryJson: string, municipalities: readonly Municipality[]): Station[] {
  const stations = stationsSchema.parse(JSON.parse(stationJson)); const boundaries = boundariesSchema.parse(JSON.parse(boundaryJson));
  const codes = new Map(municipalities.filter(row => row.prefecture_code === '13').map(row => [row.code.slice(0, 5), row.code]));
  const polygons = boundaries.features.flatMap(feature => {
    if (feature.properties.N03_007 === '13000') return [];
    const code = codes.get(feature.properties.N03_007); if (!code) throw new Error(`Unknown Tokyo municipality: ${feature.properties.N03_007}`);
    return feature.geometry.coordinates.map(polygon => { const points = polygon.flat(); return { code, polygon, minX: Math.min(...points.map(point => point[0])), maxX: Math.max(...points.map(point => point[0])), minY: Math.min(...points.map(point => point[1])), maxY: Math.max(...points.map(point => point[1])) }; });
  });
  const grouped = new Map<string, { station_name: string; municipality_code: string; ids: Set<string> }>();
  for (const station of stations.features) {
    const line = station.geometry.coordinates; const minX = Math.min(...line.map(point => point[0])); const maxX = Math.max(...line.map(point => point[0])); const minY = Math.min(...line.map(point => point[1])); const maxY = Math.max(...line.map(point => point[1]));
    for (const boundary of polygons) {
      if (minX > boundary.maxX || maxX < boundary.minX || minY > boundary.maxY || maxY < boundary.minY || !lineOverlapsPolygon(line, boundary.polygon)) continue;
      const key = JSON.stringify([boundary.code, station.properties.N02_005]); const group = grouped.get(key) ?? { station_name: station.properties.N02_005, municipality_code: boundary.code, ids: new Set<string>() }; group.ids.add(station.properties.N02_005c); grouped.set(key, group);
    }
  }
  if (!grouped.size) throw new Error('No station lines overlap Tokyo municipality boundaries');
  return [...grouped.values()].sort((a, b) => compareText(a.municipality_code, b.municipality_code) || compareText(a.station_name, b.station_name)).map(row => ({ station_name: row.station_name, municipality_code: row.municipality_code, source_station_id: [...row.ids].sort(compareText).join(';') }));
}
export async function buildRegionMaster(sourceDirectory: string, outputDirectory: string, download: boolean): Promise<void> {
  const archives: Buffer[] = [];
  for (const source of regionSources) {
    const path = join(sourceDirectory, source.filename);
    if (download) { const response = await fetch(source.url, { signal: AbortSignal.timeout(120000), redirect: 'error' }); if (!response.ok) throw new Error(`Region download failed: url=${source.url}; status=${response.status}`); await mkdir(sourceDirectory, { recursive: true }); await writeFile(path, Buffer.from(await response.arrayBuffer())); }
    const bytes = await readFile(path); const actual = createHash('sha256').update(bytes).digest('hex'); if (actual !== source.sha256) throw new Error(`Source checksum changed: url=${source.url}; expected=${source.sha256}; actual=${actual}`); archives.push(bytes);
  }
  const [cities, railways, boundaries] = archives; if (!cities || !railways || !boundaries) throw new Error('Region source archive is missing');
  const municipalities = loadMunicipalities(archiveMember(cities, 'mt_city_all.csv')); const stations = loadStations(archiveMember(railways, 'N02-25_GML/UTF-8/N02-25_Station.geojson'), archiveMember(boundaries, 'N03-20260101_13.geojson'), municipalities);
  await mkdir(outputDirectory, { recursive: true });
  await writeFile(join(outputDirectory, 'municipalities.csv'), stringify(municipalities, { header: true, columns: ['code', 'prefecture_code', 'prefecture', 'county', 'city', 'municipality'], record_delimiter: '\n' }));
  await writeFile(join(outputDirectory, 'tokyo_stations.csv'), stringify(stations, { header: true, columns: ['station_name', 'municipality_code', 'source_station_id'], record_delimiter: '\n' }));
  console.info(`Wrote ${municipalities.length} municipalities and ${stations.length} station/municipality pairs`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { const { values } = parseArgs({ options: { 'source-dir': { type: 'string' }, 'output-dir': { type: 'string', default: 'web/data' }, download: { type: 'boolean', default: false } } }); await buildRegionMaster(z.string().min(1).parse(values['source-dir']), values['output-dir'], values.download); }
  catch (error) { console.error('Region master build failed', error); process.exitCode = 1; }
}
