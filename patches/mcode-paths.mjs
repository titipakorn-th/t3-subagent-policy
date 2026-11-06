// Where mcode lives on this machine. The tests used to hardcode one macOS
// home path, so they could not run on the Linux boxes that T3 also drives.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const MCODE_HOME = path.join(os.homedir(), '.minimax-code');

export function defaultProxy() {
  return path.join(MCODE_HOME, 'bin/mcode-acp-proxy.js');
}

export function defaultAcp() {
  return path.join(MCODE_HOME, 'bin/mcode-acp');
}

// The release the launcher runs comes from ~/.minimax-code/current, so this
// works on any machine and after an mcode upgrade.
export function defaultLauncher() {
  try {
    const release = fs.readFileSync(path.join(MCODE_HOME, 'current'), 'utf8').trim();
    return path.join(MCODE_HOME, 'releases', release, '.mcode-launcher');
  } catch {
    return '';
  }
}
