// Each pair uses only sites both tools read successfully, with measured times.
export function pairedLatency(sites, ours, competitor) {
  const left = [], right = [];
  for (const site of Object.values(sites)) {
    const a = site.arms[ours], b = site.arms[competitor];
    if (a?.[0] !== 1 || b?.[0] !== 1 || !Number.isFinite(a[1]) || !Number.isFinite(b[1]) || a[1] < 0 || b[1] < 0) continue;
    left.push(a[1]); right.push(b[1]);
  }
  const median = (values) => {
    values.sort((a, b) => a - b);
    const n = values.length;
    return n ? (values[(n - 1) >> 1] + values[n >> 1]) / 2 : null;
  };
  return { count: left.length, ours: median(left), other: median(right) };
}
