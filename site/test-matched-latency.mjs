import test from 'node:test';
import assert from 'node:assert/strict';
import { pairedLatency } from './src/lib/matched-latency.mjs';

test('compares only matching successful sites with measured times', () => {
  const sites = {
    one: { arms: { us: [1, 1], other: [1, 4] } },
    two: { arms: { us: [1, 7], other: [1, 2] } },
    onlyOurs: { arms: { us: [1, 30], other: [0, 0.1] } },
    onlyOther: { arms: { us: [0, 0.1], other: [1, 99] } },
    missingTime: { arms: { us: [1, null], other: [1, 0] } },
    absent: { arms: { us: [1, 0] } },
  };
  assert.deepEqual(pairedLatency(sites, 'us', 'other'), { count: 2, ours: 4, other: 3 });
});

test('handles no comparable reads and valid zero times', () => {
  assert.deepEqual(pairedLatency({}, 'us', 'other'), { count: 0, ours: null, other: null });
  assert.deepEqual(pairedLatency({x:{arms:{us:[1,0],other:[1,1]}}}, 'us', 'other'), { count: 1, ours: 0, other: 1 });
});
