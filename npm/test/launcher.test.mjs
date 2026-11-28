import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import launcher from "../lib/launcher.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BIN_PATH = path.resolve(__dirname, "..", "bin", "pbi-mcp-server.js");

test("resolveLaunchPlan prefers uvx and pins version by default", () => {
  const runner = (cmd, args) => {
    if (cmd === "uvx" && args[0] === "--version") {
      return { status: 0, stdout: "uvx 0.6.0\n", stderr: "" };
    }
    return { status: 1, stdout: "", stderr: "not found" };
  };

  const plan = launcher.resolveLaunchPlan(["--workspace", "./proj.pbip"], {
    env: {},
    version: "0.3.0",
    runner,
  });

  assert.deepEqual(plan, {
    mode: "uvx",
    command: "uvx",
    args: ["--from", "pbi-mcp-server==0.3.0", "pbi-mcp-server", "--workspace", "./proj.pbip"],
  });
});

test("resolveLaunchPlan honors PBI_MCP_SPEC override", () => {
  const runner = (cmd) =>
    cmd === "uvx"
      ? { status: 0, stdout: "uvx 0.6.0\n", stderr: "" }
      : { status: 1, stdout: "", stderr: "" };

  const plan = launcher.resolveLaunchPlan(["--meta-tools"], {
    env: { PBI_MCP_SPEC: "git+https://github.com/jdestef90/PBI_MCP" },
    version: "0.3.0",
    runner,
  });

  assert.equal(plan.mode, "uvx");
  assert.deepEqual(plan.args, [
    "--from",
    "git+https://github.com/jdestef90/PBI_MCP",
    "pbi-mcp-server",
    "--meta-tools",
  ]);
});

test("findCompatiblePython accepts 3.11/3.12 and skips 3.10/3.13", () => {
  const runner = (cmd, args) => {
    if (cmd === "python3.12") return { status: 1, stdout: "", stderr: "" };
    if (cmd === "python3.11") return { status: 1, stdout: "", stderr: "" };
    if (cmd === "python3") return { status: 0, stdout: "3.13\n", stderr: "" };
    if (cmd === "python" && args[0] === "-c") return { status: 0, stdout: "3.12\n", stderr: "" };
    return { status: 1, stdout: "", stderr: "" };
  };

  const found = launcher.findCompatiblePython({ platform: "linux", runner });
  assert.deepEqual(found, { command: "python", prefixArgs: [], version: "3.12" });
});

test("resolveLaunchPlan falls back to cached venv when uvx is unavailable and reuses cache", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "pbi-mcp-npm-test-"));
  const pipCalls = [];
  const stderrChunks = [];
  const fakeStderr = { write: (s) => stderrChunks.push(s) };

  try {
    const runner = (cmd, args) => {
      if (cmd === "python3.12" && args[0] === "-c") {
        return { status: 0, stdout: "3.12\n", stderr: "" };
      }
      if (cmd === "python3.12" && args[0] === "-m" && args[1] === "venv") {
        const venvDir = args[2];
        const binDir = path.join(venvDir, "bin");
        fs.mkdirSync(binDir, { recursive: true });
        fs.writeFileSync(path.join(binDir, "python"), "");
        return { status: 0, stdout: "", stderr: "" };
      }
      if (cmd.endsWith(path.join("bin", "python")) && args[1] === "pip") {
        pipCalls.push(args);
        const binDir = path.dirname(cmd);
        fs.writeFileSync(path.join(binDir, "pbi-mcp-server"), "");
        return { status: 0, stdout: "Successfully installed", stderr: "" };
      }
      return { status: 1, stdout: "", stderr: "" };
    };

    const opts = {
      env: { PBI_MCP_NO_UVX: "1", PBI_MCP_CACHE_DIR: tmpDir },
      platform: "linux",
      version: "0.3.0",
      runner,
      stderr: fakeStderr,
    };

    const plan1 = launcher.resolveLaunchPlan(["--workspace", "a.pbip"], opts);
    assert.equal(plan1.mode, "venv");
    assert.equal(pipCalls.length, 1);
    assert.ok(plan1.command.endsWith(path.join("venv-0.3.0", "bin", "pbi-mcp-server")));
    assert.ok(stderrChunks.join("").includes("Installing pbi-mcp-server==0.3.0"));

    // Second run should hit cache with zero additional pip invocations
    const plan2 = launcher.resolveLaunchPlan(["--workspace", "b.pbip"], opts);
    assert.equal(plan2.mode, "venv");
    assert.equal(pipCalls.length, 1);
    assert.deepEqual(plan2.args, ["--workspace", "b.pbip"]);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("CLI writes missing-runtime guidance exclusively to stderr (keeps MCP stdout clean)", () => {
  const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pbi-mcp-empty-path-"));
  try {
    const res = spawnSync(process.execPath, [BIN_PATH, "--version"], {
      env: {
        PATH: emptyDir,
        HOME: emptyDir,
        USERPROFILE: emptyDir,
        PBI_MCP_NO_UVX: "1",
      },
      encoding: "utf8",
    });
    assert.equal(res.status, 1);
    assert.equal(res.stdout, "");
    assert.match(res.stderr, /Could not find 'uvx' or a compatible Python runtime/);
    assert.match(res.stderr, /astral\.sh\/uv/);
  } finally {
    fs.rmSync(emptyDir, { recursive: true, force: true });
  }
});
