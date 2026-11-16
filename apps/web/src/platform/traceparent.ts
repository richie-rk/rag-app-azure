// W3C `traceparent` for every API request (10-web-client.md section 5.4). The sampled flag is
// always 01: the browser has no sampler, and the API's sampler decides from the trace ID, so the
// browser can neither force nor suppress sampling.

export interface Traceparent {
  /** The header value, `00-<trace-id>-<parent-id>-01`. */
  readonly header: string;
  /** 32 lower-case hexadecimal characters; kept with the request as its reference ID. */
  readonly traceId: string;
  readonly parentId: string;
}

export type RandomFill = (bytes: Uint8Array<ArrayBuffer>) => void;

const cryptoFill: RandomFill = (bytes) => {
  crypto.getRandomValues(bytes);
};

/** Random bytes as lower-case hexadecimal, drawn once more while they are all zero, because an
 * all-zero ID is invalid. */
function randomHex(length: number, fill: RandomFill): string {
  const bytes = new Uint8Array(length);
  do {
    fill(bytes);
  } while (bytes.every((byte) => byte === 0));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function newTraceparent(fill: RandomFill = cryptoFill): Traceparent {
  const traceId = randomHex(16, fill);
  const parentId = randomHex(8, fill);
  return { header: `00-${traceId}-${parentId}-01`, traceId, parentId };
}
