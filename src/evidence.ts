export type NormalizedSource = {
  text: string;
  /** Maps displayed UTF-16 selection offsets back to offsets in the decoded source. */
  originalIndexAtDisplayOffset: number[];
};

/**
 * Textareas normalize CR and CRLF to LF. Preserve an offset map so the selection
 * can still be translated back to byte offsets in the exact uploaded bytes.
 */
export function normalizeSourceForSelection(source: string): NormalizedSource {
  const displayed: string[] = [];
  const originalIndexAtDisplayOffset = [0];
  let index = 0;

  while (index < source.length) {
    const character = source[index];
    if (character === '\r') {
      index += source[index + 1] === '\n' ? 2 : 1;
      displayed.push('\n');
    } else {
      displayed.push(character);
      index += 1;
    }
    originalIndexAtDisplayOffset.push(index);
  }

  return { text: displayed.join(''), originalIndexAtDisplayOffset };
}

/** Convert a UTF-16 code-unit index into its exact UTF-8 byte offset. */
export function utf8ByteOffset(source: string, utf16Index: number, bomPrefixBytes = 0): number {
  if (!Number.isInteger(utf16Index) || utf16Index < 0 || utf16Index > source.length) {
    throw new RangeError('Text selection is outside the source.');
  }
  const previous = source.charCodeAt(utf16Index - 1);
  const current = source.charCodeAt(utf16Index);
  const splitsSurrogatePair = previous >= 0xd800 && previous <= 0xdbff && current >= 0xdc00 && current <= 0xdfff;
  if (splitsSurrogatePair) {
    throw new RangeError('Select whole Unicode characters; the selection boundary splits a character.');
  }
  return bomPrefixBytes + new TextEncoder().encode(source.slice(0, utf16Index)).length;
}

/** Read source text without losing the byte identity checked against the server hash. */
export async function decodeVerifiedTextFile(file: File, expectedSha256: string): Promise<{
  source: string;
  normalized: NormalizedSource;
  bomPrefixBytes: number;
}> {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const actualSha256 = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
  if (actualSha256 !== expectedSha256) {
    throw new Error('This local file does not match the stored source SHA-256. Choose the exact uploaded version.');
  }

  let source: string;
  try {
    source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new Error('This source is not valid UTF-8 text and cannot be byte-range annotated.');
  }

  // TextDecoder strips a UTF-8 BOM by default. Account for it when mapping a
  // visible selection back into the original document's byte coordinates.
  const raw = new Uint8Array(bytes);
  const hasUtf8Bom = raw.length >= 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf;
  const bomPrefixBytes = hasUtf8Bom && !source.startsWith('\uFEFF') ? 3 : 0;

  return { source, normalized: normalizeSourceForSelection(source), bomPrefixBytes };
}

export function selectionToByteRange(
  source: string,
  normalized: NormalizedSource,
  displayStart: number,
  displayEnd: number,
  bomPrefixBytes = 0,
): { startByte: number; endByte: number } {
  if (displayStart >= displayEnd) throw new RangeError('Select a non-empty source passage first.');
  const sourceStart = normalized.originalIndexAtDisplayOffset[displayStart];
  const sourceEnd = normalized.originalIndexAtDisplayOffset[displayEnd];
  if (sourceStart === undefined || sourceEnd === undefined) throw new RangeError('Text selection is outside the source.');
  const startByte = utf8ByteOffset(source, sourceStart, bomPrefixBytes);
  const endByte = utf8ByteOffset(source, sourceEnd, bomPrefixBytes);
  if (startByte >= endByte) throw new RangeError('Select a non-empty source passage first.');
  return { startByte, endByte };
}
