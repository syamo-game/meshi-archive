import assert from 'node:assert/strict';
import { test } from 'node:test';
import { lineOverlapsPolygon } from '../src/operations/region-builder.js';

test('station membership counts positive overlap and rejects a single-point touch', () => {
  const polygon: Parameters<typeof lineOverlapsPolygon>[1] = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]];
  assert.equal(lineOverlapsPolygon([[-1, 5], [11, 5]], polygon), true);
  assert.equal(lineOverlapsPolygon([[-1, -1], [0, 0]], polygon), false);
  assert.equal(lineOverlapsPolygon([[0, 0], [10, 0]], polygon), true);
  assert.equal(lineOverlapsPolygon([[5, 5], [5, 5]], polygon), false);
  assert.equal(lineOverlapsPolygon([[11, 0], [11, 10]], polygon), false);
});

test('station membership excludes polygon holes while retaining their boundary lines', () => {
  const polygon: Parameters<typeof lineOverlapsPolygon>[1] = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[2, 2], [8, 2], [8, 8], [2, 8], [2, 2]]];
  assert.equal(lineOverlapsPolygon([[3, 3], [7, 7]], polygon), false);
  assert.equal(lineOverlapsPolygon([[2, 2], [8, 2]], polygon), true);
  assert.equal(lineOverlapsPolygon([[3, 3], [9, 9]], polygon), true);
});
