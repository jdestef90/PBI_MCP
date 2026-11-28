"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const PKG_NAME = "pbi-mcp-server";
const MIN_PY = [3, 11];
const MAX_PY_EXCLUSIVE = [3, 13];

function loadPackageVersion() {
  const pkgPath = path.resolve(__dirname, "..", "package.json");
  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
  return pkg.version;
}

function getPackageSpec(env = process.env, version = loadPackageVersion()) {
  if (env.PBI_MCP_SPEC && env.PBI_MCP_SPEC.trim()) {
    return env.PBI_MCP_SPEC.trim();
  }
  return `${PKG_NAME}==${version}`;
}

function getCacheDir(env = process.env, platform = process.platform, homedir = os.homedir()) {
  if (env.PBI_MCP_CACHE_DIR && env.PBI_MCP_CACHE_DIR.trim()) {
    return path.resolve(env.PBI_MCP_CACHE_DIR.trim());
  }
  if (platform === "win32") {
    const localAppData = env.LOCALAPPDATA || path.join(homedir, "AppData", "Local");
    return path.join(localAppData, PKG_NAME);
  }
  const xdg = env.XDG_CACHE_HOME || path.join(homedir, ".cache");
  return path.join(xdg, PKG_NAME);
}

function isSupportedPythonVersion(major, minor) {
  if (!Number.isInteger(major) || !Number.isInteger(minor)) {
    return false;
  }
  if (major < MIN_PY[0] || (major === MIN_PY[0] && minor < MIN_PY[1])) {
    return false;
  }
  if (major > MAX_PY_EXCLUSIVE[0] || (major === MAX_PY_EXCLUSIVE[0] && minor >= MAX_PY_EXCLUSIVE[1])) {
    return false;
  }
  return true;
}

function canExecute(cmd, args, runner = spawnSync) {
  const res = runner(cmd, args, {
    stdio: ["ignore", "pipe", "pipe"],
    encoding: "utf8",
    windowsHide: true,
  });
  return res && !res.error && res.status === 0 ? (res.stdout || "").trim() : null;
}

function findUvx({
  env = process.env,
  platform = process.platform,
  homedir = os.homedir(),
  runner = spawnSync,
  existsSync = fs.existsSync,
} = {}) {
  if (env.PBI_MCP_NO_UVX === "1" || env.PBI_MCP_NO_UVX === "true") {
    return null;
  }
  const exe = platform === "win32" ? "uvx.exe" : "uvx";
  const candidates = [
    "uvx",
    path.join(homedir, ".local", "bin", exe),
    path.join(homedir, ".cargo", "bin", exe),
  ];
  for (const candidate of candidates) {
    if (candidate !== "uvx" && !existsSync(candidate)) {
      continue;
    }
    const out = canExecute(candidate, ["--version"], runner);
    if (out !== null) {
      return candidate;
    }
  }
  return null;
}

function findCompatiblePython({
  platform = process.platform,
  runner = spawnSync,
} = {}) {
  const candidates =
    platform === "win32"
      ? [
          { command: "py", prefixArgs: ["-3.12"] },
          { command: "py", prefixArgs: ["-3.11"] },
          { command: "python3.12", prefixArgs: [] },
          { command: "python3.11", prefixArgs: [] },
          { command: "python", prefixArgs: [] },
        ]
      : [
          { command: "python3.12", prefixArgs: [] },
          { command: "python3.11", prefixArgs: [] },
          { command: "python3", prefixArgs: [] },
          { command: "python", prefixArgs: [] },
        ];

  const probe = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')";
  for (const cand of candidates) {
    const out = canExecute(cand.command, [...cand.prefixArgs, "-c", probe], runner);
    if (!out) {
      continue;
    }
    const match = /^(\d+)\.(\d+)$/.exec(out);
    if (!match) {
      continue;
    }
    const major = Number(match[1]);
    const minor = Number(match[2]);
    if (isSupportedPythonVersion(major, minor)) {
      return { ...cand, version: `${major}.${minor}` };
    }
  }
  return null;
}

function getVenvPaths(cacheDir, version, platform = process.platform) {
  const venvDir = path.join(cacheDir, `venv-${version}`);
  const binDir = platform === "win32" ? path.join(venvDir, "Scripts") : path.join(venvDir, "bin");
  const pythonBin = path.join(binDir, platform === "win32" ? "python.exe" : "python");
  const serverBin = path.join(binDir, platform === "win32" ? `${PKG_NAME}.exe` : PKG_NAME);
  const markerFile = path.join(venvDir, ".installed-spec");
  return { venvDir, binDir, pythonBin, serverBin, markerFile };
}

