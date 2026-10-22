// Print the contract address recorded by the newest finalized deployment, so a
// workflow evaluates exactly the contract the repository already evidences.
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { resolveRecordedAddress } from '../api/_genlayer.js';

const network = process.argv[2] || 'studionet';
const dir = process.argv[3] || 'deployments';

// Evidence filenames embed a UTC timestamp, so sorting them orders attempts.
const files = (await readdir(dir).catch(() => []))
  .filter((name) => name.endsWith('.json') && !name.startsWith('evaluation-'))
  .sort();
const records = [];
for (const name of files) {
  try {
    records.push(JSON.parse(await readFile(path.join(dir, name), 'utf8')));
  } catch {
    // A malformed evidence file must not decide the target.
  }
}
const resolved = resolveRecordedAddress(records, network);
if (!resolved) {
  console.error(`No finalized ${network} deployment record with a contract address was found in ${dir}/.`);
  process.exit(1);
}
console.error(`Using ${resolved.address} from deployments evidence${resolved.checked_at ? ` recorded ${resolved.checked_at}` : ''}.`);
console.log(resolved.address);
