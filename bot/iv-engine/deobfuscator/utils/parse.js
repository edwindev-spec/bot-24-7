'use strict';
// The upstream numeric-expression helper used eval; disable it for untrusted input.
function eL() { return null; }
function* eS(s) {
  let i = 0, n = false, q = '', e = false, st = 0;
  while (i < s.length) {
    const c = s[i];
    if (n) {
      if (e) { e = false; }
      else if (c === '\\') { e = true; }
      else if (c === q) { yield s.slice(st + 1, i); n = false; }
      i++; continue;
    }
    if (c === '"' || c === "'") { n = true; q = c; st = i; }
    i++;
  }
}
function fF(s) {
  const f = [];
  for (const x of eS(s)) if (x.length >= 80 && /^[A-Za-z0-9+/=]+$/.test(x)) f.push(x);
  return f;
}
function fK(s) {
  const c = [];
  for (const m of s.matchAll(/,(\d{5,12})\)[^,]{1,400},(\d{5,12})\)~=(\d{5,12})/g))
    c.push({ payloadKey: parseInt(m[1]) >>> 0, payloadHashSeed: parseInt(m[2]) >>> 0, payloadHash: parseInt(m[3]) >>> 0 });
  if (c.length > 0) return c[0];
  for (const m of s.matchAll(/\((\d{5,12})\)[^~]{1,400}~=(\d{5,12})/g)) {
    const b = s.slice(Math.max(0, m.index - 300), m.index);
    const bm = [...b.matchAll(/,(\d{5,12})\)/g)];
    if (bm.length > 0) c.push({ payloadKey: parseInt(bm[bm.length - 1][1]) >>> 0, payloadHashSeed: parseInt(m[1]) >>> 0, payloadHash: parseInt(m[2]) >>> 0 });
  }
  return c[0] || null;
}
function pO(s) {
  if (!s.includes('ironveil')) throw new Error('');
  const f = fF(s);
  if (f.length === 0) throw new Error('');
  const k = fK(s);
  if (!k) throw new Error('');
  return { frags: f, ...k };
}
function eSa(s) {
  const c = new Set();
  const a = (r, v) => { if (v > 0 && v <= 65535 && !c.has(v)) { c.add(v); r.push(v); } };
  const d1 = [], d2 = [], df = [];
  for (const m of s.matchAll(/\[1\],\s*\w{1,3}\[1\],\s*\w{1,3},\s*(\d{1,5}),\s*\w{1,3}\[3\],\s*\w{1,3}\[5\]/g)) a(d1, parseInt(m[1]));
  for (const m of s.matchAll(/\[2\],\s*\w{1,3}\[2\],\s*\w{1,3},\s*(\d{1,5}),\s*\w{1,3}\[4\],\s*\w{1,3}\[6\]/g)) a(d2, parseInt(m[1]));
  for (const m of s.matchAll(/\[1\],\s*\w{1,3},\s*\w{1,3},\s*(\d{1,5})\)/g)) a(df, parseInt(m[1]));
  return { dec: d1, decB: d2, def: df };
}
module.exports = { parseObfuscated: pO, evalLuaNum: eL, extractSalts: eSa };
