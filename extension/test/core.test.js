'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const vm = require('node:vm');
const core = require('../js/core');

const TOKEN = 'private-device-token-123456';
const CODE = 'KF-ABCD-1234';
const bytes = Buffer.from('test asset bytes');
const sha = crypto.createHash('sha256').update(bytes).digest('hex');
const info = {ok: true, ae_version: '25.0', project_name: '한글 Project.aep',
    project_saved: true, fonts: [{family: 'Arial', style: 'Regular', postscript: 'ArialMT'}]};
const job = {id: 'j1', kind: 'sync', project: 'demo', scene: 's1', version: 'v7', params: {force: true}};
const spec = {schema: 'keepframe.ae-comp/1', project: 'demo', scene: 's1', version: 'v7',
    assets: [{name: 'a.png', sha256: sha}], layers: []};
const success = {ok: true, applied: true, created: ['kf:a', 'kf:b'], updated: ['kf:c'],
    deleted: [], warnings: ['test warning'], keys: {'kf:a': 4}};
const never = () => new Promise(() => {});

async function fixture(t, route) {
    const requests = [], statuses = [], logs = [], scripts = [];
    const documentsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'keepframe-panel-'));
    const server = http.createServer(async (req, res) => {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const raw = Buffer.concat(chunks).toString();
        const request = {method: req.method, path: req.url, headers: req.headers,
            body: raw ? JSON.parse(raw) : undefined};
        requests.push(request);
        const reply = (status, body, headers) => {
            res.writeHead(status, headers || {'Content-Type': 'application/json'});
            res.end(Buffer.isBuffer(body) ? body : body === undefined ? '' :
                typeof body === 'string' ? body : JSON.stringify(body));
        };
        if (route && await route(request, reply, res)) return;
        if (req.url === '/api/ae/info') reply(204);
        else if (req.url === '/api/ae/pair') reply(200, {device_id: 'd1', token: TOKEN, server_name: 'studio'});
        else if (req.url.endsWith('/spec')) reply(200, spec);
        else if (req.url.includes('/assets/')) reply(200, bytes, {'X-Keepframe-Sha256': sha});
        else if (req.url.endsWith('/progress')) reply(204);
        else if (req.url.endsWith('/result')) reply(200, {job: {state: 'done'}});
        else reply(401, {error: 'not paired'});
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    t.after(async () => {
        await new Promise(resolve => server.close(resolve));
        fs.rmSync(documentsDir, {recursive: true, force: true});
    });
    const deps = {http, https, fs, path, crypto, os, documentsDir, sleep: never, now: () => 0,
        log: message => logs.push(message), setStatus: (message, details) => statuses.push({message, details}),
        evalScript: (script, callback) => {
            scripts.push(script);
            callback(JSON.stringify(script === 'kfInfo()' ? info : success));
        }};
    return {serverUrl: 'http://127.0.0.1:' + server.address().port,
        requests, statuses, logs, scripts, documentsDir, deps};
}

function runner(f, overrides) {
    return core.createRunner(Object.assign({serverUrl: f.serverUrl, token: TOKEN}, overrides), f.deps);
}

async function oneJob(t, options) {
    options = options || {};
    let polls = 0;
    const f = await fixture(t, (req, reply, res) => {
        if (req.path.startsWith('/api/ae/next') && polls++ === 0) {
            reply(200, {job: Object.assign({}, job, options.job)}); return true;
        }
        if (options.route) return options.route(req, reply, res);
    });
    if (options.deps) Object.assign(f.deps, options.deps);
    if (options.prepare) await options.prepare(f);
    await runner(f).start();
    f.result = f.requests.find(req => req.path.endsWith('/result')).body;
    return f;
}

function assertPrivate(f, extra) {
    const text = JSON.stringify({logs: f.logs, statuses: f.statuses, extra});
    assert.equal(text.includes(TOKEN), false);
    assert.equal(text.includes(CODE), false);
}

test('URL policy table and normalization', () => {
    const allowed = ['https://public.example', 'https://100.63.0.1/', 'http://localhost:8080',
        'http://127.0.0.1', 'http://127.255.255.255', 'http://[::1]:8000',
        'http://100.64.0.0', 'http://100.127.255.255', 'http://studio.tail.ts.net', 'HTTP://LOCALHOST/'];
    const denied = ['http://public.example', 'http://192.168.1.2', 'http://100.63.255.255',
        'http://100.128.0.0', 'http://[::2]', 'http://ts.net', 'http://x.ts.net.evil.test',
        'ftp://localhost', 'https://user:pass@public.example', 'https://user@public.example',
        'https://public.example/path', 'https://public.example/?x=1', 'https://public.example/#x',
        'https://public.example/../', 'https://public.example/?', 'https://public.example/#',
        'http://localhost\\evil.test', '', null];
    for (const url of allowed) assert.equal(core.validateServerUrl(url).ok, true, url);
    for (const url of denied) {
        const result = core.validateServerUrl(url);
        assert.equal(result.ok, false, String(url));
        assert.equal(typeof result.error, 'string');
    }
    assert.deepEqual(core.validateServerUrl(' https://PUBLIC.example/ '), {ok: true, url: 'https://public.example'});
});

test('manifest bundle and extension versions equal EXTENSION_VERSION', () => {
    const xml = fs.readFileSync(path.join(__dirname, '../CSXS/manifest.xml'), 'utf8');
    assert.equal(core.EXTENSION_VERSION, '1.0.0');
    assert.equal(xml.match(/ExtensionBundleVersion="([^"]+)"/)[1], core.EXTENSION_VERSION);
    assert.equal(xml.match(/<Extension Id="com.keepframe.ae.panel" Version="([^"]+)"/)[1], core.EXTENSION_VERSION);
    const browser = {window: {}, module: {exports: {}}}; // CEP mixed context also exposes Node globals.
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/core.js'), 'utf8'), browser);
    assert.equal(browser.window.KeepframeCore.EXTENSION_VERSION, core.EXTENSION_VERSION);
});

test('pair sends exact headers and info from kfInfo without bearer', async t => {
    const f = await fixture(t);
    const paired = await core.pair({serverUrl: f.serverUrl + '/', code: CODE}, f.deps);
    assert.deepEqual(paired, {serverUrl: f.serverUrl, deviceId: 'd1', token: TOKEN, serverName: 'studio'});
    const request = f.requests[0];
    assert.equal(request.path, '/api/ae/pair');
    assert.equal(request.method, 'POST');
    assert.equal(request.headers['x-keepframe-extension'], '1.0.0');
    assert.equal(request.headers.authorization, undefined);
    assert.equal(request.headers['content-type'], 'application/json');
    assert.deepEqual(request.body, {code: CODE, info: {ae_version: info.ae_version,
        extension_version: '1.0.0', os: os.platform() + ' ' + os.release(), fonts: info.fonts}});
    assert.deepEqual(f.scripts, ['kfInfo()']);
    assertPrivate(f);
});

test('pair maps errors and never exposes credentials', async t => {
    for (const status of [400, 401, 426, 500]) {
        const f = await fixture(t, (req, reply) => {
            reply(status, {error: 'bad ' + CODE + ' ' + TOKEN}); return true;
        });
        let error;
        try { await core.pair({serverUrl: f.serverUrl, code: CODE}, f.deps); }
        catch (e) { error = e; }
        assert.ok(error);
        if (status === 401) assert.match(error.message, /Pairing code is invalid or expired/);
        if (status === 426) assert.ok(error.message.includes('Update the Keepframe extension: ' + f.serverUrl + '/ae/keepframe.zxp'));
        assertPrivate(f, error.message);
    }
});

test('backoff sequence caps at 30 seconds and resets after success', async t => {
    let calls = 0;
    const f = await fixture(t, (req, reply, res) => {
        if (!req.path.startsWith('/api/ae/next')) return;
        calls++;
        if (calls === 8) reply(204);
        else if (calls === 10) reply(401);
        else res.destroy();
        return true;
    });
    const sleeps = [];
    f.deps.sleep = ms => { sleeps.push(ms); return Promise.resolve(); };
    await runner(f).start();
    assert.deepEqual(sleeps, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 1000]);
    assert.ok(f.statuses.some(s => /Not connected: .*\(retrying in 30 s\)/.test(s.message)));
    assert.ok(f.statuses.some(s => s.message === 'Connected · 127.0.0.1:' + new URL(f.serverUrl).port));
    const poll = f.requests.find(req => req.path.startsWith('/api/ae/next'));
    assert.equal(poll.path, '/api/ae/next?wait=25');
    assert.equal(poll.headers['x-keepframe-project'], encodeURIComponent(info.project_name));
    assert.equal(poll.headers['x-keepframe-project-saved'], '1');
    assert.equal(poll.headers.authorization, 'Bearer ' + TOKEN);
    assert.deepEqual(f.requests[0].body.info, {ae_version: info.ae_version, extension_version: '1.0.0',
        os: os.platform() + ' ' + os.release(), fonts: info.fonts});
    assertPrivate(f);
});

