"use strict";

const assert = require("node:assert");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const npmDir = path.resolve(__dirname, "..");
const npmCmd = process.platform === "win32" ? "npm.cmd" : "npm";
const tarCmd = process.platform === "win32" ? "tar.exe" : "tar";

const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), "unrig-npm-test-"));
let tgzPath = "";

try {
  const packedName = (
    process.platform === "win32"
      ? execFileSync("cmd.exe", ["/c", "npm.cmd", "pack", "--silent"], { cwd: npmDir, encoding: "utf-8" })
      : execFileSync(npmCmd, ["pack", "--silent"], { cwd: npmDir, encoding: "utf-8" })
  )
    .trim()
    .split(/\r?\n/)
    .pop()
    .trim();
  tgzPath = path.join(npmDir, packedName);
  assert.ok(fs.existsSync(tgzPath), `Expected packed tarball at ${tgzPath}`);

  execFileSync(tarCmd, ["-xzf", tgzPath, "-C", tmpBase]);
  const packedPkg = JSON.parse(
    fs.readFileSync(path.join(tmpBase, "package", "package.json"), "utf8")
  );
  assert.strictEqual(packedPkg.name, "@janson79jc/unrig");
  assert.ok(
    packedPkg.bin && packedPkg.bin.unrig === "bin/unrig.js",
    `Expected bin.unrig === "bin/unrig.js" in packed package.json, got ${JSON.stringify(packedPkg.bin)}`
  );
  const binPath = path.join(tmpBase, "package", "bin", "unrig.js");
  assert.ok(fs.existsSync(binPath), "Missing bin/unrig.js in unpacked tarball");
  assert.ok(
    fs.existsSync(path.join(tmpBase, "package", "python", "unrig", "templates", "unrig.yaml")),
    "Missing bundled Python templates in unpacked tarball"
  );

  const ver = spawnSync(process.execPath, [binPath, "--version"], { encoding: "utf-8" });
  assert.strictEqual(ver.status, 0, ver.stderr);
  assert.match(ver.stdout, /unrig\s+0\.1\.0/);

  const rep = spawnSync(process.execPath, [binPath, "replay"], { encoding: "utf-8" });
  assert.strictEqual(rep.status, 0, rep.stdout + rep.stderr);
  assert.match(rep.stdout, /10 scenarios/);

  const hookRes = spawnSync(process.execPath, [binPath, "hook", "--root", tmpBase], {
    input: JSON.stringify({ tool_name: "Write", tool_input: { file_path: ".unrig.yaml" } }),
    encoding: "utf-8",
  });
  assert.strictEqual(hookRes.status, 2, hookRes.stderr);
  assert.match(hookRes.stderr, /UNRIG L0 HOOK BLOCKED/);

  process.stdout.write("[PASS] npm wrapper tarball verified (--version, replay, hook).\n");
} finally {
  if (tgzPath && fs.existsSync(tgzPath)) {
    fs.rmSync(tgzPath, { force: true });
  }
  fs.rmSync(tmpBase, { recursive: true, force: true });
}
