/**
 * Fold a web export into ONE self-contained .html file.
 *
 * Produces a file that opens by double-clicking — no server, no install, no
 * network. The JS bundle is inlined and every image asset becomes a data URI,
 * so there is nothing left to fetch.
 *
 *   node scripts/single-file.mjs dist-demo careeros-demo.html
 */
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { extname, join, relative } from 'node:path';

const [dir, out] = process.argv.slice(2);
if (!dir || !out) {
  console.error('usage: node scripts/single-file.mjs <export-dir> <output.html>');
  process.exit(1);
}

const MIME = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.svg': 'image/svg+xml',
};

function files(root) {
  const found = [];
  (function walk(d) {
    for (const entry of readdirSync(d)) {
      const p = join(d, entry);
      if (statSync(p).isDirectory()) walk(p);
      else found.push(p);
    }
  })(root);
  return found;
}

// 1. Every image becomes a data URI, keyed by the path the bundle references.
const dataUris = new Map();
for (const path of files(dir)) {
  const mime = MIME[extname(path).toLowerCase()];
  if (!mime) continue;
  const key = relative(dir, path).split(/[\\/]/).join('/');
  dataUris.set(key, `data:${mime};base64,${readFileSync(path).toString('base64')}`);
}

// 2. Inline the bundle, swapping asset paths for their data URIs.
const bundlePath = files(dir).find((p) => p.endsWith('.js'));
let bundle = readFileSync(bundlePath, 'utf8');
let swapped = 0;
for (const [key, uri] of dataUris) {
  const before = bundle;
  bundle = bundle.split(`"${key}"`).join(`"${uri}"`).split(`'${key}'`).join(`'${uri}'`);
  if (bundle !== before) swapped += 1;
}

// A literal "</script" inside the bundle would close the tag early.
bundle = bundle.replace(/<\/script/gi, '<\\/script');

// 3. The shell, minus the external <script src>, with its own asset
//    references (the favicon link) swapped for data URIs as well -- otherwise
//    the one self-contained file still reaches out for one file.
let shell = readFileSync(join(dir, 'index.html'), 'utf8').replace(
  /<script src="[^"]*"[^>]*><\/script>/,
  '',
);
for (const [key, uri] of dataUris) {
  shell = shell.split(`"${key}"`).join(`"${uri}"`);
}

/**
 * Expo Router resolves the active route from `location.pathname`, so the app
 * only finds its index route when served from a host root. Anywhere else -- a
 * preview URL, a docs subdirectory, an artifact's own path -- nothing matches
 * and the router renders `+not-found` instead of the app.
 *
 * Normalising the path before the bundle boots fixes that. It is safe here
 * precisely because this is the single-file build: every asset is already a
 * data URI, so nothing depends on the document's directory any more.
 *
 * `history.replaceState` is refused on `file://` and by opaque origins, hence
 * the try/catch: there the path stays wrong and the app shows its not-found
 * screen, which is why this file wants serving over http (see the README).
 */
const BOOT = `try {
  if (location.pathname !== '/') history.replaceState(null, '', '/');
} catch (error) {
  /* file:// or an opaque origin - serve over http instead */
}`;

writeFileSync(
  out,
  `${shell.trimEnd()}\n<script>\n${BOOT}\n</script>\n<script>\n${bundle}\n</script>\n`,
);
console.log(
  `wrote ${out} (${(statSync(out).size / 1024 / 1024).toFixed(2)} MB), ` +
    `${swapped}/${dataUris.size} assets inlined`,
);