test('401 and 426 stop at either info or next with actionable status', async t => {
    for (const route of ['/api/ae/info', '/api/ae/next?wait=25']) {
        for (const status of [401, 426]) {
            const f = await fixture(t, (req, reply) => {
                if (req.path !== route) return;
                reply(status, {error: TOKEN}); return true;
            });
            await runner(f).start();
            assert.equal(f.statuses[f.statuses.length - 1].message, status === 401 ?
                'Not paired: enter a new code from the Keepframe web page' :
                'Update the Keepframe extension: ' + f.serverUrl + '/ae/keepframe.zxp');
            assert.equal(f.requests.length, route.includes('/info') ? 1 : 2);
            assertPrivate(f);
        }
    }
});

test('test_asset_cache_paths_stay_inside_folder', async t => {
    const bad = [{project: '../x'}, {assets: [{name: '../../a.png', sha256: sha}]},
        {assets: [{name: '..\\a.png', sha256: sha}]}, {assets: [{name: '/a.png', sha256: sha}]},
        {assets: [{name: 'a.png', sha256: sha.toUpperCase()}]}, {assets: [{name: 'a.png', sha256: 'abc'}]},
        {assets: [{name: 'a.exe', sha256: sha}]}];
    for (const change of bad) {
        const f = await oneJob(t, {route: (req, reply) => {
            if (req.path.endsWith('/spec')) { reply(200, Object.assign({}, spec, change)); return true; }
        }});
        assert.equal(f.result.ok, false);
        assert.match(f.result.error, /Invalid (project|asset)/);
        assert.deepEqual(fs.readdirSync(f.documentsDir), []);
        assert.equal(f.requests.some(req => req.path.includes('/assets/')), false);
    }
    for (const p of [path, path.win32]) {
        const result = core.assetCachePath('C:/Documents', 'demo', spec.assets[0], p);
        assert.equal(p.dirname(result), p.resolve('C:/Documents', 'Keepframe', 'demo', 'assets'));
    }
});

