import { access, copyFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
try {
  await access('.dev.vars');
} catch {
  await copyFile('.dev.vars.example', '.dev.vars');
  console.log('Created local-only mock configuration.');
}
const child = spawn(
  process.execPath,
  [resolve('node_modules/wrangler/bin/wrangler.js'), 'd1', 'migrations', 'apply', 'DB', '--local'],
  { stdio: 'inherit', windowsHide: true },
);
child.on('error', () => {
  console.error('Could not start Wrangler. Run npm ci first.');
  process.exitCode = 1;
});
child.on('exit', (code) => {
  process.exitCode = code ?? 1;
});
