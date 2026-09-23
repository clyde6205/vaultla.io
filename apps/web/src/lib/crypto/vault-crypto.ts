/**
 * Vaultla.io — client-side cryptography (Module A, browser half).
 *
 * Everything here runs in the user's browser via the Web Crypto API. The server receives
 * only: ciphertext, its SHA-256, one KMS-sealed Shamir share, and opaque wrapped shares.
 *
 *   DEK (random 256-bit) --AES-256-GCM chunked--> ciphertext        -> S3
 *   DEK --Shamir 2-of-3--> share[0] -> server (released after Chronos maturity)
 *                          share[1] -> wrapped under OWNER key      -> opaque blob
 *                          share[2] -> wrapped under RECOVERY key   -> opaque blob
 *
 * Ciphertext format v1 (self-describing for 100-year retention; bump version, never mutate):
 *   header[16] = "VLA1" | version u8 | chunkSize u32be | noncePrefix[4] | reserved[3]
 *   then N records of (chunkSize plaintext + 16-byte GCM tag); the last may be shorter.
 *   nonce_i = noncePrefix || i (u64be);   AAD_i = header || isFinal(u8)
 * Binding the header and a "final" flag into the AAD defeats reordering, truncation,
 * and downgrade. A fresh DEK per item means (key, nonce) pairs never repeat.
 */
import { combine, split } from "./shamir.ts";

export const ENVELOPE_VERSION = 1;
export const CHUNK_SIZE = 4 * 1024 * 1024;
/** Client-side item ceiling: SubtleCrypto has no streaming SHA-256, so each item is hashed in memory. */
export const MAX_ITEM_PLAINTEXT = 128 * 1024 * 1024;
const TAG = 16;
const HEADER_LEN = 16;
const MAGIC = [0x56, 0x4c, 0x41, 0x31]; // "VLA1"
export const PBKDF2_ITERATIONS = 600_000; // OWASP 2023 for PBKDF2-HMAC-SHA256

export class VaultCryptoError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VaultCryptoError";
  }
}

const subtle = (): SubtleCrypto => {
  const s = globalThis.crypto?.subtle;
  if (!s) throw new VaultCryptoError("Web Crypto is unavailable (requires a secure context)");
  return s;
};
const rand = (n: number): Uint8Array => crypto.getRandomValues(new Uint8Array(n));
const hex = (b: ArrayBuffer): string =>
  Array.from(new Uint8Array(b), (x) => x.toString(16).padStart(2, "0")).join("");
export const toB64 = (b: Uint8Array): string => {
  let s = "";
  for (const x of b) s += String.fromCharCode(x);
  return btoa(s);
};
export const fromB64 = (s: string): Uint8Array => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function aesKey(raw: Uint8Array, usage: KeyUsage[]): Promise<CryptoKey> {
  if (raw.length !== 32) throw new VaultCryptoError("key must be 32 bytes");
  return subtle().importKey("raw", raw as BufferSource, "AES-GCM", false, usage); // non-extractable
}

const nonceFor = (prefix: Uint8Array, i: number): Uint8Array => {
  const n = new Uint8Array(12);
  n.set(prefix, 0);
  new DataView(n.buffer).setBigUint64(4, BigInt(i), false);
  return n;
};
const aadFor = (header: Uint8Array, isFinal: boolean): Uint8Array => {
  const a = new Uint8Array(HEADER_LEN + 1);
  a.set(header, 0);
  a[HEADER_LEN] = isFinal ? 1 : 0;
  return a;
};

/** Generate a fresh 256-bit data-encryption key. */
export const generateDek = (): Uint8Array => rand(32);

export interface EncryptedBlob {
  ciphertext: Blob;
  sizeBytes: number;
  sha256Hex: string;
}