test('cached matching asset skips download; evalScript string round-trips data and force', async t => {
    let polls = 0;
    const special = Object.assign({}, spec, {notes: '"\\\n);evil(); // 한글\u2028\u2029'});
    const specText = JSON.stringify(special, null, 2);
    const f = await fixture(t, (req, reply) => {
        if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
        if (req.path.endsWith('/spec')) { reply(200, specText); return true; }
    });
    const cache = core.assetCachePath(f.documentsDir, 'demo', spec.assets[0], path);
    fs.mkdirSync(path.dirname(cache), {recursive: true});
    fs.writeFileSync(cache, bytes);
    await runner(f).start();
    assert.equal(f.requests.some(req => req.path.includes('/assets/')), false);
    let args;
    vm.runInNewContext(f.scripts.find(s => s.startsWith('kfSync(')), {kfSync: (...values) => { args = values; }});
    assert.equal(args[0], specText);
    assert.deepEqual(JSON.parse(args[1]), {'a.png': cache});
    assert.equal(args[2], 'true');
    assert.deepEqual(f.requests.find(req => req.path.endsWith('/result')).body, {ok: true, result: success});
    assert.ok(f.statuses.some(s => s.message === 'Synced v7: 2 created, 1 updated, 0 deleted'));
    assertPrivate(f);
});

