'use strict';

const path = require('path');

exports.default = async function stampWindowsIcon(context) {
  if (context.electronPlatformName !== 'win32') return;

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
};