export async function encryptBlob(plain: Blob, dek: Uint8Array): Promise<EncryptedBlob> {
  if (plain.size > MAX_ITEM_PLAINTEXT) {
    throw new VaultCryptoError(`item exceeds ${MAX_ITEM_PLAINTEXT} bytes; split into multiple items`);
  }
  const key = await aesKey(dek, ["encrypt"]);
  const header = new Uint8Array(HEADER_LEN);
  header.set(MAGIC, 0);
  header[4] = ENVELOPE_VERSION;
  new DataView(header.buffer).setUint32(5, CHUNK_SIZE, false);
  const prefix = rand(4);
  header.set(prefix, 9);

  const parts: BlobPart[] = [header as BlobPart];
  const chunks = Math.max(1, Math.ceil(plain.size / CHUNK_SIZE));
  for (let i = 0; i < chunks; i++) {
    const slice = plain.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
    const buf = await slice.arrayBuffer();
    const ct = await subtle().encrypt(
      { name: "AES-GCM", iv: nonceFor(prefix, i) as BufferSource,
        additionalData: aadFor(header, i === chunks - 1) as BufferSource, tagLength: 128 },
      key, buf);
    parts.push(ct);
  }
  const ciphertext = new Blob(parts, { type: "application/octet-stream" });
  const sha256Hex = hex(await subtle().digest("SHA-256", await ciphertext.arrayBuffer()));
  return { ciphertext, sizeBytes: ciphertext.size, sha256Hex };
}

export async function decryptBlob(ct: Blob, dek: Uint8Array): Promise<Blob> {
  const all = new Uint8Array(await ct.arrayBuffer());
  if (all.length < HEADER_LEN + TAG) throw new VaultCryptoError("ciphertext too short");
  const header = all.subarray(0, HEADER_LEN);
  if (!MAGIC.every((m, i) => header[i] === m)) throw new VaultCryptoError("not a Vaultla ciphertext");
  if (header[4] !== ENVELOPE_VERSION) throw new VaultCryptoError(`unsupported envelope version ${header[4]}`);
  const chunkSize = new DataView(header.buffer, header.byteOffset).getUint32(5, false);
  if (chunkSize < 1024 || chunkSize > 64 * 1024 * 1024) throw new VaultCryptoError("invalid chunk size");
  const prefix = header.slice(9, 13);
  const key = await aesKey(dek, ["decrypt"]);

  const rec = chunkSize + TAG;
  const body = all.subarray(HEADER_LEN);
  const chunks = Math.max(1, Math.ceil(body.length / rec));
  const out: BlobPart[] = [];
  try {
    for (let i = 0; i < chunks; i++) {
      const piece = body.subarray(i * rec, Math.min((i + 1) * rec, body.length));
      const pt = await subtle().decrypt(
        { name: "AES-GCM", iv: nonceFor(prefix, i) as BufferSource,
          additionalData: aadFor(header, i === chunks - 1) as BufferSource, tagLength: 128 },
        key, piece as BufferSource);
      out.push(pt);
    }
  } catch {
    // Deliberately generic: do not reveal which chunk failed or why.
    throw new VaultCryptoError("decryption failed: wrong key or corrupted/truncated data");
  }
  return new Blob(out);
}

// ---------------------------------------------------------------- key wrapping
export interface KdfParams { kdf: "pbkdf2-sha256"; iterations: number; salt_b64: string }

/** Passphrase -> 256-bit KEK. Upgrade path: Argon2id (WASM) or a passkey PRF output. */
export async function deriveKekFromPassphrase(pass: string, salt?: Uint8Array,
    iterations = PBKDF2_ITERATIONS): Promise<{ kek: Uint8Array; params: KdfParams }> {
  if (pass.length < 12) throw new VaultCryptoError("passphrase must be at least 12 characters");
  const s = salt ?? rand(16);
  const base = await subtle().importKey("raw", new TextEncoder().encode(pass), "PBKDF2", false, ["deriveBits"]);
  const bits = await subtle().deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: s as BufferSource, iterations }, base, 256);
  return { kek: new Uint8Array(bits), params: { kdf: "pbkdf2-sha256", iterations, salt_b64: toB64(s) } };
}

