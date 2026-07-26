import type { CapacitorConfig } from '@capacitor/cli';

const appUrl = process.env.LUMEN_APP_URL || 'https://lumenboard.org/app';

const config: CapacitorConfig = {
  appId: 'org.lumenboard.app',
  appName: 'Lumen',
  webDir: 'www',
  server: {
    url: appUrl,
    allowNavigation: ['lumenboard.org', '*.lumenboard.org'],
    cleartext: appUrl.startsWith('http://'),
  },
  ios: {
    contentInset: 'automatic',
    preferredContentMode: 'mobile',
    allowsLinkPreview: false,
    scrollEnabled: true,
  },
  android: {
    allowMixedContent: false,
    captureInput: true,
    webContentsDebuggingEnabled: false,
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 1200,
      backgroundColor: '#f4efe7',
      showSpinner: false,
    },
    StatusBar: {
      style: 'LIGHT',
      backgroundColor: '#f4efe7',
      overlaysWebView: false,
    },
  },
};

export default config;
