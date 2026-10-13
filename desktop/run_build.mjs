import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const dir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(dir, '..');
const backendDir = join(rootDir, 'backend');
const sidecarDir = join(rootDir, 'frontend', 'backend-sidecar');

const PYTHON_VERSION = '3.12.12';
const NODE_VERSION = '22.12.0';
const RELEASE_TAG = '20260211';
const RIPGREP_VERSION = '14.1.1';
const CLAUDE_AGENT_ACP_VERSION = '0.87.0';
const CLAUDE_CODE_VERSION = '2.1.293';
const CODEX_ACP_VERSION = '2.1.1';
const BROWSER_USE_VERSION = '0.13.8';
const GET_PIP_URL = 'https://bootstrap.pypa.io/get-pip.py';

const platform = process.platform;
const arch = process.arch;
const forceClean = process.argv.includes('--clean');
const isWindows = platform === 'win32';

const supported =
  (platform === 'darwin' && ['arm64', 'x64'].includes(arch)) ||
  (platform === 'win32' && arch === 'x64');
if (!supported) {
  throw new Error(
    `Desktop build supports macOS arm64/x64 and Windows x64 only (received: ${arch}/${platform})`,
  );
}

const ARCH_TRIPLE = isWindows
  ? 'x86_64-pc-windows-msvc'
  : arch === 'arm64'
    ? 'aarch64-apple-darwin'
    : 'x86_64-apple-darwin';

// python-build-standalone and the Node zip lay their trees out differently per OS:
// Windows puts the executables at the root, Unix under bin/.
const pythonBin = isWindows
  ? join(sidecarDir, 'python', 'python.exe')
  : join(sidecarDir, 'python', 'bin', 'python3');
const browserUseDir = join(sidecarDir, 'browser-use');
const browserUsePython = isWindows
  ? join(browserUseDir, 'Scripts', 'python.exe')
  : join(browserUseDir, 'bin', 'python');
const nodeDir = join(sidecarDir, 'node');
const nodeBin = isWindows ? join(nodeDir, 'node.exe') : join(nodeDir, 'bin', 'node');
const npmBin = isWindows ? join(nodeDir, 'npm.cmd') : join(nodeDir, 'bin', 'npm');
const npmCli = isWindows
  ? join(nodeDir, 'node_modules', 'npm', 'bin', 'npm-cli.js')
  : join(nodeDir, 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js');
const sidecarBinDir = join(sidecarDir, 'bin');
const rgBin = join(sidecarBinDir, isWindows ? 'rg.exe' : 'rg');
const launcherExt = isWindows ? '.cmd' : '';
const claudeAcpBin = join(sidecarBinDir, `claude-agent-acp${launcherExt}`);
const codexAcpBin = join(sidecarBinDir, `codex-acp${launcherExt}`);

const PYTHON_STAMP = `${PYTHON_VERSION}+${RELEASE_TAG}-${arch}-${platform}`;
const NODE_STAMP = `${NODE_VERSION}-${arch}-${platform}`;
const RG_STAMP = `${RIPGREP_VERSION}-${arch}-${platform}`;
const ACP_STAMP = `${CLAUDE_AGENT_ACP_VERSION}-${CLAUDE_CODE_VERSION}-${CODEX_ACP_VERSION}-${NODE_STAMP}`;
const BROWSER_USE_STAMP = `${BROWSER_USE_VERSION}-${PYTHON_STAMP}`;

function stampPath(name) {
  return join(sidecarDir, `.${name}-stamp`);
}

function upToDate(name, value, ...required) {
  const stamp = stampPath(name);
  if (!existsSync(stamp)) return false;
  if (required.some((f) => !existsSync(f))) return false;
  return readFileSync(stamp, 'utf-8').trim() === value;
}

function writeStamp(name, value) {
  writeFileSync(stampPath(name), `${value}\n`);
}

function download(url, outputPath) {
  execFileSync('curl', ['-fsSL', '-o', outputPath, url], { stdio: 'inherit' });
}

// Windows ships bsdtar in System32, which reads both .tar.gz and .zip. Git Bash
// puts GNU tar (no zip support) first on PATH, so address the system one directly.
function tarBin() {
  if (!isWindows) return 'tar';
  return join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe');
}

function extractArchive(archivePath, destDir) {
  mkdirSync(destDir, { recursive: true });
  const flags = archivePath.endsWith('.zip') ? '-xf' : '-xzf';
  execFileSync(tarBin(), [flags, archivePath, '-C', destDir], { stdio: 'inherit' });
}

function npm(args, options = {}) {
  // Run npm through the bundled node rather than its shell shim: the shim is a
  // symlink on macOS (see replaceNodeShims) and a .cmd on Windows.
  execFileSync(nodeBin, [npmCli, ...args], { stdio: 'inherit', ...options });
}

function depsStampValue() {
  const requirements = readFileSync(join(dir, 'requirements.txt'), 'utf-8');
  return JSON.stringify({ requirements, python: PYTHON_STAMP });
}

function fetchPython() {
  const archive = join(sidecarDir, 'python.tar.gz');
  const name = `cpython-${PYTHON_VERSION}+${RELEASE_TAG}-${ARCH_TRIPLE}-install_only_stripped.tar.gz`;
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE_TAG}/${name}`;
  console.log(`Downloading Python ${PYTHON_VERSION}...`);
  rmSync(join(sidecarDir, 'python'), { recursive: true, force: true });
  try {
    download(url, archive);
    extractArchive(archive, sidecarDir);
  } finally {
    rmSync(archive, { force: true });
  }
  if (!existsSync(pythonBin)) throw new Error(`Python binary not found at ${pythonBin}`);
  writeStamp('python', PYTHON_STAMP);
}

function fetchNode() {
  const archiveName = isWindows
    ? `node-v${NODE_VERSION}-win-${arch}.zip`
    : `node-v${NODE_VERSION}-darwin-${arch}.tar.gz`;
  const archive = join(sidecarDir, isWindows ? 'node.zip' : 'node.tar.gz');
  const extractedDir = join(sidecarDir, archiveName.replace(/\.(zip|tar\.gz)$/, ''));
  const url = `https://nodejs.org/dist/v${NODE_VERSION}/${archiveName}`;
  const extractedNode = isWindows
    ? join(extractedDir, 'node.exe')
    : join(extractedDir, 'bin', 'node');
  console.log(`Downloading Node ${NODE_VERSION}...`);
  rmSync(extractedDir, { recursive: true, force: true });
  rmSync(nodeDir, { recursive: true, force: true });
  try {
    download(url, archive);
    extractArchive(archive, sidecarDir);
    if (!existsSync(extractedNode)) {
      throw new Error(`Node binary not found at ${extractedNode}`);
    }
    renameSync(extractedDir, nodeDir);
  } finally {
    rmSync(archive, { force: true });
    rmSync(extractedDir, { recursive: true, force: true });
  }
  writeStamp('node', NODE_STAMP);
}

