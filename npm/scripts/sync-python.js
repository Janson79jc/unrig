"use strict";

const fs = require("node:fs");
const path = require("node:path");

const npmDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(npmDir, "..");
const srcUnrig = path.join(repoRoot, "src", "unrig");
const destUnrig = path.join(npmDir, "python", "unrig");

function copyFilteredDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name.endsWith(".pyc") || entry.name.endsWith(".pyo")) {
      continue;
    }
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyFilteredDir(s, d);
    } else if (entry.isFile()) {
      fs.copyFileSync(s, d);
    }
  }
}

if (!fs.existsSync(path.join(srcUnrig, "__init__.py"))) {
  throw new Error(`Expected Python source at ${srcUnrig}`);
}

fs.rmSync(path.join(npmDir, "python"), { recursive: true, force: true });
copyFilteredDir(srcUnrig, destUnrig);
fs.copyFileSync(path.join(repoRoot, "README.md"), path.join(npmDir, "README.md"));
fs.copyFileSync(path.join(repoRoot, "LICENSE"), path.join(npmDir, "LICENSE"));
process.stdout.write(`Synced unrig Python package into ${destUnrig}\n`);