test('download verifies bytes and header, cleans temporary files on mismatch', async t => {
    for (const mode of ['ok', 'bytes', 'header', 'missing']) {
        const f = await oneJob(t, {job: {params: {force: false}}, route: (req, reply) => {
            if (!req.path.includes('/assets/')) return;
            reply(200, mode === 'bytes' ? Buffer.from('wrong') : bytes,
                mode === 'missing' ? {} : {'X-Keepframe-Sha256': mode === 'header' ? '0'.repeat(64) : sha});
            return true;
        }});
        const folder = path.join(f.documentsDir, 'Keepframe/demo/assets');
        assert.equal(fs.readdirSync(folder).some(name => name.includes('.part')), false);
        if (mode === 'ok') {
            assert.equal(f.result.ok, true);
            assert.deepEqual(fs.readFileSync(path.join(folder, sha + '.png')), bytes);
            let force;
            vm.runInNewContext(f.scripts.find(s => s.startsWith('kfSync(')), {kfSync: (s, a, value) => { force = value; }});
            assert.equal(force, 'false');
        } else {
            assert.equal(f.result.ok, false);
            assert.match(f.result.error, /SHA-256 mismatch/);
            assert.deepEqual(fs.readdirSync(folder), []);
        }
    }
});

test('applied:false posts full successful result and surfaces hand edits', async t => {
    const result = {ok: true, applied: false, hand_edited: ['kf:a', 'kf:title']};
    const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(JSON.stringify(script === 'kfInfo()' ? info : result))}});
    assert.deepEqual(f.result, {ok: true, result});
    assert.ok(f.statuses.some(s => s.message ===
        'AE layers were edited by hand: kf:a, kf:title — overwrite from the web page'));
});

test('unparseable and failed evalScript results fail jobs with safe raw output and line', async t => {
    for (const raw of ['EvalScript error. ' + CODE + ' ' + TOKEN + 'x'.repeat(500),
        JSON.stringify({ok: false, error: 'AE failed ' + TOKEN, line: 42}), 'null', '{}']) {
        const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(script === 'kfInfo()' ? JSON.stringify(info) : raw)}});
        assert.equal(f.result.ok, false);
        assertPrivate(f, f.result);
        if (raw.startsWith('EvalScript')) {
            assert.match(f.result.error, /EvalScript error\./);
            assert.ok(f.result.error.length <= 350);
        }
        if (raw.startsWith('{"ok":false')) assert.equal(f.result.line, 42);
    }
});

test('unsupported job kinds post readable failures', async t => {
    for (const kind of ['render_frames', 'render_final', 'package']) {
        const f = await oneJob(t, {job: {kind}});
        assert.deepEqual(f.result, {ok: false, error: kind + ' is not supported by this extension version'});
        assert.equal(f.requests.some(req => req.path.endsWith('/spec')), false);
    }
});

async function until(check) {
    for (let i = 0; i < 1000; i++) {
        if (check()) return;
        await new Promise(resolve => setTimeout(resolve, 1));
    }
    assert.fail('condition never became true');
}

test('heartbeats continue every 15 seconds during a fake slow evalScript', async t => {
    let polls = 0, time = 0, finishSync;
    const waits = [], progress = [];
    const f = await fixture(t, (req, reply) => {
        if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
        if (req.path.endsWith('/progress')) { progress.push({time, body: req.body}); reply(204); return true; }
    });
    f.deps.now = () => time;
    f.deps.sleep = ms => new Promise(resolve => waits.push({at: time + ms, resolve}));
    f.deps.evalScript = (script, cb) => {
        if (script === 'kfInfo()') cb(JSON.stringify(info)); else finishSync = cb;
    };
    const running = runner(f).start();
    await until(() => finishSync && waits.length);
    for (let i = 0; i < 6; i++) {
        time += 15000;
        const due = waits.splice(0).filter(wait => wait.at <= time);
        assert.ok(due.length);
        due.forEach(wait => wait.resolve());
        await until(() => progress.some(p => p.time === time) && waits.length);
    }
    finishSync(JSON.stringify(success));
    await running;
    assert.ok(progress.length >= 7);
    for (let i = 1; i < progress.length; i++) assert.ok(progress[i].time - progress[i - 1].time <= 15000);
    assert.ok(progress.filter(p => p.time > 0).every(p => p.body.stage === 'syncing'));
    assert.equal(f.requests.find(req => req.path.endsWith('/result')).body.ok, true);
});

test('stop aborts long poll and prevents retry', async t => {
    let polling = false;
    const f = await fixture(t, req => {
        if (req.path.startsWith('/api/ae/next')) { polling = true; return true; }
    });
    const r = runner(f);
    const running = r.start();
    await until(() => polling);
    r.stop();
    await running;
    assert.equal(f.requests.length, 2);
});