// Tauri's resource bundler dereferences symlinks when copying the sidecar
// into the .app, turning `bin/npx` (a symlink to ../lib/node_modules/npm/bin/npx-cli.js)
// into a verbatim copy of that script. Its `require('../lib/cli.js')` then
// resolves relative to bin/ instead of lib/node_modules/npm/bin/ and crashes.
// Replace each symlink with a bash launcher that invokes the real script
// through the bundled node — survives any copy/bundle step.
// The Windows zip ships .cmd shims instead of symlinks, so nothing to do there.
function replaceNodeShims() {
  if (isWindows) return;
  const shims = [
    { name: 'npm', target: 'lib/node_modules/npm/bin/npm-cli.js' },
    { name: 'npx', target: 'lib/node_modules/npm/bin/npx-cli.js' },
    { name: 'corepack', target: 'lib/node_modules/corepack/dist/corepack.js' },
  ];
  for (const { name, target } of shims) {
    const shimPath = join(nodeDir, 'bin', name);
    if (!existsSync(join(nodeDir, target))) continue;
    rmSync(shimPath, { force: true });
    writeFileSync(
      shimPath,
      '#!/bin/bash\n' +
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n' +
        `exec "$SCRIPT_DIR/node" "$SCRIPT_DIR/../${target}" "$@"\n`,
    );
    chmodSync(shimPath, 0o755);
  }
}

