// Minimal ExtendScript/After Effects host for running keepframe_panel.jsx under Node.
// It mimics the ES3 engine's gaps (no JSON) and the File/Folder/$/app/ScriptUI calls the
// panel makes, so the bridge protocol can be exercised without After Effects.
// Usage: node ae_panel_host.js <panel.jsx> <LOCALAPPDATA dir> <seconds>
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");

const [panelPath, localAppData, seconds] = process.argv.slice(2);

function File(p) {
  if (!(this instanceof File)) return new File(p);
  this.fsName = path.resolve(String(p));
  this.name = path.basename(this.fsName);
  this.encoding = "BINARY";
  this._mode = null;
  this._buf = "";
}
Object.defineProperty(File.prototype, "exists", {
  get() { try { return fs.statSync(this.fsName).isFile(); } catch (e) { return false; } },
});
File.prototype.open = function (mode) {
  this._mode = mode;
  if (mode === "r") {
    try { this._buf = fs.readFileSync(this.fsName, "utf8"); } catch (e) { return false; }
  } else {
    this._buf = "";
  }
  return true;
};
File.prototype.read = function () { return this._buf; };
File.prototype.write = function (text) { this._buf += String(text); return true; };
File.prototype.close = function () {
  if (this._mode === "w") fs.writeFileSync(this.fsName, this._buf, "utf8");
  this._mode = null;
  return true;
};
File.prototype.remove = function () { try { fs.unlinkSync(this.fsName); return true; } catch (e) { return false; } };
File.prototype.rename = function (name) { // ExtendScript refuses to overwrite
  const dest = path.join(path.dirname(this.fsName), String(name));
  if (fs.existsSync(dest)) return false;
  try { fs.renameSync(this.fsName, dest); } catch (e) { return false; }
  this.fsName = dest;
  this.name = path.basename(dest);
  return true;
};

function Folder(p) {
  if (!(this instanceof Folder)) return new Folder(p);
  this.fsName = path.resolve(String(p));
  this.name = path.basename(this.fsName);
}
Object.defineProperty(Folder.prototype, "exists", {
  get() { try { return fs.statSync(this.fsName).isDirectory(); } catch (e) { return false; } },
});
Folder.prototype.create = function () { fs.mkdirSync(this.fsName, { recursive: true }); return true; };
Folder.prototype.getFiles = function () {
  return fs.readdirSync(this.fsName).map((name) => {
    const full = path.join(this.fsName, name);
    return fs.statSync(full).isDirectory() ? new Folder(full) : new File(full);
  });
};
Folder.userData = new Folder(localAppData);

function Panel() {}
function Window() {}
Window.prototype.add = function (type, bounds, text) {
  let value = text;
  return { // statictext changes are echoed so a run shows what the user would see
    type, onClick: null,
    get text() { return value; },
    set text(next) { if (type === "statictext" && next !== value) process.stderr.write(`STATUS: ${next}\n`); value = next; },
  };
};
Window.prototype.show = function () { return true; };
Window.prototype.close = function () { if (this.onClose) this.onClose(); };

const tasks = [];
const sandbox = {
  File, Folder, Panel, Window,
  alert(message) { process.stderr.write(`ALERT: ${message}\n`); },
  app: {
    version: "24.1.0x12",
    project: {},
    scheduleTask(code) { tasks.push(String(code)); return tasks.length; },
    cancelTask() {},
  },
};
sandbox.$ = { global: sandbox, getenv: (name) => (name === "LOCALAPPDATA" ? localAppData : null), sleep() {} };
const context = vm.createContext(sandbox);
vm.runInContext("delete JSON;", context);
vm.runInContext(fs.readFileSync(panelPath, "utf8"), context, { filename: panelPath });

const seen = new Set();
const deadline = Date.now() + Number(seconds || 10) * 1000;
const timer = setInterval(() => {
  for (const code of tasks) {
    try {
      vm.runInContext(code, context);
    } catch (error) {
      const text = `TASK ERROR: ${error && error.message}`;
      if (!seen.has(text)) { seen.add(text); process.stderr.write(text + "\n"); }
    }
  }
  if (Date.now() > deadline) clearInterval(timer);
}, 50);