test('200-entry ring log timestamps and redacts secrets', () => {
    const log = core.createLog(() => 0, [TOKEN, CODE]);
    for (let i = 0; i < 205; i++) log.add('entry ' + i + ' ' + TOKEN + ' ' + CODE);
    assert.equal(log.entries.length, 200);
    assert.match(log.entries[0], /^1970-01-01T00:00:00.000Z entry 5 /);
    assert.equal(log.text().includes(TOKEN), false);
    assert.equal(log.text().includes(CODE), false);
});

test('corrupt cache is replaced; interrupted downloads remove their temporary file', async t => {
    const cached = await oneJob(t, {prepare: f => {
        const file = core.assetCachePath(f.documentsDir, 'demo', spec.assets[0], path);
        fs.mkdirSync(path.dirname(file), {recursive: true});
        fs.writeFileSync(file, 'corrupt');
    }});
    assert.equal(cached.requests.filter(req => req.path.includes('/assets/')).length, 1);
    assert.equal(cached.result.ok, true);
    assert.deepEqual(fs.readFileSync(core.assetCachePath(cached.documentsDir, 'demo', spec.assets[0], path)), bytes);
    const interrupted = await oneJob(t, {route: (req, reply, res) => {
        if (!req.path.includes('/assets/')) return;
        res.writeHead(200, {'Content-Length': 100, 'X-Keepframe-Sha256': sha});
        res.end('partial'); return true;
    }});
    assert.equal(interrupted.result.ok, false);
    assert.deepEqual(fs.readdirSync(path.join(interrupted.documentsDir, 'Keepframe/demo/assets')), []);
});

test('result post retries retain the full result after network loss', async t => {
    let posts = 0;
    const sleeps = [];
    const f = await oneJob(t, {deps: {sleep: ms => {
        if (ms === 15000) return never();
        sleeps.push(ms); return Promise.resolve();
    }}, route: (req, reply, res) => {
        if (req.path.endsWith('/result') && posts++ === 0) { res.destroy(); return true; }
    }});
    const results = f.requests.filter(req => req.path.endsWith('/result'));
    assert.equal(results.length, 2);
    assert.deepEqual(results[0].body, {ok: true, result: success});
    assert.deepEqual(results[1].body, results[0].body);
    assert.deepEqual(sleeps, [1000]);
    assertPrivate(f);
});

test('requests select HTTPS and set 30 second / 35 second timeouts', async t => {
    const f = await fixture(t);
    const timeouts = [];
    function transport(url, options, callback) {
        assert.equal(typeof url, 'string');
        url = new URL(url);
        const req = http.request(f.serverUrl + url.pathname + url.search, options, callback);
        const original = req.setTimeout.bind(req);
        req.setTimeout = (ms, cb) => { timeouts.push({route: url.pathname, ms}); return original(ms, cb); };
        return req;
    }
    f.deps.https = {request: transport};
    f.deps.http = {request: () => assert.fail('HTTPS must use https transport')};
    await core.pair({serverUrl: 'https://studio.example', code: CODE}, f.deps);
    await runner(f, {serverUrl: 'https://studio.example'}).start();
    assert.deepEqual(timeouts, [{route: '/api/ae/pair', ms: 30000},
        {route: '/api/ae/info', ms: 30000}, {route: '/api/ae/next', ms: 35000}]);
});

test('401/426 during a slow evalScript stops the runner without waiting for AE', async t => {
    for (const status of [401, 426]) {
        let polls = 0, finishSync, completed = false;
        const waits = [];
        const f = await fixture(t, (req, reply) => {
            if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
            if (req.path.endsWith('/progress') && finishSync) { reply(status); return true; }
        });
        f.deps.sleep = () => new Promise(resolve => waits.push(resolve));
        f.deps.evalScript = (script, cb) => {
            if (script === 'kfInfo()') cb(JSON.stringify(info)); else finishSync = cb;
        };
        const running = runner(f).start().then(() => { completed = true; });
        await until(() => finishSync && waits.length);
        waits.shift()();
        await until(() => completed);
        await running;
        assert.ok(f.requests.some(req => req.path.endsWith('/result')));
        assert.equal(f.statuses[f.statuses.length - 1].message, status === 401 ?
            'Not paired: enter a new code from the Keepframe web page' :
            'Update the Keepframe extension: ' + f.serverUrl + '/ae/keepframe.zxp');
        finishSync(JSON.stringify(success));
    }
});

