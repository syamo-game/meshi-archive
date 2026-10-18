import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildCategoryFilterGroups, canonicalCategory, categories, categoryGroups, categoryInstructions, categoryMasterSchema, categoryMatches } from '../src/categories.js';

test('category master separates headings, genres and verified styles', () => {
  assert.equal(categoryGroups.length, 15);
  for (const heading of ['和食', '寿司・魚介', 'パスタ・ピザ', '各国料理', '菓子・甘味']) assert.equal(canonicalCategory(heading), null);
  for (const value of ['洋食', 'カレー', '寿司', '回転寿司', '立ち食い寿司', 'ピザ', 'ナポリピザ', 'ニューヨークピザ', 'ジェラート', 'アイスクリーム', 'タコス']) assert.equal(canonicalCategory(value), value);
  assert.equal(categories.size, categoryGroups.flatMap(group => group.options).length);
  assert.deepEqual(categoryGroups.find(group => group.label === 'パスタ・ピザ')?.options, [
    { value: 'パスタ', label: 'パスタ', depth: 0 },
    { value: 'ピザ', label: 'ピザ', depth: 0 },
    ...['ナポリピザ', 'ローマピザ', 'ニューヨークピザ', 'シカゴピザ', 'デトロイトピザ'].map(value => ({ value, label: value, depth: 1 })),
  ]);
  assert.ok(categoryInstructions.includes('ニューヨークピザ'));
  assert.ok(categoryInstructions.includes('根拠で確認できる場合だけ細分類'));
  assert.ok(categoryInstructions.includes('店舗そのものの主な業態・専門性を根拠に分類し、メニューにある料理だけで専門店と判断しない'));
});

test('only same-meaning aliases normalize while ambiguous legacy values remain unresolved', () => {
  const equivalents: readonly [string, string][] = [[' 鮨 ', '寿司'], ['イタリア料理', 'イタリアン'], ['ピッツァ', 'ピザ'], [' ＮＹピザ　', 'ニューヨークピザ'], ['ジェラート専門店', 'ジェラート'], ['タコス専門店', 'タコス']];
  for (const [value, expected] of equivalents) assert.equal(canonicalCategory(value), expected);
  for (const value of [null, '', '　', 'イタリアン・パスタ・ピザ', '寿司・回転寿司', '居酒屋、寿司、海鮮', 'カフェ・喫茶店', 'ジェラート・アイスクリーム', 'その他', '__proto__', 'constructor']) assert.equal(canonicalCategory(value), null);
});

test('category filters include children without inferring styles or reclassifying legacy values', () => {
  assert.equal(categoryMatches('ニューヨークピザ', 'ピザ'), true);
  assert.equal(categoryMatches(' ＮＹピザ ', 'ピッツァ'), true);
  assert.equal(categoryMatches('ピザ', 'ニューヨークピザ'), false);
  assert.equal(categoryMatches('ナポリピザ', 'ニューヨークピザ'), false);
  assert.equal(categoryMatches('イタリアン', 'ピザ'), false);
  assert.equal(categoryMatches('回転ずし', '寿司'), true);
  assert.equal(categoryMatches('四川料理', '中華料理'), true);
  assert.equal(categoryMatches('沖縄料理', '郷土料理'), true);
  assert.equal(categoryMatches('ケーキ', '洋菓子'), true);
  assert.equal(categoryMatches('ジェラート', '洋菓子'), false);
  assert.equal(categoryMatches('寿司・回転寿司', '寿司'), false);
  assert.equal(categoryMatches('寿司・回転寿司', '寿司・回転寿司'), true);
  assert.equal(categoryMatches(' 寿司・回転寿司 ', '寿司・回転寿司'), false);
  for (const value of [null, '', '　']) assert.equal(categoryMatches(value, '__none__'), true);
  assert.equal(categoryMatches('その他', '__none__'), false);
});

test('filter choices use master order, keep populated parents and expose unresolved values separately', () => {
  const values = ['ＮＹピザ', '回転寿司', '鮨', 'ジェラート専門店', 'イタリアン・パスタ・ピザ', '寿司・回転寿司', '回転寿司', null, ''];
  const expected = [
    { label: '寿司・魚介', options: [{ value: '寿司', label: '寿司', depth: 0 }, { value: '回転寿司', label: '回転寿司', depth: 1 }] },
    { label: 'パスタ・ピザ', options: [{ value: 'ピザ', label: 'ピザ', depth: 0 }, { value: 'ニューヨークピザ', label: 'ニューヨークピザ', depth: 1 }] },
    { label: '菓子・甘味', options: [{ value: 'ジェラート', label: 'ジェラート', depth: 0 }] },
    { label: '未整理', options: [{ value: 'イタリアン・パスタ・ピザ', label: 'イタリアン・パスタ・ピザ', depth: 0 }, { value: '寿司・回転寿司', label: '寿司・回転寿司', depth: 0 }] },
  ];
  assert.deepEqual(buildCategoryFilterGroups(values), expected);
  assert.deepEqual(buildCategoryFilterGroups([...values].reverse()), expected);
  assert.deepEqual(buildCategoryFilterGroups(['ピザ']), [{ label: 'パスタ・ピザ', options: [{ value: 'ピザ', label: 'ピザ', depth: 0 }] }]);
  assert.deepEqual(buildCategoryFilterGroups([null, '', '　']), []);
});

test('category master rejects empty groups, duplicate values and invalid alias targets', () => {
  const group = { label: 'ピザ分類', genres: [{ value: 'ピザ', children: ['ニューヨークピザ'] }] };
  assert.equal(categoryMasterSchema.safeParse({ groups: [group], aliases: { 'ピッツァ': 'ピザ' } }).success, true);
  for (const invalid of [
    { groups: [], aliases: {} },
    { groups: [{ label: '空', genres: [] }], aliases: {} },
    { groups: [group, group], aliases: {} },
    { groups: [group, { label: '別分類', genres: [{ value: 'ニューヨークピザ', children: [] }] }], aliases: {} },
    { groups: [{ label: '循環', genres: [{ value: 'ピザ', children: ['ピザ'] }] }], aliases: {} },
    { groups: [{ label: '不明', genres: [{ value: 'ピザ' }] }], aliases: {} },
    { groups: [group], aliases: { '旧分類': '不存在' } },
    { groups: [group], aliases: { 'ピザ': 'ニューヨークピザ' } },
    { groups: [group], aliases: { 'ピザ分類': 'ピザ' } },
    { groups: [group], aliases: { ' ＮＹピザ ': 'ニューヨークピザ' } },
  ]) assert.equal(categoryMasterSchema.safeParse(invalid).success, false, JSON.stringify(invalid));
});
