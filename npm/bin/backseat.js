#!/usr/bin/env node

"use strict";

const { spawnSync } = require("child_process");
const { platform } = require("process");

const PYPI_PACKAGE = "backseat";
const MIN_PYTHON_VERSION = [3, 10];

// ── Find Python ───────────────────────────────────────────────────────────────

function findPython() {
  const candidates =
    platform === "win32"
      ? ["python", "python3", "py"]
      : ["python3", "python"];

  for (const cmd of candidates) {
    try {
      const result = spawnSync(cmd, ["--version"], { encoding: "utf8" });
      if (result.status !== 0 || result.error) continue;

      const match = (result.stdout + result.stderr).match(/Python (\d+)\.(\d+)/);
      if (!match) continue;

      const major = parseInt(match[1]);
      const minor = parseInt(match[2]);
      if (
        major > MIN_PYTHON_VERSION[0] ||
        (major === MIN_PYTHON_VERSION[0] && minor >= MIN_PYTHON_VERSION[1])
      ) {
        return cmd;
      }
    } catch {
      // not found, try next
    }
  }
  return null;
}

// ── Check if backseat pip package is installed ────────────────────────────────

function isInstalled(python) {
  const result = spawnSync(
    python,
    ["-c", `import ${PYPI_PACKAGE}`],
    { encoding: "utf8" }
  );
  return result.status === 0 && !result.error;
}

// ── Install backseat via pip ──────────────────────────────────────────────────

function install(python) {
  console.log(`Installing ${PYPI_PACKAGE} via pip...`);
  const result = spawnSync(
    python,
    ["-m", "pip", "install", "--quiet", PYPI_PACKAGE],
    { stdio: "inherit" }
  );

  if (result.error || result.status !== 0) {
    console.error(`\nFailed to install ${PYPI_PACKAGE}.`);
    console.error(`Try manually: pip install ${PYPI_PACKAGE}`);
    process.exit(1);
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

const python = findPython();

if (!python) {
  console.error("Error: Python 3.10+ is required but was not found.");
  console.error("");
  console.error("Install Python from https://python.org and make sure it is in your PATH.");
  console.error(
    platform === "win32"
      ? "On Windows: check 'Add Python to PATH' during installation."
      : "On macOS: brew install python3"
  );
  process.exit(1);
}

if (!isInstalled(python)) {
  install(python);
}

// Forward all arguments to the Python CLI
const result = spawnSync(
  python,
  ["-m", PYPI_PACKAGE, ...process.argv.slice(2)],
  { stdio: "inherit" }
);

process.exit(result.status ?? 1);