function fetchRipgrep() {
  const innerDir = `ripgrep-${RIPGREP_VERSION}-${ARCH_TRIPLE}`;
  const ext = isWindows ? 'zip' : 'tar.gz';
  const url = `https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/${innerDir}.${ext}`;
  const archive = join(sidecarDir, `rg.${ext}`);
  // Extract into a scratch dir so we can pluck out just the `rg` binary and
  // drop the accompanying docs/completions — keeps the bundle lean.
  const extractDir = join(sidecarDir, '_rg-extract');
  console.log(`Downloading ripgrep ${RIPGREP_VERSION}...`);
  rmSync(extractDir, { recursive: true, force: true });
  try {
    download(url, archive);
    extractArchive(archive, extractDir);
    const rgSource = join(extractDir, innerDir, isWindows ? 'rg.exe' : 'rg');
    if (!existsSync(rgSource)) throw new Error(`rg binary not found at ${rgSource}`);
    mkdirSync(sidecarBinDir, { recursive: true });
    copyFileSync(rgSource, rgBin);
    if (!isWindows) chmodSync(rgBin, 0o755);
  } finally {
    rmSync(archive, { force: true });
    rmSync(extractDir, { recursive: true, force: true });
  }
  writeStamp('rg', RG_STAMP);
}

function installPip() {
  console.log('Installing pip...');
  const getPip = join(sidecarDir, 'get-pip.py');
  try {
    download(GET_PIP_URL, getPip);
    execFileSync(pythonBin, [getPip, '--disable-pip-version-check'], { stdio: 'inherit' });
  } finally {
    rmSync(getPip, { force: true });
  }
}

function installPyDeps() {
  try {
    execFileSync(pythonBin, ['-m', 'pip', '--version'], { stdio: 'ignore' });
  } catch {
    installPip();
  }
  console.log('Installing dependencies...');
  execFileSync(
    pythonBin,
    [
      '-m', 'pip', 'install', '-q',
      '--disable-pip-version-check',
      '--no-warn-script-location',
      '-r', join(dir, 'requirements.txt'),
    ],
    { cwd: backendDir, stdio: 'inherit' },
  );
  writeStamp('deps', depsStampValue());
}

function installBrowserUse() {
  rmSync(browserUseDir, { recursive: true, force: true });
  execFileSync(pythonBin, ['-m', 'venv', browserUseDir], { stdio: 'inherit' });
  execFileSync(
    browserUsePython,
    [
      '-m', 'pip', 'install', '-q',
      '--disable-pip-version-check',
      `browser-use[cli]==${BROWSER_USE_VERSION}`,
    ],
    { stdio: 'inherit' },
  );
  writeStamp('browser-use', BROWSER_USE_STAMP);
}

function installAcpAdapters() {
  console.log('Installing ACP adapters...');
  npm([
    'install', '--prefix', sidecarDir,
    '--omit=dev', '--no-audit', '--no-fund', '--package-lock=false',
    `@agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION}`,
    `@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}`,
    `@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}`,
  ]);
  writeStamp('acp', ACP_STAMP);
}

// On Windows the launchers are .cmd files. CreateProcess runs those through
// cmd.exe, so the backend kills the whole tree (see app/utils/process.py)
// instead of signalling the leader.
function writeLauncherScript(name, relativeTarget, { viaNode }) {
  mkdirSync(sidecarBinDir, { recursive: true });
  if (isWindows) {
    const target = `"%~dp0..\\${relativeTarget.replaceAll('/', '\\')}"`;
    const body = viaNode ? `"%~dp0..\\node\\node.exe" ${target} %*` : `${target} %*`;
    writeFileSync(join(sidecarBinDir, `${name}.cmd`), `@echo off\r\n${body}\r\n`);
    return;
  }
  const launcher = join(sidecarBinDir, name);
  const target = `"$SCRIPT_DIR/../${relativeTarget}"`;
  const body = viaNode ? `exec "$SCRIPT_DIR/../node/bin/node" ${target} "$@"` : `exec ${target} "$@"`;
  writeFileSync(
    launcher,
    '#!/bin/bash\n' + 'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n' + `${body}\n`,
  );
  chmodSync(launcher, 0o755);
}

function writeAcpLaunchers() {
  writeLauncherScript(
    'claude-agent-acp',
    'node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js',
    { viaNode: true },
  );
  writeLauncherScript('codex-acp', 'node_modules/@agentclientprotocol/codex-acp/dist/index.js', {
    viaNode: true,
  });
  if (isWindows) {
    // claude-code's postinstall drops a native binary; npm's .cmd shim for it
    // lives in node_modules/.bin, but bin/ keeps the same name on both OSes.
    writeLauncherScript('claude', 'node_modules/@anthropic-ai/claude-code/bin/claude.exe', {
      viaNode: false,
    });
  } else {
    writeLauncherScript('claude', 'node_modules/.bin/claude', { viaNode: false });
  }
}

