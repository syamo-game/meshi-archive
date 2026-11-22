import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { candidateAddressMatchesArea, candidateAddressMatchesGroup, candidateAddressPreciselyMatchesArea, candidateAreaContextMatches, canonicalAreasInText, geographicAreaConflict, hasMultipleGeographicValues } from '../src/candidate-geography.js';
import { areaFromAddress, municipalitiesInText } from '../src/region-master.js';
import { compareText } from '../src/text.js';

const reference = z.object({ reference: z.literal('c790aa246f6ff9ce2aec7fc6a2d9e8fe16c4184d'), synthetic: z.literal(true), contexts: z.array(z.object({ area: z.string(), value: z.string(), context: z.boolean(), conflict: z.boolean(), multiple: z.boolean(), embedded: z.array(z.string()) })), addresses: z.array(z.object({ area: z.string(), address: z.string().nullable(), group: z.boolean(), matches: z.boolean(), precise: z.boolean() })), municipalities: z.array(z.object({ address: z.string(), area: z.string().nullable(), codes: z.array(z.string()) })) });
test('area and address safeguards match 208 Python checks and 1918 municipality addresses', async () => {
  const data = reference.parse(JSON.parse(await readFile(new URL('./geography-parity.json', import.meta.url), 'utf8')));
  for (const item of data.contexts) {
    const label = item.area + ' / ' + item.value;
    assert.equal(candidateAreaContextMatches(item.area, item.value), item.context, label);
    assert.equal(geographicAreaConflict(item.area, item.value), item.conflict, label);
    assert.equal(hasMultipleGeographicValues(item.value), item.multiple, label);
    assert.deepEqual([...canonicalAreasInText(item.value)].sort(compareText), item.embedded, label);
  }
  for (const item of data.addresses) {
    const label = item.area + ' / ' + item.address;
    assert.equal(candidateAddressMatchesGroup(item.area, item.address), item.group, label);
    assert.equal(candidateAddressMatchesArea(item.area, item.address), item.matches, label);
    assert.equal(candidateAddressPreciselyMatchesArea(item.area, item.address), item.precise, label);
  }
  for (const item of data.municipalities) {
    assert.equal(areaFromAddress(item.address), item.area, item.address);
    assert.deepEqual(municipalitiesInText(item.address).map(row => row.code), item.codes, item.address);
  }
});


test('filter areas restore municipality hierarchy while keeping counted leaves', async () => {
  const { buildAreaFilterOptions, areaMatches } = await import('../src/geography.js');
  const rows = [{ area: '清澄白河', count: 20 }, { area: '門前仲町', count: 12 }, { area: '北海道札幌市中央区', count: 1 }, { area: '札幌', count: 2 }, { area: '神田', count: 3 }];
  const result = buildAreaFilterOptions(rows);
  assert.deepEqual(result.map(row => [row.value, row.count, row.depth]), [
    ['北海道札幌市', 3, 0], ['札幌', 2, 1], ['北海道札幌市中央区', 1, 1],
    ['東京都千代田区', 3, 0], ['神田', 3, 1], ['東京都江東区', 32, 0], ['清澄白河', 20, 1], ['門前仲町', 12, 1],
  ]);
  assert.deepEqual(buildAreaFilterOptions([...rows].reverse()), result);
  assert.equal(areaMatches('清澄白河', '東京都江東区'), true);
  assert.equal(areaMatches('北海道札幌市中央区', '北海道札幌市'), true);
  assert.equal(areaMatches('神田', '東京都江東区'), false);
});

test('Harumi is a selectable Chuo area and posts naming it resolve to it', async () => {
  const { areaLabel, canonicalArea } = await import('../src/geography.js');
  for (const value of ['晴海', '東京都中央区晴海', '中央区/晴海']) assert.equal(canonicalArea(value), '東京都中央区 / 晴海', value);
  assert.equal(areaLabel('東京都中央区 / 晴海', true), '中央区・晴海');
});

test('a Tokyo ward alone goes to metadata review, while its stations and other cities pass', async () => {
  const { wardOnlyArea } = await import('../src/geography.js');
  const { metadataReviewState } = await import('../src/pipeline-storage.js');
  assert.deepEqual(metadataReviewState('東京都中央区', '居酒屋'), ['pending', 'ward_only_area']);
  assert.deepEqual(metadataReviewState('東京都江東区', '居酒屋'), ['pending', 'ward_only_area']);
  for (const area of ['月島', '東京都中央区 / 晴海', '東京都三鷹市', '京都府京都市東山区']) {
    assert.equal(wardOnlyArea(area), false, area);
    assert.deepEqual(metadataReviewState(area, '居酒屋'), ['approved', null], area);
  }
});

test('every master area except multi-municipality islands has an address mapping', async () => {
  const { areas } = await import('../src/geography.js');
  const { areaMunicipality } = await import('../src/region-master.js');
  assert.deepEqual([...areas.keys()].filter(area => areaMunicipality(area) === null), ['五島列島']);
});

test('a ward or city area becomes the finer area its address names', async () => {
  const { refineAreaByAddress } = await import('../src/candidate-geography.js');
  assert.equal(refineAreaByAddress('東京都中央区', '東京都中央区晴海5丁目6番7'), '東京都中央区 / 晴海');
  assert.equal(refineAreaByAddress('東京都中央区', '東京都中央区月島3-1-1'), '月島');
  assert.equal(refineAreaByAddress('東京都江東区', '東京都江東区豊洲6-5-1'), '豊洲');
  assert.equal(refineAreaByAddress('東京都中央区', '東京都港区芝浦1-1'), '東京都中央区');
  assert.equal(refineAreaByAddress('東京都江東区', null), '東京都江東区');
  assert.equal(refineAreaByAddress('月島', '東京都中央区晴海5丁目'), '月島');
});
