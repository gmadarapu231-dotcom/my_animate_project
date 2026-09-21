/* Reading a PDF's text, with positions, in the browser.
 *
 * The demo has no server, so if an upload is to mean anything the file has to
 * be read here. This is a small PDF text extractor: it finds the content
 * streams, inflates them with the browser's own DecompressionStream, and walks
 * the text operators keeping track of where each run was drawn.
 *
 * It is deliberately not a full PDF implementation. It handles what a payroll
 * W-2 actually is -- drawn text in simple fonts -- and returns nothing for a
 * scan, an image, or a font with a custom CID encoding. Returning nothing is
 * the correct answer in those cases, and the caller says so rather than
 * guessing.
 *
 * The server does this properly in Python. This exists so the demo tells the
 * truth about your own file instead of showing you a sample.
 */
'use strict';

(function (global) {
  const LATIN = new TextDecoder('latin1');

  /* ------------------------------------------------------------------ bytes */
  function bytesToLatin(bytes) {
    return LATIN.decode(bytes);
  }

  function indexOfSeq(haystack, needle, from) {
    outer: for (let i = from; i <= haystack.length - needle.length; i += 1) {
      for (let j = 0; j < needle.length; j += 1) {
        if (haystack[i + j] !== needle[j]) continue outer;
      }
      return i;
    }
    return -1;
  }

  const ASCII = (text) => Array.from(text, (c) => c.charCodeAt(0));

  /** Inflate a zlib stream using the browser. PDF FlateDecode is zlib-wrapped. */
  async function inflate(bytes) {
    if (typeof DecompressionStream === 'undefined') return null;
    for (const format of ['deflate', 'deflate-raw']) {
      try {
        const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream(format));
        return new Uint8Array(await new Response(stream).arrayBuffer());
      } catch {
        // Try the other framing before giving up.
      }
    }
    return null;
  }

  /** ASCII85, which PDF writers layer on top of Flate to keep streams printable. */
  function ascii85(bytes) {
    const out = [];
    let tuple = 0;
    let count = 0;
    for (let i = 0; i < bytes.length; i += 1) {
      const code = bytes[i];
      if (code === 0x7e) break;                       // "~>" terminator
      if (code <= 0x20 || code === 0x0a || code === 0x0d) continue;   // whitespace
      if (code === 0x7a && count === 0) { out.push(0, 0, 0, 0); continue; }   // "z"
      if (code < 0x21 || code > 0x75) continue;       // outside the alphabet
      tuple = tuple * 85 + (code - 0x21);
      count += 1;
      if (count === 5) {
        out.push((tuple >>> 24) & 0xff, (tuple >>> 16) & 0xff, (tuple >>> 8) & 0xff, tuple & 0xff);
        tuple = 0;
        count = 0;
      }
    }
    if (count > 1) {
      // A partial group encodes count-1 bytes; pad the rest with the maximum digit.
      for (let i = count; i < 5; i += 1) tuple = tuple * 85 + 84;
      const full = [(tuple >>> 24) & 0xff, (tuple >>> 16) & 0xff, (tuple >>> 8) & 0xff, tuple & 0xff];
      out.push(...full.slice(0, count - 1));
    }
    return new Uint8Array(out);
  }

  /** ASCIIHex, the other printable wrapper. */
  function asciiHex(bytes) {
    const text = bytesToLatin(bytes);
    const clean = text.slice(0, text.indexOf('>') < 0 ? text.length : text.indexOf('>'))
      .replace(/[^0-9a-fA-F]/g, '');
    const out = new Uint8Array(Math.floor(clean.length / 2));
    for (let i = 0; i < out.length; i += 1) {
      out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
    }
    return out;
  }

  /**
   * Apply a stream's filters, in order.
   *
   * `/Filter` may be one name or an array of them, and writers routinely chain
   * them -- ReportLab emits `/Filter [ /ASCII85Decode /FlateDecode ]`. Handling
   * only the single-name form means silently skipping the stream, which is how
   * a perfectly readable PDF came back as "no text found".
   */
  async function applyFilters(bytes, dictionary) {
    const declared = dictionary.match(/\/Filter\s*(\[[^\]]*\]|\/[A-Za-z0-9]+)/);
    if (!declared) return bytes;

    const names = declared[1].match(/\/([A-Za-z0-9]+)/g) || [];
    let data = bytes;
    for (const raw of names) {
      const name = raw.slice(1);
      if (name === 'FlateDecode' || name === 'Fl') {
        data = await inflate(data);
      } else if (name === 'ASCII85Decode' || name === 'A85') {
        data = ascii85(data);
      } else if (name === 'ASCIIHexDecode' || name === 'AHx') {
        data = asciiHex(data);
      } else {
        return null;    // DCT, CCITT, LZW, JPX: an image or an encoding we do not do
      }
      if (!data || !data.length) return null;
    }
    return data;
  }

  /* ---------------------------------------------------------------- objects */
  /**
   * Every stream in the file, inflated where we can.
   *
   * Walking the objects directly rather than the cross-reference table: a
   * linearised or incrementally-updated PDF has several xref sections, and for
   * pulling text out it does not matter which object is current -- the content
   * is in all of them.
   */
  async function streamsOf(bytes) {
    const out = [];
    const STREAM = ASCII('stream');
    const ENDSTREAM = ASCII('endstream');
    let cursor = 0;

    while (cursor < bytes.length) {
      const start = indexOfSeq(bytes, STREAM, cursor);
      if (start < 0) break;
      const end = indexOfSeq(bytes, ENDSTREAM, start);
      if (end < 0) break;

      // The dictionary sits just before the `stream` keyword.
      const headFrom = Math.max(0, start - 800);
      const head = bytesToLatin(bytes.subarray(headFrom, start));

      // Skip the EOL that must follow the keyword.
      let from = start + STREAM.length;
      if (bytes[from] === 0x0d) from += 1;
      if (bytes[from] === 0x0a) from += 1;

      let body = bytes.subarray(from, end);
      try {
        body = await applyFilters(body, head);
      } catch {
        body = null;
      }
      if (body && body.length) out.push(body);
      cursor = end + ENDSTREAM.length;
    }
    return out;
  }

  /* --------------------------------------------------------------- operands */
  function decodeLiteral(raw) {
    let out = '';
    for (let i = 0; i < raw.length; i += 1) {
      const ch = raw[i];
      if (ch !== '\\') { out += ch; continue; }
      const next = raw[i + 1];
      i += 1;
      if (next === 'n') out += '\n';
      else if (next === 'r') out += '\r';
      else if (next === 't') out += '\t';
      else if (next === 'b') out += '\b';
      else if (next === 'f') out += '\f';
      else if (next === '\n') { /* line continuation */ }
      else if (next >= '0' && next <= '7') {
        let octal = next;
        while (octal.length < 3 && raw[i + 1] >= '0' && raw[i + 1] <= '7') {
          octal += raw[i + 1];
          i += 1;
        }
        out += String.fromCharCode(parseInt(octal, 8));
      } else out += next;
    }
    return out;
  }

  function decodeHex(raw) {
    const clean = raw.replace(/[^0-9a-fA-F]/g, '');
    let out = '';
    for (let i = 0; i < clean.length; i += 2) {
      out += String.fromCharCode(parseInt(clean.slice(i, i + 2).padEnd(2, '0'), 16));
    }
    return out;
  }

  /** Split a content stream into operands and operators. */
  function tokenize(text) {
    const tokens = [];
    let i = 0;
    while (i < text.length) {
      const ch = text[i];

      if (ch === '%') {                                   // comment to line end
        while (i < text.length && text[i] !== '\n') i += 1;
        continue;
      }
      if (/\s/.test(ch)) { i += 1; continue; }

      if (ch === '(') {                                   // literal string
        let depth = 1;
        let j = i + 1;
        let raw = '';
        while (j < text.length && depth > 0) {
          if (text[j] === '\\') { raw += text[j] + (text[j + 1] || ''); j += 2; continue; }
          if (text[j] === '(') depth += 1;
          else if (text[j] === ')') { depth -= 1; if (!depth) break; }
          raw += text[j];
          j += 1;
        }
        tokens.push({ kind: 'string', value: decodeLiteral(raw) });
        i = j + 1;
        continue;
      }

      if (ch === '<' && text[i + 1] !== '<') {             // hex string
        const close = text.indexOf('>', i);
        tokens.push({ kind: 'string', value: decodeHex(text.slice(i + 1, close)) });
        i = close + 1;
        continue;
      }

      if (ch === '<' || ch === '>') {                      // dictionary markers
        tokens.push({ kind: 'op', value: text.slice(i, i + 2) });
        i += 2;
        continue;
      }

      if (ch === '[' || ch === ']') {
        tokens.push({ kind: ch === '[' ? 'arrayStart' : 'arrayEnd' });
        i += 1;
        continue;
      }

      if (ch === '/') {                                    // name
        let j = i + 1;
        while (j < text.length && !/[\s/[\]()<>]/.test(text[j])) j += 1;
        tokens.push({ kind: 'name', value: text.slice(i + 1, j) });
        i = j;
        continue;
      }

      let j = i;
      while (j < text.length && !/[\s/[\]()<>]/.test(text[j])) j += 1;
      const word = text.slice(i, j);
      i = j === i ? i + 1 : j;
      if (!word) continue;
      if (/^[-+.\d]+$/.test(word) && !Number.isNaN(parseFloat(word))) {
        tokens.push({ kind: 'number', value: parseFloat(word) });
      } else {
        tokens.push({ kind: 'op', value: word });
      }
    }
    return tokens;
  }

  /* ------------------------------------------------------------------ text */
  const multiply = (a, b) => [
    a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
    a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
    a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5],
  ];

  /**
   * Walk the text operators, emitting each run with where it was drawn.
   *
   * The text matrix is what carries position: `Tm` sets it outright, `Td` and
   * `TD` translate the line matrix, and `T*` steps down by the leading. Without
   * tracking these the runs come out in stream order, which on a two-column
   * form is not reading order and not the grid either.
   */
  function runsFrom(content) {
    const tokens = tokenize(content);
    const runs = [];
    let tm = [1, 0, 0, 1, 0, 0];
    let tlm = tm.slice();
    let leading = 0;
    let stack = [];
    let array = null;

    const emit = (text) => {
      const value = String(text || '').replace(/\u0000/g, '').trim();
      if (value) runs.push({ text: value, x: tm[4], y: tm[5] });
    };
    const setLine = (matrix) => { tlm = matrix; tm = matrix.slice(); };

    for (const token of tokens) {
      if (token.kind === 'arrayStart') { array = []; continue; }
      if (token.kind === 'arrayEnd') { stack.push({ kind: 'array', value: array || [] }); array = null; continue; }
      if (token.kind !== 'op') {
        if (array) array.push(token);
        else stack.push(token);
        continue;
      }

      const numbers = stack.filter((t) => t.kind === 'number').map((t) => t.value);
      const strings = stack.filter((t) => t.kind === 'string').map((t) => t.value);
      const arrays = stack.filter((t) => t.kind === 'array').map((t) => t.value);

      switch (token.value) {
        case 'BT': tm = [1, 0, 0, 1, 0, 0]; tlm = tm.slice(); break;
        case 'Tm':
          if (numbers.length >= 6) setLine(numbers.slice(-6));
          break;
        case 'Td':
          if (numbers.length >= 2) {
            const [tx, ty] = numbers.slice(-2);
            setLine(multiply([1, 0, 0, 1, tx, ty], tlm));
          }
          break;
        case 'TD':
          if (numbers.length >= 2) {
            const [tx, ty] = numbers.slice(-2);
            leading = -ty;
            setLine(multiply([1, 0, 0, 1, tx, ty], tlm));
          }
          break;
        case 'TL': if (numbers.length) leading = numbers[numbers.length - 1]; break;
        case 'T*': setLine(multiply([1, 0, 0, 1, 0, -leading], tlm)); break;
        case 'Tj': case 'Tz': if (strings.length) emit(strings[strings.length - 1]); break;
        case "'":
          setLine(multiply([1, 0, 0, 1, 0, -leading], tlm));
          if (strings.length) emit(strings[strings.length - 1]);
          break;
        case '"':
          setLine(multiply([1, 0, 0, 1, 0, -leading], tlm));
          if (strings.length) emit(strings[strings.length - 1]);
          break;
        case 'TJ':
          if (arrays.length) {
            // Numbers inside a TJ array are kerning offsets. A large negative
            // one is a deliberate gap, which is how forms pad a label -- so it
            // becomes a space rather than being dropped.
            const parts = arrays[arrays.length - 1].map((entry) => {
              if (entry.kind === 'string') return entry.value;
              return entry.kind === 'number' && entry.value < -120 ? ' ' : '';
            });
            emit(parts.join(''));
          }
          break;
        default: break;
      }
      stack = [];
    }
    return runs;
  }

  /* ----------------------------------------------------------------- public */
  /**
   * Every text run in the PDF, with its position.
   *
   * Returns `{ runs, ok, reason }`. `ok` is false when there is nothing to
   * read, which for a W-2 means it is a scan and needs typing in by hand.
   */
  async function readPdf(arrayBuffer) {
    const bytes = new Uint8Array(arrayBuffer);
    if (bytesToLatin(bytes.subarray(0, 5)) !== '%PDF-') {
      return { runs: [], ok: false, reason: 'That file is not a PDF.' };
    }
    if (/\/Encrypt[\s/]/.test(bytesToLatin(bytes.subarray(0, Math.min(bytes.length, 4000))))) {
      return { runs: [], ok: false, reason: 'That PDF is password-protected.' };
    }

    let streams;
    try {
      streams = await streamsOf(bytes);
    } catch (error) {
      return { runs: [], ok: false, reason: 'That PDF could not be opened.' };
    }

    const runs = [];
    for (const stream of streams) {
      const text = bytesToLatin(stream);
      // Only content streams carry text operators; skip fonts, images, metadata.
      if (!/\bBT\b/.test(text)) continue;
      try {
        runs.push(...runsFrom(text));
      } catch {
        // One unreadable stream should not lose the rest of the page.
      }
    }

    if (!runs.length) {
      return {
        runs: [],
        ok: false,
        reason: 'This PDF holds no readable text, so it is a scan or a photo saved '
          + 'as a PDF. Reading it needs OCR. Enter the boxes by hand.',
      };
    }
    return { runs, ok: true, reason: '' };
  }

  global.TaxVaultPdf = { readPdf, runsFrom, tokenize };
})(window);
