// Window security, adapted from electron-starter-template (src/main/security/*),
// Copyright (c) 2026 Hasan Aboul Hasan, used under desktop/TEMPLATE-LICENSE.
//
// The window only ever shows the local MapProspector server. Anything else (Google sign-in,
// business websites, Maps links) opens in your normal browser.
import path from 'path';
import { pathToFileURL } from 'url';
import { session, shell, type WebContents } from 'electron';

// Shown while the server starts; the only local file the window may display.
export const LOADING_PAGE = pathToFileURL(path.join(__dirname, '..', 'static', 'loading.html')).href;

// MapProspector's pages use inline scripts and load htmx/Leaflet from unpkg and map tiles over https.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://unpkg.com",
  "style-src 'self' 'unsafe-inline' https://unpkg.com",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'self'",
].join('; ');

// navigator.clipboard.writeText (the "Copy text" buttons) needs this one.
const ALLOWED_PERMISSIONS = new Set(['clipboard-sanitized-write']);

export function isAppUrl(url: string, origin: string | null): boolean {
  try {
    const u = new URL(url);
    if (u.protocol === 'file:') return url === LOADING_PAGE;
    return origin !== null && u.origin === origin;
  } catch {
    return false;
  }
}

function openOutside(url: string): void {
  try {
    if (['http:', 'https:', 'mailto:', 'tel:'].includes(new URL(url).protocol)) void shell.openExternal(url);
  } catch {
    // not a URL: ignore
  }
}

export function registerSessionPolicy(origin: () => string | null): void {
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.url.startsWith('file:') || !isAppUrl(details.url, origin())) {
      callback({});
      return;
    }
    callback({ responseHeaders: { ...details.responseHeaders, 'Content-Security-Policy': [CSP] } });
  });
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
    callback(ALLOWED_PERMISSIONS.has(permission) && isAppUrl(wc.getURL(), origin()));
  });
  session.defaultSession.setPermissionCheckHandler((_wc, permission, requestingOrigin) => {
    return ALLOWED_PERMISSIONS.has(permission) && requestingOrigin === origin();
  });
}

// Attached to every page (main window and pop-ups).
export function attachGuards(wc: WebContents, origin: () => string | null): void {
  const keepInApp = (details: { url: string; preventDefault: () => void }) => {
    if (isAppUrl(details.url, origin())) return;
    details.preventDefault();
    openOutside(details.url);
  };
  wc.on('will-navigate', keepInApp);
  // Server redirects too: /gbp/oauth/start redirects to Google, which blocks sign-in inside apps.
  wc.on('will-redirect', keepInApp);

  // Links with target="_blank": app pages (share links, report print view) open in a new app
  // window; everything else goes to the browser.
  wc.setWindowOpenHandler(({ url }) => {
    if (isAppUrl(url, origin()) && !url.startsWith('file:')) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          autoHideMenuBar: true,
          webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
        },
      };
    }
    openOutside(url);
    return { action: 'deny' };
  });

  wc.on('will-attach-webview', (event) => event.preventDefault());
}