function panelHarness(locale, coreOverrides) {
    const nodes = {};
    for (const id of ['version', 'current-job', 'status', 'log', 'update-link', 'pair', 'disconnect',
        'settings', 'server-url', 'pairing-code', 'copy-log', 'copy-notice']) {
        nodes[id] = {value: '', textContent: '', handlers: {}, addEventListener: function (name, handler) {
            this.handlers[name] = handler;
        }, focus: () => {}, select: () => {}};
    }
    const storage = {serverUrl: 'https://studio.example.ts.net', deviceId: 'd1', token: TOKEN};
    const runs = [], pairs = [];
    const glueCore = Object.assign({}, core, {
        createRunner: (options, deps) => {
            const record = {options, deps, stopped: false}; runs.push(record);
            return {start: () => Promise.resolve(), stop: () => { record.stopped = true; }};
        },
        pair: async options => { pairs.push(options); return {serverUrl: storage.serverUrl, deviceId: 'd2', token: TOKEN}; }
    }, coreOverrides);
    const browser = {document: {documentElement: {}, getElementById: id => nodes[id],
        querySelectorAll: () => [], execCommand: () => true},
        window: {KeepframeCore: glueCore, __adobe_cep__: {getHostEnvironment: () => JSON.stringify({appUILocale: locale})},
            addEventListener: () => {}}, require, URL, setTimeout, clearTimeout,
        localStorage: {getItem: key => storage[key], setItem: (key, value) => { storage[key] = value; },
            removeItem: key => { delete storage[key]; }}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/panel.js'), 'utf8'), browser);
    return {nodes, storage, runs, pairs, browser};
}

test('panel glue restores pairing, translates status, pairs, forgets credentials and copies log', async () => {
    for (const locale of ['en_US', 'ko_KR']) {
        const p = panelHarness(locale);
        assert.equal(p.runs.length, 1);
        assert.equal(p.runs[0].options.token, TOKEN);
        assert.equal(p.browser.document.documentElement.lang, locale.startsWith('ko') ? 'ko' : 'en');
        p.runs[0].deps.setStatus('Connected · studio.example.ts.net');
        assert.equal(p.nodes.status.textContent, locale.startsWith('ko') ?
            '연결됨 · studio.example.ts.net' : 'Connected · studio.example.ts.net');
        p.nodes.disconnect.handlers.click();
        assert.equal(p.storage.token, undefined);
        assert.equal(p.storage.deviceId, undefined);
        assert.equal(p.runs[0].stopped, true);
        p.nodes['pairing-code'].value = CODE;
        await p.nodes.settings.handlers.submit({preventDefault: () => {}});
        assert.equal(p.pairs[0].code, CODE);
        assert.equal(p.storage.deviceId, 'd2');
        assert.equal(p.storage.token, TOKEN);
        assert.equal(p.nodes['pairing-code'].value, '');
        assert.equal(p.runs.length, 2);
        const url = p.storage.serverUrl + '/ae/keepframe.zxp';
        p.runs[1].deps.setStatus('Update the Keepframe extension: ' + url, {downloadUrl: url});
        assert.equal(p.nodes['update-link'].href, url);
        assert.equal(p.nodes['update-link'].hidden, false);
        assert.equal(p.nodes.status.textContent, (locale.startsWith('ko') ?
            'Keepframe 확장을 업데이트하세요: ' : 'Update the Keepframe extension: ') + url);
        p.nodes['copy-log'].handlers.click();
        assert.equal(p.nodes['copy-notice'].textContent, locale.startsWith('ko') ? '로그 복사됨' : 'Log copied');
        assert.equal(p.nodes.log.value.includes(TOKEN), false);
        assert.equal(p.nodes.log.value.includes(CODE), false);
    }
});

test('disconnect then pair waits for an already running AE call to finish', async () => {
    let finish, paired = false;
    const pending = new Promise(resolve => { finish = resolve; });
    const p = panelHarness('en_US', {
        createRunner: () => ({start: () => pending, stop: () => {}}),
        pair: async () => { paired = true; return {serverUrl: 'https://studio.example.ts.net', deviceId: 'd2', token: TOKEN}; }
    });
    p.nodes.disconnect.handlers.click();
    p.nodes['pairing-code'].value = CODE;
    const pairing = p.nodes.settings.handlers.submit({preventDefault: () => {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(paired, false);
    finish();
    await pairing;
    assert.equal(paired, true);
});
