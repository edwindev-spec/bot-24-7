'use strict';
// Fusion adapter. Each invocation owns a fresh engine and temporary workspace.
const fs = require('fs');
const path = require('path');
const driver = require('./src/driver');
const { restoreHeaderNewline } = require('./src/detect');

async function main() {
  const c = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const source = restoreHeaderNewline(fs.readFileSync(c.input, 'latin1'));
  const sourcePath = path.join(c.work, 'source.lua');
  fs.writeFileSync(sourcePath, source, 'latin1');
  const tracePath = path.join(c.work, 'recovered.deobf.luau');
  const base = path.join(c.work, 'recovered');
  const job = {
    input: sourcePath, source, sourcePath, tracePath,
    debug: false, obfuscator: c.engine === '15' ? 'luraph_v15' : 'luraph_v14',
    args: {
      noDevirt: c.traceOnly, noHooks: c.traceOnly, noFold: false,
      keepHarness: false, keepPreamble: false, strings: false,
      timeout: c.timeout, budget: c.budget, maxRuns: c.maxRuns,
      devirtRounds: c.rounds, executor: 'Wave', debug: false,
    },
    outdir: c.work,
    path: suffix => base + suffix,
    creditHeader: () => '',
    wrote: () => {},
    write: (file, text, encoding = 'utf8') => {
      fs.writeFileSync(file, text, encoding);
      return file;
    },
  };
  const result = await driver.run(job);
  if (!result || !fs.existsSync(result)) throw new Error('Engine produced no result');
  const lifted = result === job.path('.devirt.luau');
  fs.copyFileSync(result, c.output);
  fs.writeFileSync(c.metadata, JSON.stringify({ kind: lifted ? 'lifted' : 'trace' }));
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
