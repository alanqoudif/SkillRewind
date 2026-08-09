#!/usr/bin/env node
// Fails the build if built HTML contains prohibited overreach phrases.
// Simple substring scan across dist/**/*.html — intentionally small.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const DIST = new URL('../dist', import.meta.url).pathname;

const PROHIBITED = [
  'the first ever',
  'first-ever',
  'used by openai',
  'used by anthropic',
  'trusted by anthropic',
  'trusted by openai',
  'production ready',
  'production-ready',
];

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const s = statSync(full);
    if (s.isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.html')) out.push(full);
  }
  return out;
}

let files;
try {
  files = walk(DIST);
} catch {
  console.error(`check-claims: could not read ${DIST} — did you run "npm run build" first?`);
  process.exit(1);
}

let failures = 0;

for (const file of files) {
  const html = readFileSync(file, 'utf8').toLowerCase();
  for (const phrase of PROHIBITED) {
    if (html.includes(phrase)) {
      console.error(`check-claims: FOUND prohibited phrase "${phrase}" in ${file}`);
      failures += 1;
    }
  }
}

if (failures > 0) {
  console.error(`check-claims: ${failures} violation(s) found across ${files.length} file(s).`);
  process.exit(1);
}

console.log(`check-claims: OK — scanned ${files.length} file(s), no prohibited phrases found.`);
