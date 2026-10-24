import { createHmac } from 'node:crypto';

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const STEP = 30;

function base32Decode(input: string): Buffer {
  let bits = '';
  for (const ch of input.replace(/=+$/, '').toUpperCase()) {
    bits += ALPHABET.indexOf(ch).toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

export function codeAt(secret: string, counter: number): string {
  const msg = Buffer.alloc(8);
  msg.writeBigUInt64BE(BigInt(counter));
  const hash = createHmac('sha1', base32Decode(secret)).update(msg).digest();
  const offset = hash[hash.length - 1]! & 0xf;
  const bin = hash.readUInt32BE(offset) & 0x7fffffff;
  return String(bin % 1_000_000).padStart(6, '0');
}

/** A API recusa codigo repetido ou de passo anterior (anti-replay): cada uso precisa de um passo novo. */
export class Totp {
  private last = -1;
  constructor(private readonly secret: string) {}

  async next(): Promise<string> {
    for (;;) {
      const center = Math.floor(Date.now() / 1000 / STEP);
      const counter = Math.max(this.last + 1, center);
      if (counter <= center + 1) {
        this.last = counter;
        return codeAt(this.secret, counter);
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}
