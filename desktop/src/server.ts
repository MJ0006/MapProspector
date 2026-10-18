// Starts and stops the MapProspector server (python -m app.desktop_run) for the desktop window.
import { spawn, type ChildProcess } from 'child_process';
import fs from 'fs';
import net from 'net';
import path from 'path';
import { app } from 'electron';

const DEFAULT_PORT = 8000; // matches the Google OAuth redirect URI in the README

export interface Paths {
  python: string;
  root: string; // folder that holds app/ and vendor/
  dataDir: string;
  envFile: string;
  logFile: string;
}

export function resolvePaths(): Paths {
  const userData = app.getPath('userData');
  const packaged = app.isPackaged;
  return {
    // Installed app: the Python and server copied in by .github/workflows/desktop.yml.
    // From a checkout: the repo itself and your own Python (MAPPROSPECTOR_PYTHON, e.g. .venv\Scripts\python.exe).
    python: packaged
      ? path.join(process.resourcesPath, 'python', process.platform === 'win32' ? 'python.exe' : 'bin/python3')
      : process.env.MAPPROSPECTOR_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
    root: packaged ? path.join(process.resourcesPath, 'server') : path.resolve(__dirname, '..', '..'),
    dataDir: path.join(userData, 'data'),
    envFile: path.join(userData, '.env'),
    logFile: path.join(userData, 'logs', 'server.log'),
  };
}

/** First run: start from the repo's .env.example so every key is listed for you to fill in. */
export function ensureEnvFile(p: Paths): void {
  if (fs.existsSync(p.envFile)) return;
  fs.mkdirSync(path.dirname(p.envFile), { recursive: true });
  const example = path.join(p.root, '.env.example');
  const text = fs.existsSync(example) ? fs.readFileSync(example, 'utf8') : '';
  // The app always keeps its data next to this file, so the DATA_DIR setting would be ignored here.
  fs.writeFileSync(p.envFile, text.replace(/^# Where the database[^\n]*\n(DATA_DIR=[^\n]*\n)?/m, ''));
}

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.once('error', () => resolve(false));
    s.listen(port, '127.0.0.1', () => s.close(() => resolve(true)));
  });
}

function anyPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const port = (s.address() as net.AddressInfo).port;
      s.close(() => resolve(port));
    });
  });
}

function envValue(file: string, key: string): string {
  try {
    const line = fs.readFileSync(file, 'utf8').split(/\r?\n/).find((l) => l.trim().startsWith(`${key}=`));
    return line ? line.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '') : '';
  } catch {
    return '';
  }
}

function killChildren(pid: number | undefined): Promise<void> {
  if (!pid) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => resolve();
    if (process.platform === 'win32') {
      const ps = `Get-CimInstance Win32_Process -Filter "ParentProcessId=${pid}" | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }`;
      const k = spawn('powershell', ['-NoProfile', '-NonInteractive', '-Command', ps], { windowsHide: true });
      k.on('exit', done);
      k.on('error', done);
      setTimeout(done, 8_000);
    } else {
      const k = spawn('pkill', ['-TERM', '-P', String(pid)], { stdio: 'ignore' }); // the server's direct children
      k.on('exit', done);
      k.on('error', done);
    }
  });
}

export class Server {
  private child: ChildProcess | null = null;
  private stopping = false;
  port = 0;
  url = '';
  /** Called when the server exits without being asked to. */
  onCrash: (code: number | null) => void = () => {};

  constructor(readonly paths: Paths) {}

  async start(): Promise<string> {
    const p = this.paths;
    if (!this.port) this.port = (await portFree(DEFAULT_PORT)) ? DEFAULT_PORT : await anyPort();
    this.url = `http://127.0.0.1:${this.port}`;
    fs.mkdirSync(p.dataDir, { recursive: true });
    fs.mkdirSync(path.dirname(p.logFile), { recursive: true });
    try {
      if (fs.statSync(p.logFile).size > 5_000_000) fs.renameSync(p.logFile, `${p.logFile}.old`);
    } catch {
      // no log yet
    }

    const env: NodeJS.ProcessEnv = {
      ...process.env,
      DATA_DIR: p.dataDir,
      MAPPROSPECTOR_ENV_FILE: p.envFile,
      PYTHONUTF8: '1', // Windows: read and write files and child output as UTF-8
      PYTHONIOENCODING: 'utf-8',
    };
    // Share and review links must point at the port actually in use, unless you set a public URL.
    const publicUrl = envValue(p.envFile, 'PUBLIC_BASE_URL');
    if (this.port !== DEFAULT_PORT && (!publicUrl || /\/\/(127\.0\.0\.1|localhost)[:/]?/.test(publicUrl))) {
      env.PUBLIC_BASE_URL = this.url;
    }

    const log = fs.createWriteStream(p.logFile, { flags: 'a' });
    log.write(`\n--- ${new Date().toISOString()} starting on ${this.url} (python: ${p.python})\n`);
    this.stopping = false;
    // stdin stays open as the link to this window: the server stops when it closes (see app/desktop_run.py).
    const child = spawn(p.python, ['-m', 'app.desktop_run', '--port', String(this.port)], {
      cwd: p.root,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true, // no console window; Claude and Local SEO Brain child processes stay hidden too
    });
    child.stdin?.on('error', () => {}); // writing "quit" to a server that already exited
    this.child = child;
    child.stdout?.pipe(log, { end: false });
    child.stderr?.pipe(log, { end: false });

    let exited = false;
    let ready = false;
    let spawnError = '';
    child.on('error', (err) => {
      spawnError = err.message;
      log.write(`could not start ${p.python}: ${err.message}\n`);
    });
    child.on('exit', (code) => {
      exited = true;
      log.write(`--- server exited (code ${code})\n`);
      if (this.child === child) this.child = null;
      // A failure while starting is reported once, by start() throwing; onCrash is for a server that was running.
      if (!this.stopping && ready) this.onCrash(code);
    });

    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      if (exited || spawnError) throw new Error(spawnError || 'The server stopped while starting.');
      try {
        const r = await fetch(`${this.url}/static/favicon.svg`);
        if (r.ok) {
          ready = true;
          return this.url;
        }
      } catch {
        // not listening yet
      }
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error('The server did not start within 90 seconds.');
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    this.stopping = true;
    const done = new Promise<void>((resolve) => child.once('exit', () => resolve()));
    // First end what the server started (a running `claude`/`codex` call, a Local SEO Brain script), which would
    // otherwise keep running and keep files open; then ask the server itself to stop cleanly.
    await killChildren(child.pid);
    child.stdin?.write('quit\n'); // clean stop: the scheduler and database close properly
    const timer = setTimeout(() => {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
      } else {
        child.kill('SIGKILL');
      }
    }, 12_000); // before app/desktop_run.py's own 15 s exit, so the whole tree is still reachable
    await done;
    clearTimeout(timer);
  }

  async restart(): Promise<string> {
    await this.stop();
    return this.start();
  }

  logTail(lines = 25): string {
    try {
      return fs.readFileSync(this.paths.logFile, 'utf8').trimEnd().split('\n').slice(-lines).join('\n');
    } catch {
      return '';
    }
  }
}
