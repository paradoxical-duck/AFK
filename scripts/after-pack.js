'use strict';

const { execFileSync } = require('child_process');
const path = require('path');

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
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', appPath], {
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
