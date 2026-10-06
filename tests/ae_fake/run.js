"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const { createAE } = require("./ae");

let scriptPath;
try {
  const args = process.argv.slice(2);
  const flag = args.indexOf("--documents");
  let documents;
  if (flag !== -1) {
    if (!args[flag + 1]) throw Error("--documents requires a directory");
    documents = args[flag + 1];
    args.splice(flag, 2);
  }
  const [statePath, script, entry, ...entryArgs] = args;
  scriptPath = script;
  if (!statePath || !script || !entry) throw Error("usage: node run.js <state.json> <script.jsx> <entry> [args...] [--documents <dir>]");
  if (!/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(entry)) throw Error("entry must be a function name");
  let state = {};
  try { state = JSON.parse(fs.readFileSync(statePath, "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const ae = createAE({ state, documents });
  vm.runInContext(fs.readFileSync(script, "utf8"), ae.context, { filename: script });
  // Invoke in the VM as well, preserving its stack/line information. Args are data.
  const call = `${entry}(${entryArgs.map((arg) => JSON.stringify(arg)).join(",")})`;
  const result = vm.runInContext(call, ae.context, { filename: "fake-ae-entry" });
  if (typeof result !== "string") throw Error("entry must return a string");
  const temporary = `${statePath}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(ae.serialize(), null, 2) + "\n", "utf8");
    fs.renameSync(temporary, statePath);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
  process.stdout.write(JSON.stringify({ result, undo_groups: ae.counters.undoGroups, writes: ae.counters.writes }) + "\n");
} catch (error) {
  const stackLine = scriptPath && String(error?.stack || "").split("\n").find((line) => line.includes(`${scriptPath}:`));
  const location = stackLine && stackLine.slice(stackLine.indexOf(`${scriptPath}:`) + scriptPath.length + 1).match(/^\d+/);
  const line = Number(error?.line || location?.[0] || 0);
  process.stdout.write(JSON.stringify({ error: String(error?.message ?? error), line }) + "\n");
  process.exitCode = 1;
}
