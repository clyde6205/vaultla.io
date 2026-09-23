/**
 * Shamir secret sharing over GF(2^8) (AES polynomial 0x11b), byte-wise.
 * Share layout: [x, y_0 ... y_{n-1}] where x in 1..255 is the evaluation point.
 * Any k shares reconstruct the secret; fewer than k reveal NOTHING (information-theoretic).
 * Runs entirely in the browser; the server never sees a reconstructable set of shares.
 */

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    // multiply by generator 3: x*3 = x ^ (x*2)
    let x2 = x << 1;
    if (x2 & 0x100) x2 ^= 0x11b;
    x ^= x2;
    x &= 0xff;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();

const mul = (a: number, b: number): number => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]);
const div = (a: number, b: number): number => {
  if (b === 0) throw new RangeError("division by zero in GF(256)");
  return a === 0 ? 0 : EXP[LOG[a] + 255 - LOG[b]];
};

export function split(secret: Uint8Array, n: number, k: number): Uint8Array[] {
  if (!Number.isInteger(n) || !Number.isInteger(k) || k < 2 || n < k || n > 255) {
    throw new RangeError("require 2 <= k <= n <= 255");
  }
  if (secret.length === 0) throw new RangeError("empty secret");
  const shares = Array.from({ length: n }, (_, i) => {
    const s = new Uint8Array(1 + secret.length);
    s[0] = i + 1;
    return s;
  });
  const coeffs = new Uint8Array(k - 1);
  for (let b = 0; b < secret.length; b++) {
    crypto.getRandomValues(coeffs);
    // Ensure the top coefficient is non-zero so the polynomial has full degree k-1.
    while (coeffs[k - 2] === 0) crypto.getRandomValues(coeffs.subarray(k - 2));
    for (let i = 0; i < n; i++) {
      const x = i + 1;
      let y = 0;
      for (let c = k - 2; c >= 0; c--) y = mul(y, x) ^ coeffs[c]; // Horner
      shares[i][1 + b] = mul(y, x) ^ secret[b];
    }
  }
  coeffs.fill(0);
  return shares;
}

export function combine(shares: Uint8Array[]): Uint8Array {
  if (shares.length < 2) throw new RangeError("need at least 2 shares");
  const len = shares[0].length;
  const xs = new Set<number>();
  for (const s of shares) {
    if (s.length !== len || len < 2) throw new RangeError("shares have inconsistent length");
    if (s[0] === 0 || xs.has(s[0])) throw new RangeError("invalid or duplicate share index");
    xs.add(s[0]);
  }
  const out = new Uint8Array(len - 1);
  for (let b = 0; b < out.length; b++) {
    let acc = 0;
    for (let i = 0; i < shares.length; i++) {
      let num = 1;
      let den = 1;
      for (let j = 0; j < shares.length; j++) {
        if (i === j) continue;
        num = mul(num, shares[j][0]);
        den = mul(den, shares[i][0] ^ shares[j][0]);
      }
      acc ^= mul(shares[i][1 + b], div(num, den));
    }
    out[b] = acc;
  }
  return out;
}