/** Passkey/biometric path: WebAuthn PRF extension output (32 bytes) -> KEK via HKDF. */
export async function deriveKekFromPrf(prfOutput: Uint8Array, info = "vaultla/kek/v1"): Promise<Uint8Array> {
  const base = await subtle().importKey("raw", prfOutput as BufferSource, "HKDF", false, ["deriveBits"]);
  const bits = await subtle().deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32) as BufferSource,
      info: new TextEncoder().encode(info) as BufferSource }, base, 256);
  return new Uint8Array(bits);
}

/** Random recovery code (160 bits, Crockford-style base32 in groups) for the guardian share. */
export function generateRecoveryCode(): string {
  const alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"; // 30 symbols, no 0/O/1/I/L
  let out = "";
  while (out.length < 32) {
    for (const b of rand(64)) {
      if (b < 240 && out.length < 32) out += alphabet[b % 30]; // reject b>=240: no modulo bias
    }
  }
  return out.match(/.{4}/g)!.join("-"); // 32 symbols * log2(30) ~ 157 bits
}

/** wrapped = version(1) | nonce(12) | AES-GCM(share). Opaque to the server. */
export async function wrapShare(share: Uint8Array, kek: Uint8Array, context: string): Promise<Uint8Array> {
  const nonce = rand(12);
  const ct = new Uint8Array(await subtle().encrypt(
    { name: "AES-GCM", iv: nonce as BufferSource, additionalData: new TextEncoder().encode(context) as BufferSource },
    await aesKey(kek, ["encrypt"]), share as BufferSource));
  const out = new Uint8Array(1 + 12 + ct.length);
  out[0] = 1; out.set(nonce, 1); out.set(ct, 13);
  return out;
}

export async function unwrapShare(wrapped: Uint8Array, kek: Uint8Array, context: string): Promise<Uint8Array> {
  if (wrapped[0] !== 1 || wrapped.length < 13 + 16) throw new VaultCryptoError("bad wrapped share");
  try {
    return new Uint8Array(await subtle().decrypt(
      { name: "AES-GCM", iv: wrapped.slice(1, 13) as BufferSource, additionalData: new TextEncoder().encode(context) as BufferSource },
      await aesKey(kek, ["decrypt"]), wrapped.slice(13) as BufferSource));
  } catch {
    throw new VaultCryptoError("could not unwrap key share (wrong passphrase or recovery code)");
  }
}

// ---------------------------------------------------------------- item orchestration
export interface PreparedItem {
  ciphertext: Blob;
  /** JSON body for POST /v1/vaults (one `items[]` entry). */
  body: {
    size_bytes: number;
    sha256_hex: string;
    server_share_b64: string;
    wrapped_shares: { holder: "owner" | "guardian"; holder_user_id: null; blob_b64: string }[];
  };
}

/** Encrypt one item and split its key. The DEK is zeroed before returning. */
export async function prepareItem(plain: Blob, ownerKek: Uint8Array, recoveryKek: Uint8Array,
    context: string): Promise<PreparedItem> {
  const dek = generateDek();
  try {
    const enc = await encryptBlob(plain, dek);
    const [serverShare, ownerShare, recoveryShare] = split(dek, 3, 2);
    const owner = await wrapShare(ownerShare, ownerKek, context);
    const guardian = await wrapShare(recoveryShare, recoveryKek, context);
    return {
      ciphertext: enc.ciphertext,
      body: {
        size_bytes: enc.sizeBytes,
        sha256_hex: enc.sha256Hex,
        server_share_b64: toB64(serverShare),
        wrapped_shares: [
          { holder: "owner", holder_user_id: null, blob_b64: toB64(owner) },
          { holder: "guardian", holder_user_id: null, blob_b64: toB64(guardian) },
        ],
      },
    };
  } finally {
    dek.fill(0);
  }
}

/** Open an item after maturity: server share + ONE holder share -> DEK -> plaintext. */
export async function openItem(ciphertext: Blob, serverShareB64: string, wrappedHolderShare: Uint8Array,
    holderKek: Uint8Array, context: string): Promise<Blob> {
  const held = await unwrapShare(wrappedHolderShare, holderKek, context);
  const dek = combine([fromB64(serverShareB64), held]);
  try {
    return await decryptBlob(ciphertext, dek);
  } finally {
    dek.fill(0);
    held.fill(0);
  }
}