function ensureVenvInstalled({
  python,
  spec,
  version,
  cacheDir,
  platform = process.platform,
  runner = spawnSync,
  stderr = process.stderr,
}) {
  const paths = getVenvPaths(cacheDir, version, platform);
  const alreadyReady =
    fs.existsSync(paths.serverBin) &&
    fs.existsSync(paths.markerFile) &&
    fs.readFileSync(paths.markerFile, "utf8").trim() === spec;

  if (alreadyReady) {
    return paths.serverBin;
  }

  fs.mkdirSync(cacheDir, { recursive: true });
  if (!fs.existsSync(paths.pythonBin)) {
    stderr.write(`[${PKG_NAME}] Creating isolated Python ${python.version} environment in ${paths.venvDir}...\n`);
    const venvRes = runner(python.command, [...python.prefixArgs, "-m", "venv", paths.venvDir], {
      stdio: ["ignore", "pipe", "pipe"],
      encoding: "utf8",
      windowsHide: true,
    });
    if (venvRes.error || venvRes.status !== 0) {
      const detail = (venvRes.stderr || venvRes.stdout || String(venvRes.error || "")).trim();
      throw new Error(`Failed to create Python virtual environment: ${detail}`);
    }
  }

  stderr.write(`[${PKG_NAME}] Installing ${spec} (first run only)...\n`);
  const pipRes = runner(
    paths.pythonBin,
    ["-m", "pip", "install", "--disable-pip-version-check", "--quiet", spec],
    {
      stdio: ["ignore", "pipe", "pipe"],
      encoding: "utf8",
      windowsHide: true,
    }
  );
  if (pipRes.error || pipRes.status !== 0) {
    const detail = (pipRes.stderr || pipRes.stdout || String(pipRes.error || "")).trim();
    throw new Error(`Failed to install ${spec}: ${detail}`);
  }

  fs.writeFileSync(paths.markerFile, `${spec}\n`, "utf8");
  return paths.serverBin;
}

function buildMissingRuntimeMessage() {
  return [
    `[${PKG_NAME}] Could not find 'uvx' or a compatible Python runtime (Python 3.11 or 3.12).`,
    "",
    "Install uv (recommended — automatically manages Python 3.12):",
    '  Windows (PowerShell): powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"',
    "  macOS / Linux:        curl -LsSf https://astral.sh/uv/install.sh | sh",
    "",
    "Or install Python 3.12 from https://www.python.org/downloads/ and re-run.",
    "",
  ].join("\n");
}

function resolveLaunchPlan(
  userArgs,
  {
    env = process.env,
    platform = process.platform,
    homedir = os.homedir(),
    version = loadPackageVersion(),
    runner = spawnSync,
    existsSync = fs.existsSync,
    stderr = process.stderr,
  } = {}
) {
  const spec = getPackageSpec(env, version);

  const uvxCmd = findUvx({ env, platform, homedir, runner, existsSync });
  if (uvxCmd) {
    return {
      mode: "uvx",
      command: uvxCmd,
      args: ["--from", spec, PKG_NAME, ...userArgs],
    };
  }

  const python = findCompatiblePython({ platform, runner });
  if (python) {
    const cacheDir = getCacheDir(env, platform, homedir);
    const serverBin = ensureVenvInstalled({
      python,
      spec,
      version,
      cacheDir,
      platform,
      runner,
      stderr,
    });
    return {
      mode: "venv",
      command: serverBin,
      args: [...userArgs],
    };
  }

  return null;
}

function runLauncher(userArgs = process.argv.slice(2)) {
  let plan;
  try {
    plan = resolveLaunchPlan(userArgs);
  } catch (err) {
    process.stderr.write(`[${PKG_NAME}] ${err.message}\n`);
    process.exit(1);
  }

  if (!plan) {
    process.stderr.write(buildMissingRuntimeMessage());
    process.exit(1);
  }

  const child = spawn(plan.command, plan.args, {
    stdio: "inherit",
    windowsHide: true,
  });

  const forwardSignal = (signal) => {
    if (!child.killed) {
      child.kill(signal);
    }
  };
  process.on("SIGINT", () => forwardSignal("SIGINT"));
  process.on("SIGTERM", () => forwardSignal("SIGTERM"));

  child.on("error", (err) => {
    process.stderr.write(`[${PKG_NAME}] Failed to launch '${plan.command}': ${err.message}\n`);
    process.exit(1);
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 1);
  });
}

module.exports = {
  PKG_NAME,
  buildMissingRuntimeMessage,
  ensureVenvInstalled,
  findCompatiblePython,
  findUvx,
  getCacheDir,
  getPackageSpec,
  getVenvPaths,
  isSupportedPythonVersion,
  loadPackageVersion,
  resolveLaunchPlan,
  runLauncher,
};
