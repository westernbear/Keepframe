"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { parseArgs } = require("node:util");
const core = require("../../extension/js/core");
const { createAE } = require("./ae");
const { checkES3Syntax } = require("./run");

function writeJSON(filename, value) {
  const temporary = `${filename}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(temporary, filename);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

async function main() {
  const results = [], statuses = [], evalTimers = new Set();
  let ae, runner, running, deadline, options = {}, credentials = {}, error;
  try {
    options = parseArgs({ options: {
      server: { type: "string" }, state: { type: "string" }, documents: { type: "string" },
      credentials: { type: "string" }, pair: { type: "string" },
      jobs: { type: "string", default: "1" }, timeout: { type: "string", default: "20" },
      "die-after-claim": { type: "boolean", default: false },
    } }).values;
    for (const name of ["server", "state", "documents", "credentials"])
      if (!options[name]) throw Error(`--${name} is required`);
    const jobs = Number(options.jobs), timeout = Number(options.timeout);
    const delay = Number(process.env.KEEPFRAME_TEST_EVAL_DELAY_MS || 0);
    if (!Number.isSafeInteger(jobs) || jobs < 0) throw Error("--jobs must be a nonnegative integer");
    if (!Number.isFinite(timeout) || timeout <= 0) throw Error("--timeout must be positive");
    if (!Number.isFinite(delay) || delay < 0) throw Error("invalid KEEPFRAME_TEST_EVAL_DELAY_MS");
    let state = {};
    try { state = JSON.parse(fs.readFileSync(options.state, "utf8")); }
    catch (e) { if (e.code !== "ENOENT") throw e; }
    ae = createAE({ state, documents: options.documents });
    const host = path.resolve(__dirname, "../../extension/host/keepframe.jsx");
    const source = fs.readFileSync(host, "utf8");
    checkES3Syntax(source);
    vm.runInContext(source, ae.context, { filename: host });

    let complete;
    const finished = new Promise((resolve, reject) => {
      complete = resolve;
      deadline = setTimeout(() => reject(Error(`timed out after ${timeout} s`)), timeout * 1000);
    });
    // Observe the real transport without changing its requests, responses or timing.
    const transport = module => ({ request(url, opts, callback) {
      const route = new URL(url).pathname;
      let body;
      const req = module.request(url, opts, res => {
        const chunks = [];
        if (route === "/api/ae/next") res.on("data", chunk => chunks.push(chunk));
        res.on("end", () => {
          if (options["die-after-claim"] && route === "/api/ae/next" && res.statusCode === 200
              && JSON.parse(Buffer.concat(chunks).toString()).job) {
            fs.writeFileSync(1, JSON.stringify({ results, statuses, writes: ae.counters.writes,
              undo_groups: ae.counters.undoGroups }) + "\n");
            process.exit(3);
          }
          if (opts.method === "POST" && route.endsWith("/result")
              && res.statusCode >= 200 && res.statusCode < 300) {
            results.push(JSON.parse(body));
            if (results.length >= jobs) setImmediate(complete);
          }
          // Even a pairing-only run announces itself, making the device connected.
          if (jobs === 0 && route === "/api/ae/info" && res.statusCode === 204) setImmediate(complete);
        });
        callback(res);
      });
      const end = req.end;
      req.end = function (data, ...args) { body = data; return end.call(this, data, ...args); };
      return req;
    } });
    const deps = {
      http: transport(require("node:http")), https: transport(require("node:https")),
      dns: require("node:dns"), fs, path, crypto: require("node:crypto"),
      os: { ...require("node:os"), tmpdir: () => ae.context.Folder.temp.fsName },
      documentsDir: options.documents, setTimeout, clearTimeout, now: Date.now,
      log() {}, setStatus: (message, details) => statuses.push({ message, details }),
      sleep(ms) {
        let timer;
        const sleeping = new Promise(resolve => { timer = setTimeout(resolve, ms); });
        sleeping.cancel = () => clearTimeout(timer);
        return sleeping;
      },
      evalScript(script, callback) {
        const timer = setTimeout(() => {
          evalTimers.delete(timer);
          let result;
          try {
            result = vm.runInContext(script, ae.context, { filename: "fake-cep-eval" });
            if (typeof result !== "string") throw Error("evalScript must return a string");
          } catch (e) { result = JSON.stringify({ ok: false, error: e.message, line: e.line || 0 }); }
          callback(result);
        }, /^(kfSync|kfRender)\(/.test(script) ? delay : 0);
        evalTimers.add(timer);
      },
    };
    // Attach the deadline before pairing as well, avoiding an unhandled rejection.
    await Promise.race([finished, (async () => {
      if (options.pair) {
        const paired = await core.pair({ serverUrl: options.server, code: options.pair }, deps);
        credentials = { serverUrl: paired.serverUrl, deviceId: paired.deviceId, token: paired.token };
        writeJSON(options.credentials, credentials);
      } else {
        credentials = JSON.parse(fs.readFileSync(options.credentials, "utf8"));
        if (!credentials || credentials.serverUrl !== core.validateServerUrl(options.server).url
            || typeof credentials.deviceId !== "string" || !credentials.deviceId
            || typeof credentials.token !== "string" || !credentials.token)
          throw Error("invalid credentials or server mismatch");
      }
      runner = core.createRunner(credentials, deps);
      running = runner.start();
      await Promise.race([finished, running.then(() => {
        if (results.length < jobs || jobs === 0) throw Error(statuses.at(-1)?.message || "runner stopped");
      })]);
    })()]);
    const failed = results.find(result => !result.ok);
    if (failed) throw Error(failed.error || "AE job failed");
  } catch (e) {
    error = core.redact(e.message || e, [credentials?.token, options.pair]);
  } finally {
    clearTimeout(deadline);
    if (runner) runner.stop();
    for (const timer of evalTimers) clearTimeout(timer);
    if (running) await running;
    if (ae) {
      try { writeJSON(options.state, ae.serialize()); }
      catch (e) { error = core.redact(e.message || e, [credentials?.token, options.pair]); }
    }
  }
  fs.writeFileSync(1, JSON.stringify({ results, statuses, writes: ae?.counters.writes || 0,
    undo_groups: ae?.counters.undoGroups || 0, ...(error ? { error } : {}) }) + "\n");
  process.exit(error ? 1 : 0);
}

if (require.main === module) main();
