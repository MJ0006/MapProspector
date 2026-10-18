// Starts and stops the MapProspector server (python -m uvicorn app.main:app) for the desktop window.
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
  fs.writeFileSync(p.envFile, fs.existsSync(example) ? fs.readFileSync(example) : '');
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
    const child = spawn(p.python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(this.port)], {
      cwd: p.root,
      env,
      windowsHide: true, // no console window; Claude and Local SEO Brain child processes stay hidden too
    });
    this.child = child;
    child.stdout?.pipe(log, { end: false });
    child.stderr?.pipe(log, { end: false });

    let exited = false;
    let spawnError = '';
    child.on('error', (err) => {
      spawnError = err.message;
      log.write(`could not start ${p.python}: ${err.message}\n`);
    });
    child.on('exit', (code) => {
      exited = true;
      log.write(`--- server exited (code ${code})\n`);
      if (this.child === child) this.child = null;
      if (!this.stopping) this.onCrash(code);
    });

    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      if (exited || spawnError) throw new Error(spawnError || 'The server stopped while starting.');
      try {
        const r = await fetch(`${this.url}/static/favicon.svg`);
        if (r.ok) return this.url;
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
    if (process.platform === 'win32') {
      // /T also ends a running `claude` or Local SEO Brain process started by the server.
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
    } else {
      child.kill('SIGTERM');
    }
    const timer = setTimeout(() => child.kill('SIGKILL'), 8_000);
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
