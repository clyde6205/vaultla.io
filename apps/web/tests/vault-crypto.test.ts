// Run: node --test tests/   (Node >= 22.18 strips TypeScript types natively)
import assert from "node:assert/strict";
import { test } from "node:test";
import { combine, split } from "../src/lib/crypto/shamir.ts";
import {
  CHUNK_SIZE, VaultCryptoError, decryptBlob, deriveKekFromPassphrase, encryptBlob, generateDek,
  generateRecoveryCode, openItem, prepareItem, fromB64, unwrapShare, wrapShare,
} from "../src/lib/crypto/vault-crypto.ts";

const bytes = (n: number): Uint8Array => {
  const b = new Uint8Array(n);
  for (let i = 0; i < n; i += 65536) crypto.getRandomValues(b.subarray(i, Math.min(n, i + 65536)));
  return b;
};
const same = async (a: Blob, b: Uint8Array) =>
  Buffer.compare(Buffer.from(await a.arrayBuffer()), Buffer.from(b)) === 0;

test("shamir: any 2 of 3 reconstruct; 1 does not", () => {
  const secret = generateDek();
  const shares = split(secret, 3, 2);
  for (const [i, j] of [[0, 1], [0, 2], [1, 2], [2, 0]]) assert.deepEqual(combine([shares[i], shares[j]]), secret);
  const bad = shares[1].slice(); bad[5] ^= 1;                     // corrupt one share
  assert.notDeepEqual(combine([shares[0], bad]), secret);
});

test("shamir: 3-of-5 and rejects bad input", () => {
  const secret = generateDek();
  const s = split(secret, 5, 3);
  assert.deepEqual(combine([s[4], s[1], s[2]]), secret);
  assert.throws(() => combine([s[0], s[0]]));
  assert.throws(() => split(secret, 1, 2));
});

test("aes-gcm chunked roundtrip: empty, small, multi-chunk", async () => {
  const dek = generateDek();
  for (const n of [0, 1, 1000, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE + 123]) {
    const data = bytes(n);
    const enc = await encryptBlob(new Blob([data]), dek);
    assert.ok(await same(await decryptBlob(enc.ciphertext, dek), data), `n=${n}`);
  }
});

test("tamper, truncation, reorder and wrong key are all rejected", async () => {
  const dek = generateDek();
  const data = bytes(2 * CHUNK_SIZE + 10);
  const enc = await encryptBlob(new Blob([data]), dek);
  const ct = new Uint8Array(await enc.ciphertext.arrayBuffer());

  const flipped = ct.slice(); flipped[100] ^= 1;
  await assert.rejects(decryptBlob(new Blob([flipped]), dek), VaultCryptoError);

  const rec = CHUNK_SIZE + 16;
  const truncated = ct.slice(0, 16 + rec);                // drop the final chunks
  await assert.rejects(decryptBlob(new Blob([truncated]), dek), VaultCryptoError);

  const swapped = ct.slice();                              // swap chunk 0 and 1
  swapped.set(ct.slice(16 + rec, 16 + 2 * rec), 16); swapped.set(ct.slice(16, 16 + rec), 16 + rec);
  await assert.rejects(decryptBlob(new Blob([swapped]), dek), VaultCryptoError);

  await assert.rejects(decryptBlob(enc.ciphertext, generateDek()), VaultCryptoError);
});

test("nonces never repeat across encryptions of the same data", async () => {
  const dek = generateDek();
  const a = new Uint8Array(await (await encryptBlob(new Blob(["same"]), dek)).ciphertext.arrayBuffer());
  const b = new Uint8Array(await (await encryptBlob(new Blob(["same"]), dek)).ciphertext.arrayBuffer());
  assert.notDeepEqual(a, b);
});

test("wrap/unwrap binds context and key", async () => {
  const kek = generateDek();
  const w = await wrapShare(new Uint8Array(33).fill(7), kek, "vault:1");
  assert.deepEqual(await unwrapShare(w, kek, "vault:1"), new Uint8Array(33).fill(7));
  await assert.rejects(unwrapShare(w, kek, "vault:2"), VaultCryptoError);
  await assert.rejects(unwrapShare(w, generateDek(), "vault:1"), VaultCryptoError);
});

test("end to end: server share + owner share OR recovery share opens the item", async () => {
  const { kek: ownerKek } = await deriveKekFromPassphrase("correct horse battery staple", undefined, 1000);
  const { kek: recoveryKek } = await deriveKekFromPassphrase(generateRecoveryCode(), undefined, 1000);
  const data = bytes(300_000);
  const prepared = await prepareItem(new Blob([data]), ownerKek, recoveryKek, "vault:1");
  assert.equal(fromB64(prepared.body.server_share_b64).length, 33);

  const [owner, guardian] = prepared.body.wrapped_shares.map((w) => fromB64(w.blob_b64));
  assert.ok(await same(await openItem(prepared.ciphertext, prepared.body.server_share_b64, owner, ownerKek, "vault:1"), data));
  assert.ok(await same(await openItem(prepared.ciphertext, prepared.body.server_share_b64, guardian, recoveryKek, "vault:1"), data));
  // A holder share alone (no server share) cannot open it: wrong key material fails authentication.
  await assert.rejects(openItem(prepared.ciphertext, prepared.body.server_share_b64, owner, recoveryKek, "vault:1"));
});

test("recovery codes look right and differ", () => {
  const a = generateRecoveryCode(), b = generateRecoveryCode();
  assert.match(a, /^([A-Z2-9]{4}-){7}[A-Z2-9]{4}$/);
  assert.notEqual(a, b);
});
