'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DEFAULT_MAC_SIGNING_IDENTITY = 'AFK Stable Local Code Signing';
const DEFAULT_MAC_SIGNING_KEYCHAIN = path.join(
  os.homedir(),
  'Library',
  'Application Support',
  'AFK',
  'signing',
  'afk-local-signing.keychain-db'
);
const DEFAULT_MAC_PYTHON_VERSION = '3.12';

function macPythonVersion() {
  return process.env.AFK_MAC_PYTHON_FRAMEWORK_VERSION || DEFAULT_MAC_PYTHON_VERSION;
}

async function stampWindowsIcon(context) {
  const exeName = `${context.packager.appInfo.productFilename}.exe`;
  const exePath = path.join(context.appOutDir, exeName);
  const iconPath = path.join(context.packager.projectDir, 'assets', 'icon.ico');
  const { rcedit } = await import('rcedit');

  await rcedit(exePath, {
    icon: iconPath,
    'version-string': {
      ProductName: context.packager.appInfo.productName,
      FileDescription: context.packager.appInfo.productName
    }
  });

  console.log(`Stamped Windows icon on ${exePath}`);
}

function signMacApp(context) {
  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);

  const configuredIdentity = process.env.AFK_MAC_CODESIGN_IDENTITY || DEFAULT_MAC_SIGNING_IDENTITY;
  const configuredKeychain = process.env.AFK_MAC_CODESIGN_KEYCHAIN || DEFAULT_MAC_SIGNING_KEYCHAIN;
  const hasKeychain = configuredKeychain && fs.existsSync(configuredKeychain);
  let identity = '-';

  if (configuredIdentity && configuredIdentity !== '-') {
    const findArgs = ['find-identity', '-p', 'codesigning'];
    if (hasKeychain) findArgs.push(configuredKeychain);
    try {
      const identities = execFileSync('security', findArgs, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
      if (identities.includes(`"${configuredIdentity}"`)) identity = configuredIdentity;
    } catch (_) {
      // Fall back to ad-hoc signing below.
    }
  }

  const args = ['--force', '--deep'];
  if (identity !== '-' && hasKeychain) args.push('--keychain', configuredKeychain);
  args.push('--sign', identity, appPath);

  if (identity === '-') {
    console.warn('AFK mac signing identity not found; using ad-hoc signing. Accessibility permission may need to be re-granted after rebuilds.');
  } else {
    console.log(`Signing mac app with "${identity}"`);
  }

  signModifiedPythonLaunchers(context, identity, hasKeychain ? configuredKeychain : '');
  execFileSync('codesign', args, {
    stdio: 'inherit'
  });
}

function signModifiedPythonLaunchers(context, identity, keychain) {
  if (context.electronPlatformName !== 'darwin') return;
  const appName = context.packager.appInfo.productFilename;
  const runtimeBin = path.join(
    context.appOutDir,
    `${appName}.app`,
    'Contents',
    'Resources',
    'python',
    'runtime',
    'bin'
  );
  for (const name of ['python', 'python3', `python${macPythonVersion()}`]) {
    const executable = path.join(runtimeBin, name);
    if (!fs.existsSync(executable)) continue;
    const args = ['--force'];
    if (identity !== '-' && keychain) args.push('--keychain', keychain);
    args.push('--sign', identity, executable);
    execFileSync('codesign', args, { stdio: 'inherit' });
  }
}

function runTool(command, args) {
  execFileSync(command, args, { stdio: 'inherit' });
}

function appResourcesDir(context) {
  const appName = context.packager.appInfo.productFilename;
  return context.electronPlatformName === 'darwin'
    ? path.join(context.appOutDir, `${appName}.app`, 'Contents', 'Resources')
    : path.join(context.appOutDir, 'resources');
}

function copyBundledModels(context) {
  if (process.env.AFK_BUNDLE_MODELS !== '1') return;

  const sourceRoot = process.env.AFK_BUNDLE_MODELS_DIR
    || path.join(os.homedir(), 'Library', 'Application Support', 'AFK', 'models');
  if (!fs.existsSync(sourceRoot)) {
    throw new Error(`AFK_BUNDLE_MODELS=1 but model directory does not exist: ${sourceRoot}`);
  }

  const targetRoot = path.join(appResourcesDir(context), 'models');
  fs.rmSync(targetRoot, { recursive: true, force: true });
  fs.mkdirSync(targetRoot, { recursive: true });

  let copiedAny = false;
  for (const name of ['parakeet-v3', 'clarify']) {
    const source = path.join(sourceRoot, name);
    if (!fs.existsSync(source)) {
      console.warn(`after-pack: bundled models requested but ${source} is missing`);
      continue;
    }
    fs.cpSync(source, path.join(targetRoot, name), { recursive: true, dereference: false });
    copiedAny = true;
    console.log(`after-pack: bundled model directory ${source} -> ${path.join(targetRoot, name)}`);
  }

  if (!copiedAny) {
    throw new Error(`AFK_BUNDLE_MODELS=1 but no supported model directories were found in ${sourceRoot}`);
  }
}

/**
 * Rewrite pyvenv.cfg so the bundled venv is fully relocatable.
 *
 * The venv is created with --copies so python/runtime/bin/python is a real
 * Mach-O binary. However, pyvenv.cfg still contains the absolute `home` path
 * from the build machine. On a clean Mac that path often does not exist, so
 * Python's venv bootstrapping fails before the backend emits ready.
 *
 * Fix: point `home` at the Python.framework bundled into AFK.app.
 */