function copySource() {
  console.log('Copying source...');
  const filter = (src) => !src.includes('__pycache__') && !src.endsWith('.pyc');
  rmSync(join(sidecarDir, 'app'), { recursive: true, force: true });
  rmSync(join(sidecarDir, 'migrations'), { recursive: true, force: true });
  cpSync(join(backendDir, 'app'), join(sidecarDir, 'app'), { recursive: true, filter });
  cpSync(join(backendDir, 'migrations'), join(sidecarDir, 'migrations'), { recursive: true, filter });
  copyFileSync(join(backendDir, 'alembic.ini'), join(sidecarDir, 'alembic.ini'));
  copyFileSync(join(backendDir, 'migrate.py'), join(sidecarDir, 'migrate.py'));
  copyFileSync(join(dir, 'entry.py'), join(sidecarDir, 'entry.py'));
  // Bundle the MCP server alongside the backend so agent.py can compute its path
  // (sidecar/app/services/agent.py -> sidecar/mcp-server/server.py) without config
  rmSync(join(sidecarDir, 'mcp-server'), { recursive: true, force: true });
  cpSync(join(rootDir, 'mcp-server'), join(sidecarDir, 'mcp-server'), {
    recursive: true,
    filter: (src) =>
      !src.includes('__pycache__') && !src.endsWith('.pyc') && !src.includes('.venv'),
  });
}

function writeLauncher() {
  // Prepend the bundled bin dir to PATH so the user's shell (spawned via
  // `bash -lc` by the host sandbox provider) can find our bundled `rg`
  // without any extra install step. Login shells still run the user's rc
  // files afterwards, so any user-installed `rg` earlier in PATH keeps
  // precedence — this is a floor, not a ceiling.
  if (isWindows) {
    // desktop.rs spawns python.exe directly on Windows; this launcher is for
    // running the sidecar by hand.
    writeFileSync(
      join(sidecarDir, 'agentrove-backend.cmd'),
      '@echo off\r\n' +
        'set "PYTHONPATH=%~dp0"\r\n' +
        'set "PATH=%~dp0bin;%~dp0node;%~dp0node_modules\\.bin;%PATH%"\r\n' +
        '"%~dp0python\\python.exe" "%~dp0entry.py" %*\r\n',
    );
    return;
  }
  const launcher = join(sidecarDir, 'agentrove-backend');
  writeFileSync(
    launcher,
    '#!/bin/bash\n' +
      'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n' +
      'export PYTHONPATH="$SCRIPT_DIR"\n' +
      'export PATH="$SCRIPT_DIR/bin:$SCRIPT_DIR/node/bin:$PATH"\n' +
      'exec "$SCRIPT_DIR/python/bin/python3" "$SCRIPT_DIR/entry.py" "$@"\n',
  );
  chmodSync(launcher, 0o755);
}

function run() {
  if (forceClean && existsSync(sidecarDir)) {
    rmSync(sidecarDir, { recursive: true, force: true });
  }
  mkdirSync(sidecarDir, { recursive: true });

  const pythonChanged = !upToDate('python', PYTHON_STAMP, pythonBin);
  if (pythonChanged) fetchPython();
  else console.log('Python already installed, skipping download.');

  const nodeChanged = !upToDate('node', NODE_STAMP, nodeBin, npmBin);
  if (nodeChanged) fetchNode();
  else console.log('Node already installed, skipping download.');
  replaceNodeShims();

  if (!nodeChanged && upToDate('acp', ACP_STAMP, claudeAcpBin, codexAcpBin)) {
    console.log('ACP adapters already installed, skipping install.');
  } else {
    installAcpAdapters();
  }

  writeAcpLaunchers();

  if (!pythonChanged && upToDate('deps', depsStampValue())) {
    console.log('Dependencies up to date, skipping install.');
  } else {
    installPyDeps();
  }

  if (upToDate('browser-use', BROWSER_USE_STAMP, browserUsePython)) {
    console.log('browser-use already installed, skipping install.');
  } else if (isWindows) {
    // browser-use has not been validated on Windows yet; keep the build going
    // without it until the browser tools get a Windows pass.
    try {
      installBrowserUse();
    } catch (error) {
      console.warn(
        `browser-use install failed on Windows, continuing without it: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  } else {
    installBrowserUse();
  }

  if (upToDate('rg', RG_STAMP, rgBin)) {
    console.log('ripgrep already installed, skipping download.');
  } else {
    fetchRipgrep();
  }

  copySource();
  writeLauncher();
  console.log('Done');
}

try {
  run();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
