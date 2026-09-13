/**
 * Make the web export portable to a static host serving it from a subpath.
 *
 * Two things Expo's output assumes that such a host does not provide:
 *
 * 1. Root-relative URLs. Expo emits `/_expo/...`, `/assets/...` and
 *    `/favicon.ico`, which only resolve when the app is served from a domain
 *    root. Rewritten here to relative.
 * 2. A bundle directory starting with `_`. Some hosts reserve that prefix for
 *    their own paths, so `_expo/` is renamed to `expo/`.
 *
 *   node scripts/relativise.mjs dist-demo
 */
import { readdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const root = process.argv[2];
if (!root) {
  console.error('usage: node scripts/relativise.mjs <export-dir>');
  process.exit(1);
}

const REWRITES = [
  // absolute -> relative, and _expo -> expo, in one pass
  [/"\/?_expo\//g, '"expo/'],
  [/'\/?_expo\//g, "'expo/"],
  [/"\/assets\//g, '"assets/'],
  [/'\/assets\//g, "'assets/"],
  [/"\/favicon\.ico"/g, '"favicon.ico"'],
  [/href="\/favicon\.ico"/g, 'href="favicon.ico"'],
];

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      walk(path);
    } else if (/\.(html|js|json|css)$/.test(entry)) {
      const before = readFileSync(path, 'utf8');
      const after = REWRITES.reduce((text, [find, put]) => text.replace(find, put), before);
      if (after !== before) {
        writeFileSync(path, after);
        console.log(`rewrote ${path}`);
      }
    }
  }
}

walk(root);

// Rename the bundle directory last, so the rewrites above have already fixed
// every reference to it.
const from = join(root, '_expo');
const to = join(root, 'expo');
if (existsSync(from)) {
  renameSync(from, to);
  console.log(`renamed ${from} -> ${to}`);
}

console.log('done');
