// Test de humo: valida que el sitio no esta roto.
// Ejecuta: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

test('README tiene contenido real', () => {
  const p = resolve(root, 'README.md');
  assert.ok(existsSync(p), 'falta README.md');
  assert.ok(readFileSync(p, 'utf8').length >= 500, 'README demasiado corto');
});

test('HTML tiene titulo, idioma y recursos locales existentes', () => {
  const p = resolve(root, 'index.html');
  if (!existsSync(p)) return;
  const html = readFileSync(p, 'utf8');
  assert.match(html, /<title>[^<]{3,}<\/title>/i, 'sin title');
  assert.match(html, /<html[^>]+lang=/i, 'sin lang');
  const refs = [...html.matchAll(/(?:src|href)="([^"#:]+)"/g)].map(m => m[1])
    .filter(u => !u.startsWith('data:') && !u.startsWith('http'));
  for (const r of refs.slice(0, 40)) {
    assert.ok(existsSync(resolve(root, r)), 'recurso local inexistente: ' + r);
  }
});

test('package.json es JSON valido si existe', () => {
  const p = resolve(root, 'package.json');
  if (!existsSync(p)) return;
  const j = JSON.parse(readFileSync(p, 'utf8'));
  assert.ok(j.name, 'package.json sin name');
});
