#!/usr/bin/env node
/**
 * grapply — MuPDF WASM setup helper
 *
 * Run this once after cloning to download the WASM files:
 *   node setup.js
 *
 * Files placed in the same directory as this script:
 *   mupdf.js          — ESM JS bindings
 *   mupdf-wasm.js     — Emscripten WASM loader
 *   mupdf-wasm.wasm   — the actual WASM binary (~11 MB)
 */

const { execSync } = require('child_process');
const { existsSync, copyFileSync } = require('fs');
const path = require('path');

const DEST = __dirname;
const PKG  = 'mupdf@1.3.0';

const targets = ['mupdf.js', 'mupdf-wasm.js', 'mupdf-wasm.wasm'];
const already = targets.every(f => existsSync(path.join(DEST, f)));

if (already) {
  console.log('✓ MuPDF WASM files already present.');
  process.exit(0);
}

console.log(`Downloading ${PKG} …`);
const tmp = require('os').tmpdir() + '/mupdf_setup_' + Date.now();

try {
  execSync(`npm install ${PKG} --prefix ${tmp} --silent`, { stdio: 'inherit' });
  const src = path.join(tmp, 'node_modules', 'mupdf', 'dist');

  for (const f of targets) {
    copyFileSync(path.join(src, f), path.join(DEST, f));
    console.log(`  ✓ ${f}`);
  }
  console.log('\nMuPDF WASM ready.');
} catch (e) {
  console.error('Setup failed:', e.message);
  process.exit(1);
}
