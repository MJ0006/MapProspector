// MapProspector for Windows: starts the local server and shows it in an app window.
// Window, single-instance and close-to-tray handling follow electron-starter-template
// (src/main/index.ts), Copyright (c) 2026 Hasan Aboul Hasan, used under desktop/TEMPLATE-LICENSE.
import fs from 'fs';
import path from 'path';
import { app, BrowserWindow, dialog, Menu, shell, Tray, type MenuItemConstructorOptions } from 'electron';
import { attachGuards, LOADING_PAGE, registerSessionPolicy } from './security';
import { ensureEnvFile, resolvePaths, Server } from './server';

const ICON = path.join(__dirname, '..', 'static', 'icon.png');
const startHidden = process.argv.includes('--hidden'); // started with Windows: stay in the tray

const paths = resolvePaths();
const server = new Server(paths);
const origin = (): string | null => server.url || null;
let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;

if (process.platform === 'win32') app.setAppUserModelId('com.mj0006.mapprospector');

if (!app.requestSingleInstanceLock()) {
  // Only one copy may run: two servers would run every schedule twice.
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
  app.whenReady().then(startup);
}

function showWindow(): void {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createWindow(): BrowserWindow {
  const w = new BrowserWindow({
    width: 1366,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    show: false,
    icon: ICON,
    title: 'MapProspector',
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  w.once('ready-to-show', () => {
    if (!startHidden) w.show();
  });
  // Closing the window keeps the app in the tray, so schedules, automations and review syncs keep running.
  w.on('close', (event) => {
    if (quitting || !tray) return;
    event.preventDefault();
    w.hide();
    tellAboutTrayOnce();
  });
  return w;
}

function tellAboutTrayOnce(): void {
  const flag = path.join(app.getPath('userData'), 'tray-notice-shown');
  if (fs.existsSync(flag) || !tray || process.platform !== 'win32') return;
  fs.writeFileSync(flag, '');
  tray.displayBalloon({
    title: 'MapProspector is still running',
    content: 'It stays in the tray so scheduled scans and automations keep running. Right-click the icon to quit.',
    iconType: 'info',
  });
}

function quit(): void {
  quitting = true;
  app.quit();
}

async function restartServer(): Promise<void> {
  try {
    await win?.loadURL(LOADING_PAGE);
    await server.restart();
    await win?.loadURL(server.url);
  } catch (err) {
    failed(err);
  }
}

function failed(err: unknown): void {
  const choice = dialog.showMessageBoxSync({
    type: 'error',
    title: 'MapProspector',
    message: `MapProspector's server is not running.\n${err instanceof Error ? err.message : String(err)}`,
    detail: server.logTail(),
    buttons: ['Try again', 'Open log', 'Quit'],
    defaultId: 0,
    cancelId: 2,
  });
  if (choice === 0) void restartServer();
  else if (choice === 1) {
    void shell.openPath(paths.logFile);
    failed(err);
  } else quit();
}

const actions = {
  openSettingsFile: () => void shell.openPath(paths.envFile),
  openDataFolder: () => void shell.openPath(paths.dataDir),
  openLog: () => void shell.openPath(paths.logFile),
  openInBrowser: () => server.url && void shell.openExternal(server.url),
};

function buildMenu(): Menu {
  const template: MenuItemConstructorOptions[] = [
    {
      label: 'File',
      submenu: [
        { label: 'API keys and settings (.env)…', click: actions.openSettingsFile },
        { label: 'Restart server (after editing .env)', click: () => void restartServer() },
        { type: 'separator' },
        { label: 'Open data folder', click: actions.openDataFolder },
        { label: 'Open server log', click: actions.openLog },
        { label: 'Open in browser', click: actions.openInBrowser },
        { type: 'separator' },
        { label: 'Quit MapProspector', accelerator: 'Ctrl+Q', click: quit },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Back', accelerator: 'Alt+Left', click: () => win?.webContents.navigationHistory.goBack() },
        { label: 'Forward', accelerator: 'Alt+Right', click: () => win?.webContents.navigationHistory.goForward() },
        { role: 'reload' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        { role: 'toggleDevTools' },
      ],
    },
    { role: 'windowMenu' },
  ];
  return Menu.buildFromTemplate(template);
}

function createTray(): Tray | null {
  try {
    const t = new Tray(ICON);
    t.setToolTip('MapProspector');
    const login = () => app.getLoginItemSettings({ args: ['--hidden'] }).openAtLogin;
    const refresh = () =>
      t.setContextMenu(
        Menu.buildFromTemplate([
          { label: 'Open MapProspector', click: showWindow },
          { label: 'Open in browser', click: actions.openInBrowser },
          { type: 'separator' },
          {
            label: 'Start when I sign in to Windows',
            type: 'checkbox',
            checked: login(),
            click: (item) => {
              app.setLoginItemSettings({ openAtLogin: item.checked, args: ['--hidden'] });
              refresh();
            },
          },
          { label: 'Restart server', click: () => void restartServer() },
          { type: 'separator' },
          { label: 'Quit MapProspector', click: quit },
        ]),
      );
    refresh();
    t.on('double-click', showWindow);
    t.on('click', showWindow);
    return t;
  } catch {
    return null; // no tray on this desktop: closing the window quits instead
  }
}

async function startup(): Promise<void> {
  ensureEnvFile(paths);
  registerSessionPolicy(origin);
  app.on('web-contents-created', (_e, wc) => attachGuards(wc, origin));
  Menu.setApplicationMenu(buildMenu());
  win = createWindow();
  tray = createTray();
  server.onCrash = (code) => failed(new Error(`It stopped unexpectedly (exit code ${code}).`));
  await win.loadURL(LOADING_PAGE);
  try {
    await server.start();
    await win.loadURL(server.url);
  } catch (err) {
    failed(err);
  }
}

let stopped = false;
app.on('before-quit', () => {
  quitting = true;
});
app.on('will-quit', (event) => {
  if (stopped) return;
  event.preventDefault();
  void server.stop().finally(() => {
    stopped = true;
    app.quit();
  });
});
app.on('window-all-closed', () => {
  if (!tray) quit();
});
