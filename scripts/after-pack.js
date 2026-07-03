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

  execFileSync('codesign', args, {
    stdio: 'inherit'
  });
}

exports.default = async function afterPack(context) {
  if (context.electronPlatformName === 'win32') {
    await stampWindowsIcon(context);
  } else if (context.electronPlatformName === 'darwin') {
    signMacApp(context);
  }
};