function fixPyvenvCfg(context) {
  const appOutDir = context.appOutDir;
  const appName = context.packager.appInfo.productFilename;

  // macOS: inside .app bundle
  const cfgPath = context.electronPlatformName === 'darwin'
    ? path.join(appOutDir, `${appName}.app`, 'Contents', 'Resources', 'python', 'runtime', 'pyvenv.cfg')
    : path.join(appOutDir, 'resources', 'python', 'runtime', 'pyvenv.cfg');

  if (!fs.existsSync(cfgPath)) {
    console.warn(`after-pack: pyvenv.cfg not found at ${cfgPath}; skipping relocation fix`);
    return;
  }

  const existing = fs.readFileSync(cfgPath, 'utf8');
  const fixed = existing
    .split('\n')
    .map(line => {
      if (/^home\s*=/.test(line)) {
        return context.electronPlatformName === 'darwin'
          ? `home = ../../../Frameworks/Python.framework/Versions/${macPythonVersion()}/bin`
          : 'home = ./bin';
      }
      // Drop `executable` and `command` lines; they contain absolute build-machine paths.
      if (/^(executable|command)\s*=/.test(line)) return null;
      return line;
    })
    .filter(line => line !== null)
    .join('\n');

  fs.writeFileSync(cfgPath, fixed, 'utf8');
  console.log(`after-pack: rewrote pyvenv.cfg to use bundled Python home -> ${cfgPath}`);
}

function bundleMacPythonFramework(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const version = macPythonVersion();
  const source = (
    process.env.AFK_MAC_PYTHON_FRAMEWORK
      ? path.join(process.env.AFK_MAC_PYTHON_FRAMEWORK, 'Versions', version)
      : ''
  )
    || '/Library/Frameworks/Python.framework';
  const sourceVersionDir = source.endsWith(`Versions/${version}`)
    ? source
    : path.join(source, 'Versions', version);
  if (!fs.existsSync(sourceVersionDir)) {
    console.warn(`after-pack: Python.framework ${version} not found at ${sourceVersionDir}; backend may require Python on the target Mac`);
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const contentsDir = path.join(context.appOutDir, `${appName}.app`, 'Contents');
  const frameworkDir = path.join(contentsDir, 'Frameworks', 'Python.framework');
  const targetVersionDir = path.join(frameworkDir, 'Versions', version);
  fs.rmSync(frameworkDir, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(targetVersionDir), { recursive: true });
  fs.cpSync(sourceVersionDir, targetVersionDir, { recursive: true, dereference: false });
  fs.symlinkSync(version, path.join(frameworkDir, 'Versions', 'Current'));
  fs.symlinkSync('Versions/Current/Python', path.join(frameworkDir, 'Python'));
  fs.symlinkSync('Versions/Current/Resources', path.join(frameworkDir, 'Resources'));
  fs.symlinkSync('Versions/Current/Headers', path.join(frameworkDir, 'Headers'));
  fs.rmSync(path.join(frameworkDir, '.DS_Store'), { force: true });
  fs.rmSync(path.join(frameworkDir, 'Versions', version, '.DS_Store'), { force: true });
  fs.rmSync(path.join(frameworkDir, 'Versions', version, '_CodeSignature'), { recursive: true, force: true });
  const pythonAppPlist = path.join(
    targetVersionDir,
    'Resources',
    'Python.app',
    'Contents',
    'Info.plist'
  );
  if (fs.existsSync(pythonAppPlist)) {
    try {
      execFileSync('/usr/libexec/PlistBuddy', ['-c', 'Add :LSUIElement bool true', pythonAppPlist]);
    } catch (_) {
      execFileSync('/usr/libexec/PlistBuddy', ['-c', 'Set :LSUIElement true', pythonAppPlist]);
    }
    console.log(`after-pack: configured bundled Python.app as a background agent -> ${pythonAppPlist}`);
  }
  console.log(`after-pack: bundled Python.framework -> ${frameworkDir}`);

  const oldInstallName = `/Library/Frameworks/Python.framework/Versions/${version}/Python`;
  const newInstallName = `@executable_path/../../../../Frameworks/Python.framework/Versions/${version}/Python`;
  const runtimeBin = path.join(contentsDir, 'Resources', 'python', 'runtime', 'bin');
  for (const name of ['python', 'python3', `python${version}`]) {
    const executable = path.join(runtimeBin, name);
    if (!fs.existsSync(executable)) continue;
    try {
      runTool('install_name_tool', ['-change', oldInstallName, newInstallName, executable]);
      console.log(`after-pack: rewrote Python framework load path in ${executable}`);
    } catch (err) {
      console.warn(`after-pack: install_name_tool failed for ${executable}: ${err.message || err}`);
    }
  }
}

exports.default = async function afterPack(context) {
  bundleMacPythonFramework(context);
  copyBundledModels(context);

  // Always fix pyvenv.cfg first; the backend will fail to start on any
  // machine that doesn't have the developer's Python installation otherwise.
  fixPyvenvCfg(context);

  if (context.electronPlatformName === 'win32') {
    await stampWindowsIcon(context);
  } else if (context.electronPlatformName === 'darwin') {
    signMacApp(context);
  }
};
