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
