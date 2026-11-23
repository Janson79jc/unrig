#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

function resolvePythonRoot() {
  const pkgPython = path.resolve(__dirname, "..", "python");
  if (fs.existsSync(path.join(pkgPython, "unrig", "__init__.py"))) {
    return pkgPython;
  }
  const repoSrc = path.resolve(__dirname, "..", "..", "src");
  if (fs.existsSync(path.join(repoSrc, "unrig", "__init__.py"))) {
    return repoSrc;
  }
  return "";
}

function findPythonInterpreter() {
  const candidates = [];
  if (process.env.UNRIG_PYTHON && process.env.UNRIG_PYTHON.trim()) {
    candidates.push([process.env.UNRIG_PYTHON.trim(), []]);
  }
  candidates.push(["python3", []], ["python", []]);
  if (process.platform === "win32") {
    candidates.push(["py", ["-3"]]);
  }

  for (const [cmd, prefixArgs] of candidates) {
    const probe = spawnSync(
      cmd,
      [...prefixArgs, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
      { stdio: "ignore", windowsHide: true }
    );
    if (!probe.error && probe.status === 0) {
      return [cmd, prefixArgs];
    }
  }
  return null;
}

function main() {
  const pythonRoot = resolvePythonRoot();
  if (!pythonRoot) {
    process.stderr.write("[unrig] Bundled Python engine not found alongside npm wrapper.\n");
    process.exit(1);
  }

  const interp = findPythonInterpreter();
  if (!interp) {
    process.stderr.write(
      "[unrig] Python >= 3.10 is required on PATH to run unrig (zero third-party pip deps needed).\n"
    );
    process.exit(1);
  }

  const [pyCmd, pyArgs] = interp;
  const env = Object.assign({}, process.env);
  env.PYTHONUTF8 = "1";
  env.PYTHONPATH = env.PYTHONPATH
    ? pythonRoot + path.delimiter + env.PYTHONPATH
    : pythonRoot;

  const res = spawnSync(
    pyCmd,
    [...pyArgs, "-m", "unrig.cli", ...process.argv.slice(2)],
    { stdio: "inherit", env, windowsHide: true }
  );

  if (res.error) {
    process.stderr.write(`[unrig] Failed to launch '${pyCmd}': ${res.error.message}\n`);
    process.exit(1);
  }
  process.exit(res.status === null ? 1 : res.status);
}

main();
