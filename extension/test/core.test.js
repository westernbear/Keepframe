'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const https = require('node:https');
const dns = require('node:dns');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const vm = require('node:vm');
const core = require('../js/core');

const TOKEN = 'private-device-token-123456';
const CODE = 'KF-ABCD-1234';
const bytes = Buffer.from('test asset bytes');
const pngBytes = Buffer.from('89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c63606060000000040001f61738550000000049454e44ae426082', 'hex');
const sha = crypto.createHash('sha256').update(bytes).digest('hex');
const info = {ok: true, host_build: 'dev', ae_version: '25.0', project_name: '한글 Project.aep',
    project_saved: true, fonts: [{family: 'Arial', style: 'Regular', postscript: 'ArialMT'}]};
const job = {id: 'j1', kind: 'sync', project: 'demo', scene: 's1', version: 'v7', params: {force: true}};
const spec = {schema: 'keepframe.ae-comp/1', project: 'demo', scene: 's1', version: 'v7',
    assets: [{name: 'a.png', sha256: sha, bytes: bytes.length}], layers: []};
const success = {ok: true, applied: true, created: ['kf:a', 'kf:b'], updated: ['kf:c'],
    deleted: [], warnings: ['test warning'], keys: {'kf:a': 4}};
const never = () => new Promise(() => {});

test('sync timings are logged once and stay out of the panel status', async t => {
    const timings = {validate: 1, read: 2, hash: 3, assets: 4, write: 5, order: 6, total: 21};
    const f = await oneJob(t, {prepare: f => {
        f.deps.evalScript = (script, callback) => callback(JSON.stringify(
            script.startsWith('kfInfo(') ? info : Object.assign({}, success, {timings})));
    }});
    assert.deepEqual(f.result.result.timings, timings);
    assert.deepEqual(f.logs.filter(message => message.startsWith('Sync timings:')),
        ['Sync timings: validate 1 ms, read 2 ms, hash 3 ms, assets 4 ms, write 5 ms, order 6 ms, total 21 ms']);
    assert.equal(f.statuses.some(status => status.message.includes('timings')), false);
});

async function fixture(t, route) {
    const requests = [], statuses = [], logs = [], scripts = [];
    const documentsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'keepframe-panel-'));
    const server = http.createServer(async (req, res) => {
        const chunks = [];
        try { for await (const chunk of req) chunks.push(chunk); }
        catch (error) { if (req.aborted) return; throw error; }
        const data = Buffer.concat(chunks), raw = data.toString();
        const request = {method: req.method, path: req.url, headers: req.headers, raw, data,
            body: raw && req.headers['content-type'] === 'application/json' ? JSON.parse(raw) : undefined};
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
    const deps = {http, https, dns, fs, path, crypto, os, documentsDir, sleep: never, now: () => 0,
        setTimeout, clearTimeout,
        log: message => logs.push(message), setStatus: (message, details) => statuses.push({message, details}),
        evalScript: (script, callback) => {
            scripts.push(script);
            callback(JSON.stringify(script.startsWith('kfInfo(') ? info : success));
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
    assert.equal(core.HOST_BUILD, 'dev');
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
        extension_version: '1.0.0', host_build: 'dev', panel_build: 'dev',
        os: os.platform() + ' ' + os.release(), fonts: info.fonts}});
    assert.deepEqual(f.scripts, ['kfInfo("true")']);
    assertPrivate(f);
});

test('HTTP rejects a ts.net host resolving publicly before sending credentials', async t => {
    const f = await fixture(t);
    let lookups = 0, attempts = 0, r;
    f.deps.dns = {lookup: (host, options, cb) => { lookups++; cb(null, '203.0.113.8', 4); }};
    f.deps.http = {request: (url, options, cb) => {
        attempts++;
        return http.request(f.serverUrl + new URL(url).pathname, options, cb);
    }};
    f.deps.sleep = () => { r.stop(); return Promise.resolve(); };
    r = runner(f, {serverUrl: 'http://studio.tail.ts.net'});
    await r.start();
    assert.equal(attempts, 0, 'must not create a request carrying the bearer token');
    assert.equal(lookups, 1);
    assert.equal(f.requests.length, 0);
    assert.ok(f.statuses.some(s => s.message.startsWith(
        'Not connected: http is only allowed to this machine or your Tailscale network (got 203.0.113.8)')));
    await assert.rejects(core.pair({serverUrl: 'http://studio.tail.ts.net', code: CODE}, f.deps),
        {message: 'Not connected: http is only allowed to this machine or your Tailscale network (got 203.0.113.8)'});
    assert.equal(attempts, 0, 'pairing code must not be sent either');
    assertPrivate(f);
});

test('every HTTP request is pinned to its allowed DNS address with the original Host', async t => {
    const f = await fixture(t);
    const addresses = ['100.64.1.2', '::1', 'fd7a:115c:a1e0::42', '127.5.6.7'];
    const lookups = [], connections = [];
    f.deps.dns = {lookup: (host, options, cb) => {
        lookups.push(host);
        const address = addresses[(lookups.length - 1) % addresses.length];
        cb(null, address, address.includes(':') ? 6 : 4);
    }};
    f.deps.http = {request: (url, options, cb) => {
        connections.push({url: new URL(url), headers: options.headers});
        return http.request(f.serverUrl + new URL(url).pathname, options, cb);
    }};
    for (let i = 0; i < addresses.length; i++)
        await core.pair({serverUrl: 'http://studio.tail.ts.net:8123', code: CODE}, f.deps);
    await runner(f, {serverUrl: 'http://studio.tail.ts.net:8123'}).start();
    assert.equal(lookups.length, connections.length);
    connections.forEach((connection, i) => {
        const address = addresses[i % addresses.length];
        assert.equal(connection.url.hostname, address.includes(':') ? '[' + address + ']' : address);
        assert.equal(connection.url.port, '8123');
        assert.equal(connection.headers.Host, 'studio.tail.ts.net:8123');
        assert.equal(lookups[i], 'studio.tail.ts.net');
    });
    assert.equal(f.requests[4].headers.authorization, 'Bearer ' + TOKEN);
    assert.equal(core.validateServerUrl('http://[fd7a:115c:a1e0::42]').ok, true);
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

test('JSON responses up to 32 MiB are accepted', async t => {
    const body = JSON.stringify({device_id: 'd1', token: TOKEN});
    const f = await fixture(t, (req, reply) => {
        reply(200, body + ' '.repeat(32 * 1024 * 1024 - Buffer.byteLength(body))); return true;
    });
    assert.equal((await core.pair({serverUrl: f.serverUrl, code: CODE}, f.deps)).token, TOKEN);
});

test('oversized JSON is aborted at 32 MiB without receiving the rest', async t => {
    let sent = 0, closed = false;
    const total = 40 * 1024 * 1024, chunk = Buffer.alloc(64 * 1024, ' ');
    const f = await fixture(t, (req, reply, res) => {
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.on('close', () => { closed = true; });
        function write() {
            if (closed) return;
            if (sent >= total) { res.end(); return; }
            sent += chunk.length;
            if (res.write(chunk)) setImmediate(write); else res.once('drain', write);
        }
        write(); return true;
    });
    await assert.rejects(core.pair({serverUrl: f.serverUrl, code: CODE}, f.deps), /JSON response exceeds 32 MiB/);
    await until(() => closed);
    assert.ok(sent < total, 'client must close before the server sends the rest');
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
        host_build: 'dev', panel_build: 'dev', os: os.platform() + ' ' + os.release(), fonts: info.fonts});
    assertPrivate(f);
});

test('mismatched or untagged AE builds reject jobs and keep the restart status through idle polls', async t => {
    for (const hostBuild of ['123-oldsha', undefined]) {
        const message = 'The AE script (build ' + (hostBuild || 'unknown') +
            ') does not match the panel (build dev) — fully quit and restart After Effects';
        let polls = 0, r;
        const f = await fixture(t, (req, reply) => {
            if (!req.path.startsWith('/api/ae/next')) return;
            if (polls++ === 0) reply(200, {job});
            else if (polls === 2) reply(204);
            else { r.stop(); reply(204); }
            return true;
        });
        f.deps.evalScript = (script, callback) => {
            f.scripts.push(script);
            assert.ok(script.startsWith('kfInfo('), 'a stale host must never receive a sync');
            callback(JSON.stringify(Object.assign({}, info, {host_build: hostBuild})));
        };
        r = runner(f);
        await r.start();
        assert.deepEqual(f.requests.find(req => req.path.endsWith('/result')).body, {ok: false, error: message});
        assert.equal(f.statuses.at(-1).message, message);
        assert.ok(f.statuses.every(s => s.message === message));
        assert.equal(f.requests.some(req => /\/(spec|assets|progress)(\/|$)/.test(req.path)), false);
        assert.deepEqual(fs.readdirSync(f.documentsDir), []);
        const announced = f.requests.find(req => req.path === '/api/ae/info').body.info;
        assert.equal(announced.host_build, hostBuild);
        assert.equal(announced.panel_build, 'dev');
    }
});

test('matching AE and panel builds run the job', async t => {
    const f = await oneJob(t);
    assert.equal(f.result.ok, true);
    assert.equal(f.scripts.filter(script => script.startsWith('kfSync(')).length, 1);
    assert.ok(f.statuses.some(s => s.message.startsWith('Synced v7:')));
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
        {assets: [{name: 'a.exe', sha256: sha}]},
        ...[undefined, -1, 1.5, '16', Number.MAX_SAFE_INTEGER + 1].map(size =>
            ({assets: [{name: 'a.png', sha256: sha, bytes: size}]}))];
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
            assert.match(f.result.error, /SHA-256 mismatch|Asset byte count mismatch/);
            assert.deepEqual(fs.readdirSync(folder), []);
        }
    }
});

test('asset larger than declared is aborted while streaming and removes the temp file', async t => {
    let sent = 0, closed = false;
    const total = 8 * 1024 * 1024, chunk = Buffer.alloc(64 * 1024, 'x');
    const hash = crypto.createHash('sha256').update(Buffer.alloc(total, 'x')).digest('hex');
    const asset = {name: 'a.png', sha256: hash, bytes: bytes.length};
    const f = await oneJob(t, {route: (req, reply, res) => {
        if (req.path.endsWith('/spec')) { reply(200, Object.assign({}, spec, {assets: [asset]})); return true; }
        if (!req.path.includes('/assets/')) return;
        res.writeHead(200, {'X-Keepframe-Sha256': hash});
        res.on('close', () => { closed = true; });
        function write() {
            if (closed) return;
            if (sent >= total) { res.end(); return; }
            sent += chunk.length;
            if (res.write(chunk)) setImmediate(write); else res.once('drain', write);
        }
        write(); return true;
    }});
    assert.equal(f.result.ok, false);
    assert.match(f.result.error, /Asset exceeds declared byte count/);
    await until(() => closed);
    assert.ok(sent < total);
    assert.equal(f.scripts.some(s => s.startsWith('kfSync(')), false);
    assert.deepEqual(fs.readdirSync(path.join(f.documentsDir, 'Keepframe/demo/assets')), []);
});

test('asset shorter than declared fails even when the hashes match', async t => {
    const f = await oneJob(t, {route: (req, reply) => {
        if (req.path.endsWith('/spec')) {
            reply(200, Object.assign({}, spec, {assets: [Object.assign({}, spec.assets[0], {bytes: bytes.length + 1})]}));
            return true;
        }
    }});
    assert.equal(f.result.ok, false);
    assert.match(f.result.error, /Asset byte count mismatch/);
    assert.equal(f.scripts.some(s => s.startsWith('kfSync(')), false);
    assert.deepEqual(fs.readdirSync(path.join(f.documentsDir, 'Keepframe/demo/assets')), []);
});

test('declared asset bytes stream correctly through file backpressure', async t => {
    const data = Buffer.alloc(1024 * 1024, 'x');
    const hash = crypto.createHash('sha256').update(data).digest('hex');
    const asset = {name: 'a.png', sha256: hash, bytes: data.length};
    let pauses = 0;
    const injectedFs = Object.assign({}, fs, {createWriteStream: (file, options) => {
        const stream = fs.createWriteStream(file, Object.assign({}, options, {highWaterMark: 1024}));
        const write = stream.write.bind(stream);
        stream.write = chunk => { const ready = write(chunk); if (!ready) pauses++; return ready; };
        return stream;
    }});
    const f = await oneJob(t, {deps: {fs: injectedFs}, route: (req, reply) => {
        if (req.path.endsWith('/spec')) { reply(200, Object.assign({}, spec, {assets: [asset]})); return true; }
        if (req.path.includes('/assets/')) { reply(200, data, {'X-Keepframe-Sha256': hash}); return true; }
    }});
    assert.equal(f.result.ok, true);
    assert.ok(pauses > 0);
    assert.deepEqual(fs.readFileSync(core.assetCachePath(f.documentsDir, 'demo', asset, path)), data);
});

test('unlink EPERM is logged safely and cannot mask a download error or successful sync', async t => {
    for (const failed of [true, false]) {
        const injectedFs = Object.assign({}, fs, {promises: Object.assign({}, fs.promises, {
            unlink: async () => { throw Object.assign(new Error('unlink failed ' + TOKEN + ' ' + CODE), {code: 'EPERM'}); }
        })});
        const f = await oneJob(t, {deps: {fs: injectedFs}, route: (req, reply) => {
            if (failed && req.path.includes('/assets/')) {
                reply(200, bytes, {'X-Keepframe-Sha256': '0'.repeat(64)}); return true;
            }
        }});
        if (failed) {
            assert.equal(f.result.ok, false);
            assert.match(f.result.error, /SHA-256 mismatch/);
        } else assert.deepEqual(f.result, {ok: true, result: success});
        assert.ok(f.logs.some(message => /temporary asset file.*EPERM/.test(message)));
        assertPrivate(f, f.result);
    }
    const missing = await oneJob(t);
    assert.equal(missing.result.ok, true);
    assert.equal(missing.logs.some(message => message.includes('temporary asset file')), false, 'ENOENT is ignored');
});

test('applied:false posts full successful result and surfaces hand edits', async t => {
    const result = {ok: true, applied: false, hand_edited: ['kf:a', 'kf:title']};
    const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(JSON.stringify(script.startsWith('kfInfo(') ? info : result))}});
    assert.deepEqual(f.result, {ok: true, result});
    assert.ok(f.statuses.some(s => s.message ===
        'AE layers were edited by hand: kf:a, kf:title — overwrite from the web page'));
});

test('unparseable and failed evalScript results fail jobs with safe raw output and line', async t => {
    for (const raw of ['EvalScript error. ' + CODE + ' ' + TOKEN + 'x'.repeat(500),
        JSON.stringify({ok: false, error: 'AE failed ' + TOKEN, line: 42}), 'null', '{}']) {
        const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(script.startsWith('kfInfo(') ? JSON.stringify(info) : raw)}});
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
    for (const kind of ['render_final', 'package']) {
        const f = await oneJob(t, {job: {kind}});
        assert.deepEqual(f.result, {ok: false, error: kind + ' is not supported by this extension version'});
        assert.equal(f.requests.some(req => req.path.endsWith('/spec')), false);
    }
});

const renderJob = {id: 'j_' + '0'.repeat(16), kind: 'render_frames',
    project: 'demo', scene: 's1', version: 'v7', params: {frames: [0, 5, 10000], tag: 'keepframe:demo/s1'}};

async function oneRenderJob(t, options = {}) {
    return oneJob(t, {job: Object.assign({}, renderJob, options.job),
        route: (req, reply, res) => {
            if (options.route && options.route(req, reply, res)) return true;
            if (req.path.includes('/files/')) { reply(204); return true; }
        }, prepare: async f => {
            const folder = path.join(f.documentsDir, 'temp', 'keepframe-' + renderJob.id);
            f.frameFolder = folder;
            f.deps.os = Object.assign({}, os, {tmpdir: () => path.join(f.documentsDir, 'temp')});
            let time = 0;
            f.deps.now = () => time;
            f.deps.sleep = ms => {
                if (ms !== 250) return never(); // Heartbeats remain independent of the accelerated poll clock.
                time += ms;
                return Promise.resolve();
            };
            fs.mkdirSync(folder, {recursive: true});
            const response = {ok: true, width: 320, height: 180, frames: renderJob.params.frames.map(frame => {
                const file = path.join(folder, 'frame_' + String(frame).padStart(4, '0') + '.png');
                fs.writeFileSync(file, pngBytes);
                return {frame, path: file};
            })};
            if (options.response) options.response(response, f);
            f.deps.evalScript = (script, callback) => {
                f.scripts.push(script);
                callback(JSON.stringify(script.startsWith('kfInfo(') ? info : response));
            };
            if (options.prepare) await options.prepare(f);
        }});
}

test('render_frames streams all frames, posts progress and result, and removes temporary files', async t => {
    const streams = [];
    const f = await oneRenderJob(t, {prepare: f => {
        f.deps.fs = Object.assign({}, fs, {
            readFileSync: () => assert.fail('frames must stream'),
            promises: Object.assign({}, fs.promises, {readFile: () => assert.fail('frames must stream')}),
            createReadStream: (file, options) => {
                streams.push({file, options});
                return fs.createReadStream(file, Object.assign({highWaterMark: 3}, options));
            }
        });
    }});
    assert.deepEqual(f.result, {ok: true, result: {frames: [0, 5, 10000]}});
    let request;
    vm.runInNewContext(f.scripts.find(s => s.startsWith('kfRender(')), {kfRender: raw => { request = JSON.parse(raw); }});
    assert.deepEqual(request, {job: renderJob.id, tag: 'keepframe:demo/s1', frames: [0, 5, 10000]});
    const uploads = f.requests.filter(req => req.path.includes('/files/'));
    assert.deepEqual(uploads.map(req => req.path), [0, 5, 10000].map(frame =>
        '/api/ae/jobs/' + renderJob.id + '/files/frame_' + String(frame).padStart(4, '0') + '.png'));
    for (const req of uploads) {
        assert.equal(req.method, 'PUT'); assert.deepEqual(req.data, pngBytes);
        assert.equal(req.headers['content-type'], 'image/png');
        assert.equal(req.headers['content-length'], String(pngBytes.length));
        assert.equal(req.headers.authorization, 'Bearer ' + TOKEN);
    }
    assert.equal(streams.length, 3);
    assert.ok(streams.every(s => s.options && s.options.start === 0 && s.options.end === pngBytes.length - 1));
    assert.deepEqual(f.requests.filter(req => req.path.endsWith('/progress') && req.body.stage === 'uploading')
        .map(req => req.body), [1, 2, 3].map(done => ({stage: 'uploading', done, total: 3})));
    assert.ok(f.statuses.some(s => s.message === 'Rendering 3 frames of demo / s1 v7…'));
    assert.deepEqual(f.statuses.filter(s => s.message.startsWith('Uploading frame')).map(s => s.message),
        ['Uploading frame 1/3…', 'Uploading frame 2/3…', 'Uploading frame 3/3…']);
    assert.ok(f.statuses.some(s => s.message === 'Rendered 3 frames — Keepframe is comparing them'));
    assert.equal(fs.existsSync(f.frameFolder), false);
    assertPrivate(f);
});

test('invalid AE frames reject the entire list before uploading and only clean the job folder', async t => {
    for (const bad of ['outside', 'unrequested', 'duplicate', 'missing', 'name', 'symlink', 'directory']) {
        let outside;
        const f = await oneRenderJob(t, {response: (response, f) => {
            outside = path.join(f.documentsDir, 'private.png'); fs.writeFileSync(outside, 'keep');
            const frame = response.frames[2];
            if (bad === 'outside') frame.path = outside;
            if (bad === 'unrequested') frame.frame = 99;
            if (bad === 'duplicate') frame.frame = 0;
            if (bad === 'missing') response.frames.pop();
            if (bad === 'name') frame.path = response.frames[0].path;
            if (bad === 'symlink') { fs.unlinkSync(frame.path); fs.symlinkSync(outside, frame.path); }
            if (bad === 'directory') { fs.unlinkSync(frame.path); fs.mkdirSync(frame.path); }
        }});
        assert.deepEqual(f.result, {ok: false, error: 'Invalid frame from AE'}, bad);
        assert.equal(f.requests.some(req => req.path.includes('/files/')), false, bad);
        assert.equal(fs.existsSync(f.frameFolder), false, bad);
        assert.equal(fs.readFileSync(outside, 'utf8'), 'keep');
        assert.equal(JSON.stringify({result: f.result, statuses: f.statuses}).includes(f.documentsDir), false);
    }
});

test('native realpath accepts Windows case and short-name aliases but rejects different folders', async t => {
    for (const [platform, different] of [['win32', false], ['win32', true], ['linux', false]]) {
        const nativeCalls = [];
        const f = await oneRenderJob(t, {response: (response, f) => {
            const alias = path.join(f.documentsDir, 'SEOWOO~1', path.basename(f.frameFolder));
            fs.mkdirSync(alias, {recursive: true});
            response.frames.forEach(frame => {
                frame.path = path.join(alias, path.basename(frame.path));
                fs.writeFileSync(frame.path, pngBytes);
            });
            f.deps.platform = platform;
            const canonical = 'C:\\Users\\Seowoo\\AppData\\Local\\Temp\\' + path.basename(f.frameFolder);
            const realpath = Object.assign(() => assert.fail('use realpath.native'), {native: (folder, callback) => {
                nativeCalls.push(folder);
                callback(null, folder === f.frameFolder ? canonical : different ? 'D:\\different' : canonical.toLowerCase());
            }});
            f.deps.fs = Object.assign({}, fs, {realpath, promises: Object.assign({}, fs.promises, {
                // Unlike the native API, the legacy resolver may retain SEOWOO~1.
                realpath: async folder => folder === f.frameFolder ? canonical : canonical.replace('Seowoo', 'SEOWOO~1')
            })});
        }});
        assert.deepEqual(f.result, platform === 'win32' && !different ?
            {ok: true, result: {frames: renderJob.params.frames}} : {ok: false, error: 'Invalid frame from AE'});
        assert.ok(nativeCalls.includes(f.frameFolder));
        assert.ok(nativeCalls.some(folder => folder.includes('SEOWOO~1')));
        if (different || platform !== 'win32') assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
    }
});

test('a frame written 1.5 s after kfRender returns is awaited with rendering heartbeats then uploaded', async t => {
    let returnedAt, writtenAt;
    const f = await oneRenderJob(t, {response: (response, f) => {
        const frame = response.frames[0];
        fs.unlinkSync(frame.path);
        f.deps.now = Date.now;
        f.deps.sleep = ms => new Promise(resolve => setTimeout(resolve, ms === 15000 ? 300 : ms));
    }, prepare: f => {
        const evaluate = f.deps.evalScript;
        f.deps.evalScript = (script, callback) => evaluate(script, raw => {
            callback(raw);
            if (script.startsWith('kfRender(')) {
                returnedAt = Date.now();
                const frame = JSON.parse(raw).frames[0];
                setTimeout(() => { writtenAt = Date.now(); fs.writeFileSync(frame.path, pngBytes); }, 1500);
            }
        });
    }});
    assert.ok(writtenAt - returnedAt >= 1490);
    assert.deepEqual(f.result, {ok: true, result: {frames: renderJob.params.frames}});
    assert.equal(f.requests.filter(req => req.path.includes('/files/')).length, 3);
    assert.ok(f.requests.filter(req => req.path.endsWith('/progress') && req.body.stage === 'rendering' &&
        req.body.done === 0 && req.body.total === 3).length >= 3);
    assert.deepEqual([...new Set(f.requests.filter(req => req.path.endsWith('/progress') && req.body.stage === 'rendering' &&
        req.body.done > 0).map(req => req.body.done))], [1, 2, 3]);
});

test('a frame that never appears times out after 60 s without any upload', async t => {
    const f = await oneRenderJob(t, {response: response => fs.unlinkSync(response.frames[1].path)});
    assert.deepEqual(f.result, {ok: false, error: 'AE did not write frame 5 within 60 s'});
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
    assert.ok(f.requests.some(req => req.path.endsWith('/progress') && req.body.stage === 'rendering' && req.body.done === 1));
    assert.equal(fs.existsSync(f.frameFolder), false);
});

test('transient Windows frame locks are awaited at stat, open and read until the deadline', async t => {
    for (const code of ['EBUSY', 'EPERM', 'EACCES']) {
        for (const operation of ['lstat', 'open', 'read']) {
            for (const permanent of [false, true]) {
                let blocked = 0;
                const f = await oneRenderJob(t, {prepare: f => {
                    const locked = () => {
                        blocked++;
                        if (permanent || blocked <= 2) throw Object.assign(new Error('locked'), {code});
                    };
                    f.deps.fs = Object.assign({}, fs, {promises: Object.assign({}, fs.promises, {
                        lstat: async file => { if (operation === 'lstat' && file.endsWith('frame_0000.png')) locked(); return fs.promises.lstat(file); },
                        open: async (file, flags) => {
                            if (operation === 'open' && file.endsWith('frame_0000.png')) locked();
                            const handle = await fs.promises.open(file, flags);
                            if (operation === 'read' && file.endsWith('frame_0000.png')) {
                                const read = handle.read.bind(handle);
                                handle.read = async (...args) => { locked(); return read(...args); };
                            }
                            return handle;
                        }
                    })});
                }});
                assert.deepEqual(f.result, permanent ? {ok: false, error: 'AE did not write frame 0 within 60 s'} :
                    {ok: true, result: {frames: renderJob.params.frames}});
                assert.ok(blocked >= 3);
                assert.equal(f.requests.some(req => req.path.includes('/files/')), !permanent);
                assert.equal(f.logs.some(message => message.includes('Invalid frame from AE')), false);
            }
        }
    }
});

test('an oversized frame logs its 25 MB limit without a local path', async t => {
    const f = await oneRenderJob(t, {response: response => fs.truncateSync(response.frames[0].path, 25 * 1024 * 1024 + 1)});
    assert.deepEqual(f.result, {ok: false, error: 'Invalid frame from AE'});
    assert.ok(f.logs.includes('Invalid frame from AE: frame larger than 25 MB'));
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
    assert.equal(JSON.stringify(f.logs).includes(f.documentsDir), false);
});

test('truncated PNGs without a signature or IEND never upload', async t => {
    for (const data of [pngBytes.subarray(0, 8), pngBytes.subarray(0, -12),
        Buffer.concat([Buffer.alloc(8), pngBytes.subarray(8)]), Buffer.alloc(0)]) {
        const f = await oneRenderJob(t, {response: response => fs.writeFileSync(response.frames[2].path, data)});
        assert.deepEqual(f.result, {ok: false, error: 'AE did not write frame 10000 within 60 s'});
        assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
        assert.equal(fs.existsSync(f.frameFolder), false);
    }
});

test('a PNG growing between completeness checks never uploads', async t => {
    const f = await oneRenderJob(t, {response: (response, f) => {
        const sleep = f.deps.sleep;
        let growth = 0;
        f.deps.sleep = ms => {
            if (ms === 250) fs.writeFileSync(response.frames[2].path,
                Buffer.concat([pngBytes.subarray(0, -12), Buffer.alloc(++growth), pngBytes.subarray(-12)]));
            return sleep(ms);
        };
    }});
    assert.deepEqual(f.result, {ok: false, error: 'AE did not write frame 10000 within 60 s'});
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
});

test('the combined frame wait is capped at ten minutes', async t => {
    const frames = Array.from({length: 16}, (_, i) => i);
    let time = 0;
    const f = await oneRenderJob(t, {job: {params: {...renderJob.params, frames}}, response: (response, f) => {
        response.frames = frames.map(frame => ({frame, path: path.join(f.frameFolder, 'frame_' + String(frame).padStart(4, '0') + '.png')}));
        fs.readdirSync(f.frameFolder).forEach(name => fs.unlinkSync(path.join(f.frameFolder, name)));
        f.deps.now = () => time;
        f.deps.sleep = ms => {
            if (ms !== 250) return never();
            time += ms;
            for (const frame of response.frames) {
                if (time >= (frame.frame + 1) * 39000 && !fs.existsSync(frame.path)) fs.writeFileSync(frame.path, pngBytes);
            }
            return Promise.resolve();
        };
    }});
    assert.deepEqual(f.result, {ok: false, error: 'AE did not write frame 15 within 60 s'});
    assert.equal(time, 10 * 60 * 1000);
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
});

test('uploads stop at the measured size even if the PNG grows before the stream opens', async t => {
    const readSizes = [];
    const f = await oneRenderJob(t, {prepare: f => {
        f.deps.fs = Object.assign({}, fs, {createReadStream: (file, options) => {
            fs.appendFileSync(file, 'extra data');
            const stream = fs.createReadStream(file, options);
            let read = 0;
            stream.on('data', chunk => { read += chunk.length; });
            stream.on('end', () => readSizes.push(read));
            return stream;
        }});
    }});
    assert.equal(f.result.ok, true);
    const uploads = f.requests.filter(req => req.path.includes('/files/'));
    assert.equal(uploads.length, 3);
    assert.deepEqual(readSizes, [pngBytes.length, pngBytes.length, pngBytes.length]);
    uploads.forEach(req => assert.deepEqual(req.data, pngBytes));
});

test('a frame that shrinks after the completeness check fails clearly before uploading', async t => {
    const f = await oneRenderJob(t, {prepare: f => {
        const status = f.deps.setStatus;
        f.deps.setStatus = (message, details) => {
            status(message, details);
            if (message.startsWith('Uploading frame')) fs.truncateSync(path.join(f.frameFolder, 'frame_0000.png'), 8);
        };
    }});
    assert.equal(f.result.ok, false);
    assert.match(f.result.error, /Frame upload shrank/);
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
    assert.equal(fs.existsSync(f.frameFolder), false);
});

test('a frame that shrinks as the upload stream opens fails with its byte count and closes', async t => {
    let stream;
    const f = await oneRenderJob(t, {prepare: f => {
        f.deps.fs = Object.assign({}, fs, {createReadStream: (file, options) => {
            fs.truncateSync(file, 8);
            stream = fs.createReadStream(file, options);
            return stream;
        }});
    }});
    assert.deepEqual(f.result, {ok: false, error: 'Frame upload shrank (expected ' + pngBytes.length + ' bytes, read 8)'});
    assert.equal(stream.closed, true);
    assert.equal(fs.existsSync(f.frameFolder), false);
    assertPrivate(f, f.result);
});

test('frame rejection logs a path-free reason and preserves the user-facing error', async t => {
    for (const reason of ['frame outside the job folder', 'not a regular file', 'symlink']) {
        const f = await oneRenderJob(t, {response: (response, f) => {
            const frame = response.frames[0];
            if (reason === 'frame outside the job folder') {
                frame.path = path.join(f.documentsDir, path.basename(frame.path));
                fs.writeFileSync(frame.path, pngBytes);
            } else {
                fs.unlinkSync(frame.path);
                if (reason === 'symlink') fs.symlinkSync(response.frames[1].path, frame.path);
                else fs.mkdirSync(frame.path);
            }
        }});
        assert.deepEqual(f.result, {ok: false, error: 'Invalid frame from AE'});
        assert.ok(f.logs.includes('Invalid frame from AE: ' + reason));
        assert.equal(JSON.stringify({result: f.result, statuses: f.statuses, logs: f.logs}).includes(f.documentsDir), false);
        assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
        assertPrivate(f);
    }
});

test('a symlinked job folder is rejected without deleting its target', async t => {
    let target;
    const f = await oneRenderJob(t, {response: (response, f) => {
        target = path.join(f.documentsDir, 'private-job-folder');
        fs.renameSync(f.frameFolder, target);
        fs.symlinkSync(target, f.frameFolder, 'dir');
    }});
    assert.deepEqual(f.result, {ok: false, error: 'Invalid frame from AE'});
    assert.ok(f.logs.includes('Invalid frame from AE: symlink'));
    assert.equal(f.requests.some(req => req.path.includes('/files/')), false);
    assert.equal(fs.existsSync(f.frameFolder), false);
    assert.equal(fs.readFileSync(path.join(target, 'frame_0000.png')).equals(pngBytes), true);
    assert.equal(JSON.stringify(f.logs).includes(f.documentsDir), false);
});

test('upload 409/413 retain redacted server text and clean all temporary frames', async t => {
    for (const status of [409, 413]) {
        const f = await oneRenderJob(t, {route: (req, reply) => {
            if (req.path.includes('/files/')) { reply(status, {error: 'frame rejected ' + TOKEN + ' ' + CODE}); return true; }
        }});
        assert.deepEqual(f.result, {ok: false, error: 'frame rejected [redacted] [redacted]'});
        assert.equal(f.requests.filter(req => req.path.includes('/files/')).length, 1);
        assert.equal(fs.existsSync(f.frameFolder), false);
        assert.ok(f.statuses.some(s => s.message === 'Render failed: ' + f.result.error));
        assertPrivate(f, f.result);
    }
});

test('render host errors still remove the temporary job folder', async t => {
    const f = await oneRenderJob(t, {response: response => {
        response.ok = false; response.error = 'AE did not write frame 0 within 60 s'; response.line = 9;
    }});
    assert.deepEqual(f.result, {ok: false, error: 'AE did not write frame 0 within 60 s', line: 9});
    assert.equal(fs.existsSync(f.frameFolder), false);
});

test('render upload and cleanup work with the CEP 11 filesystem surface', {timeout: 2000}, async t => {
    let removals = 0;
    const f = await oneRenderJob(t, {prepare: f => {
        f.deps.fs = Object.assign({}, fs, {
            createReadStream: file => {
                const stream = fs.createReadStream(file);
                Object.defineProperty(stream, 'closed', {value: undefined});
                return stream;
            },
            promises: Object.assign({}, fs.promises, {rm: undefined, rmdir: (folder, options) => {
                removals++;
                assert.equal(options.recursive, true);
                return fs.promises.rm(folder, {recursive: true, force: true});
            }})
        });
    }});
    assert.deepEqual(f.result, {ok: true, result: {frames: [0, 5, 10000]}});
    assert.equal(removals, 1);
    assert.equal(fs.existsSync(f.frameFolder), false);
});

test('frame stream errors close the stream, report safely, and clean the folder', async t => {
    let stream;
    const f = await oneRenderJob(t, {prepare: f => {
        f.deps.fs = Object.assign({}, fs, {createReadStream: file => {
            stream = fs.createReadStream(file);
            stream.destroy(Object.assign(new Error(file + ' ' + TOKEN), {code: 'EIO'}));
            return stream;
        }});
    }});
    assert.deepEqual(f.result, {ok: false, error: 'Could not read frame upload (EIO)'});
    assert.equal(stream.closed, true);
    assert.equal(fs.existsSync(f.frameFolder), false);
    assertPrivate(f, f.result);
    assert.equal(JSON.stringify(f.result).includes(f.documentsDir), false);
});

test('cleanup errors are logged and preserve uploaded success or an AE error', async t => {
    for (const hostFails of [false, true]) {
        const f = await oneRenderJob(t, {
            response: response => { if (hostFails) { response.ok = false; response.error = 'AE failed'; } },
            prepare: f => {
                f.deps.fs = Object.assign({}, fs, {promises: Object.assign({}, fs.promises, {
                    rm: async (folder, options) => {
                        assert.equal(options.maxRetries, 3);
                        assert.equal(options.retryDelay, 200);
                        throw Object.assign(new Error(folder + ' ' + TOKEN), {code: 'EPERM'});
                    }
                })});
            }
        });
        assert.deepEqual(f.result, hostFails ? {ok: false, error: 'AE failed'} :
            {ok: true, result: {frames: renderJob.params.frames}});
        assert.ok(f.logs.includes('Could not remove temporary frame folder (EPERM)'));
        assertPrivate(f, f.result);
    }
});

test('CEP cleanup retries transient errors with a bounded fallback', async t => {
    for (const fails of [2, 4]) {
        let attempts = 0, waits = 0;
        const f = await oneRenderJob(t, {prepare: f => {
            const sleep = f.deps.sleep;
            f.deps.sleep = ms => { if (ms === 200) { waits++; return Promise.resolve(); } return sleep(ms); };
            f.deps.fs = Object.assign({}, fs, {promises: Object.assign({}, fs.promises, {
                rm: undefined, rmdir: async (folder, options) => {
                    attempts++;
                    if (attempts <= fails) throw Object.assign(new Error(folder), {code: 'EBUSY'});
                    return fs.promises.rm(folder, options);
                }
            })});
        }});
        assert.deepEqual(f.result, {ok: true, result: {frames: renderJob.params.frames}});
        assert.equal(attempts, fails === 2 ? 3 : 4);
        assert.equal(waits, fails === 2 ? 2 : 3);
        assert.equal(fs.existsSync(f.frameFolder), fails === 4);
        assert.equal(f.logs.some(message => message.includes('Could not remove temporary frame folder')), fails === 4);
    }
});

test('panel translates rendering and uploading statuses into Korean', () => {
    const p = panelHarness('ko_KR'), deps = p.runs[0].deps;
    for (const [message, expected] of [
        ['Rendering 3 frames of demo / s1 v7…', 'demo / s1 v7 프레임 3개 렌더링 중…'],
        ['Uploading frame 1/3…', '프레임 업로드 중 1/3…'],
        ['Rendered 3 frames — Keepframe is comparing them', '프레임 3개 렌더링 완료 — Keepframe에서 비교 중'],
        ['Render failed: Network request failed', '렌더링 실패: 네트워크 요청 실패'],
        ['Render failed: AE did not write frame 5 within 60 s', '렌더링 실패: AE가 60초 안에 프레임 5를 기록하지 못했습니다'],
        ['Render failed: this After Effects cannot export frames; update to After Effects 24.1 or newer',
            '렌더링 실패: 이 After Effects에서는 프레임을 내보낼 수 없습니다. After Effects 24.1 이상으로 업데이트하세요']
    ]) {
        deps.setStatus(message); deps.log(message);
        assert.equal(p.nodes.status.textContent, expected);
        assert.ok(p.nodes.log.value.includes(expected));
    }
    deps.setStatus('Uploading frame 1/3…', {job: renderJob, progress: {stage: 'uploading', done: 1, total: 3}});
    assert.ok(p.nodes['current-job'].textContent.includes('프레임 업로드 중'));
});

async function until(check) {
    for (let i = 0; i < 1000; i++) {
        if (check()) return;
        await new Promise(resolve => setTimeout(resolve, 1));
    }
    assert.fail('condition never became true');
}

function fakeClock(deps) {
    let time = 0, id = 0;
    const timers = new Map();
    deps.now = () => time;
    deps.setTimeout = (callback, ms) => {
        timers.set(++id, {at: time + ms, callback}); return id;
    };
    deps.clearTimeout = timer => timers.delete(timer);
    deps.sleep = ms => {
        let timer;
        const sleeping = new Promise(resolve => { timer = deps.setTimeout(resolve, ms); });
        sleeping.cancel = () => deps.clearTimeout(timer);
        return sleeping;
    };
    return {advance: ms => {
        time += ms;
        for (const [timer, value] of Array.from(timers)) {
            if (value.at <= time && timers.delete(timer)) value.callback();
        }
    }, pending: () => timers.size};
}

test('hung kfSync times out at ten minutes, stops heartbeats and posts failure', async t => {
    let polls = 0, lateCallback, resultReply, completed = false;
    const f = await fixture(t, (req, reply) => {
        if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
        if (req.path.endsWith('/result')) { resultReply = reply; return true; }
    });
    const clock = fakeClock(f.deps);
    f.deps.evalScript = (script, cb) => {
        if (script.startsWith('kfInfo(')) cb(JSON.stringify(info)); else lateCallback = cb;
    };
    const r = runner(f), running = r.start().then(() => { completed = true; });
    t.after(() => r.stop());
    await until(() => lateCallback && f.requests.some(req => req.path.endsWith('/progress')));
    for (let i = 1; i < 40; i++) {
        const count = f.requests.filter(req => req.path.endsWith('/progress')).length;
        clock.advance(15000);
        await until(() => f.requests.filter(req => req.path.endsWith('/progress')).length > count);
        assert.equal(resultReply, undefined);
    }
    clock.advance(15000);
    await until(() => resultReply);
    assert.deepEqual(f.requests.find(req => req.path.endsWith('/result')).body, {ok: false,
        error: 'After Effects did not finish within 10 min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.'});
    const beats = f.requests.filter(req => req.path.endsWith('/progress')).length;
    clock.advance(60000);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.requests.filter(req => req.path.endsWith('/progress')).length, beats);
    assert.equal(clock.pending(), 0, 'host and heartbeat timers must both be cleared');
    resultReply(200, {job: {state: 'failed'}});
    await running;
    assert.equal(completed, true);
    const statuses = f.statuses.length, requests = f.requests.length;
    lateCallback(JSON.stringify(success));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.statuses.length, statuses);
    assert.equal(f.requests.length, requests);
});

test('completed sync keeps heartbeats until its result is acknowledged', async t => {
    let polls = 0, resultReply;
    const f = await fixture(t, (req, reply) => {
        if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
        if (req.path.endsWith('/result')) { resultReply = reply; return true; }
    });
    const clock = fakeClock(f.deps);
    const r = runner(f), running = r.start();
    try {
        await until(() => resultReply);
        assert.equal(clock.pending(), 1, 'keep the heartbeat timer until acknowledgement');
        const beats = f.requests.filter(req => req.path.endsWith('/progress')).length;
        clock.advance(15000);
        await until(() => f.requests.filter(req => req.path.endsWith('/progress')).length > beats);
        resultReply(200, {job: {state: 'done'}});
        await running;
        assert.equal(clock.pending(), 0);
    } finally { r.stop(); }
});

test('hung kfInfo has a thirty-second deadline during pairing and polling', async t => {
    const f = await fixture(t);
    const clock = fakeClock(f.deps);
    let lateCallback, paired = false;
    f.deps.evalScript = (script, cb) => { lateCallback = cb; };
    const pairing = core.pair({serverUrl: f.serverUrl, code: CODE}, f.deps);
    const rejection = assert.rejects(pairing,
        {message: 'After Effects did not finish within 0.5 min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.'});
    pairing.then(() => { paired = true; }, () => {});
    clock.advance(29999);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(paired, false);
    clock.advance(1);
    await Promise.race([rejection, until(() => clock.pending() === 0)]);
    assert.equal(clock.pending(), 0);
    lateCallback(JSON.stringify(info));
    await rejection;
    assert.equal(f.requests.length, 0);
    const r = runner(f), running = r.start();
    t.after(() => r.stop());
    clock.advance(30000);
    await until(() => f.statuses.some(s => s.message.includes('did not finish within 0.5 min')));
    r.stop();
    await running;
    assert.equal(clock.pending(), 0);
});

test('stop immediately rejects either pending AE entry point and ignores late callbacks', async t => {
    for (const entry of ['kfInfo(', 'kfSync']) {
        let polls = 0, lateCallback, completed = false;
        const f = await fixture(t, (req, reply) => {
            if (req.path.startsWith('/api/ae/next') && polls++ === 0) { reply(200, {job}); return true; }
        });
        const clock = fakeClock(f.deps);
        f.deps.evalScript = (script, cb) => {
            if (script.startsWith(entry)) lateCallback = cb; else cb(JSON.stringify(info));
        };
        const r = runner(f), running = r.start().then(() => { completed = true; });
        t.after(() => r.stop());
        await until(() => lateCallback);
        r.stop();
        await until(() => completed);
        await running;
        assert.equal(clock.pending(), 0);
        const count = f.requests.length, statuses = f.statuses.length;
        lateCallback('invalid late result ' + TOKEN);
        clock.advance(600000);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(f.requests.length, count);
        assert.equal(f.statuses.length, statuses);
        assertPrivate(f);
    }
});

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
        if (script.startsWith('kfInfo(')) cb(JSON.stringify(info)); else finishSync = cb;
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

test('result posts retry 5xx and 429 with the network backoff and identical bytes', async t => {
    const failures = ['network', 500, 502, 503, 504, 429, 599];
    let posts = 0;
    const sleeps = [];
    const f = await oneJob(t, {deps: {sleep: ms => {
        if (ms === 15000) return never();
        sleeps.push(ms); return Promise.resolve();
    }}, route: (req, reply, res) => {
        if (!req.path.endsWith('/result')) return;
        const status = failures[posts++];
        if (status === 'network') { res.destroy(); return true; }
        if (status) { reply(status, {error: TOKEN}); return true; }
    }});
    const results = f.requests.filter(req => req.path.endsWith('/result'));
    assert.equal(results.length, failures.length + 1);
    assert.ok(results.every(req => req.raw === JSON.stringify({ok: true, result: success})));
    assert.deepEqual(sleeps, [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    assert.equal(f.scripts.filter(s => s.startsWith('kfSync(')).length, 1);
    assertPrivate(f);
});

test('a late heartbeat acknowledgement does not reset result retry backoff', async t => {
    let posts = 0, progressReply;
    const sleeps = [];
    const f = await oneJob(t, {deps: {sleep: ms => {
        if (ms === 15000) return never();
        sleeps.push(ms);
        return Promise.resolve();
    }}, route: (req, reply) => {
        if (req.path.endsWith('/progress')) { progressReply = reply; return true; }
        if (!req.path.endsWith('/result')) return;
        if (posts++ < 2) {
            if (posts === 2) progressReply(204);
            reply(503); return true;
        }
    }});
    assert.deepEqual(sleeps, [1000, 2000]);
    assert.equal(f.requests.filter(req => req.path.endsWith('/result')).length, 3);
});

test('result 409 and other non-retryable 4xx are given up with a status line', async t => {
    for (const status of [409, 400, 403, 404, 422]) {
        const sleeps = [];
        const f = await oneJob(t, {deps: {sleep: ms => {
            if (ms === 15000) return never();
            sleeps.push(ms); return Promise.resolve();
        }}, route: (req, reply) => {
            if (req.path.endsWith('/result')) { reply(status, {error: TOKEN}); return true; }
        }});
        assert.equal(f.requests.filter(req => req.path.endsWith('/result')).length, 1);
        assert.equal(f.scripts.filter(s => s.startsWith('kfSync(')).length, 1);
        assert.deepEqual(sleeps, [], 'a discarded result must not trigger a polling retry');
        const message = status === 409 ? 'Result not posted: job already ended (HTTP 409)' :
            'Result not posted: [redacted]';
        assert.ok(f.statuses.some(s => s.message === message));
        assert.ok(f.logs.includes(message));
        assertPrivate(f);
    }
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
    f.deps.dns = {lookup: () => assert.fail('HTTPS must keep its normal DNS/TLS transport')};
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
            if (script.startsWith('kfInfo(')) cb(JSON.stringify(info)); else finishSync = cb;
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
        assert.equal(p.nodes.version.textContent, locale.startsWith('ko') ? '1.0.0 · 빌드 dev' : '1.0.0 · build dev');
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

test('panel translates host timeout status and log in English and Korean', () => {
    for (const locale of ['en_US', 'ko_KR']) {
        const p = panelHarness(locale);
        for (const minutes of [10, 0.5]) {
            const message = 'After Effects did not finish within ' + minutes +
                ' min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.';
            const translated = locale.startsWith('ko') ? 'After Effects가 ' + minutes +
                '분 안에 끝내지 못했습니다. 큰 장면을 아직 처리 중이거나 대화상자를 기다리는 중일 수 있습니다. AE가 응답하면 다시 보내세요.' : message;
            for (const prefix of ['', 'Sync failed: ', 'Not connected: ']) {
                const suffix = prefix === 'Not connected: ' ? ' (retrying in 1 s)' : '';
                const expected = prefix === 'Sync failed: ' && locale.startsWith('ko') ? '동기화 실패: ' + translated :
                    prefix === 'Not connected: ' && locale.startsWith('ko') ? '연결 안 됨: ' + translated + ' (1초 후 재시도)' :
                    prefix + translated + suffix;
                p.runs[0].deps.setStatus(prefix + message + suffix);
                p.runs[0].deps.log(prefix + message + suffix);
                assert.equal(p.nodes.status.textContent, expected);
                assert.ok(p.nodes.log.value.endsWith(expected));
            }
        }
        p.nodes.disconnect.handlers.click();
    }
});

test('disconnect then Pair never waits for the stopped runner or a late AE callback', async () => {
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
    assert.equal(paired, true);
    await pairing;
    finish();
    assert.equal(paired, true);
});


function realHost(fonts = []) {
    const {createAE} = require('../../tests/ae_fake/ae');
    const {execFileSync} = require('node:child_process');
    const root = path.resolve(__dirname, '../..');
    const ae = createAE({state: {app: {fonts: [fonts]}}});
    vm.runInContext(fs.readFileSync(path.join(root, 'extension/host/keepframe.jsx'), 'utf8'), ae.context);
    const produced = JSON.parse(execFileSync(path.join(root, '.venv/bin/python'), ['-c',
        'import json; from pathlib import Path; from tests.test_ae_spec import scene, element; ' +
        'from keepframe.ae.spec import comp_spec; ' +
        'print(json.dumps(comp_spec(scene(element("a", kind="group"), element("b", kind="group")), ' +
        'Path("/tmp"), project="demo", scene_id="s1", version="v7")))'], {cwd: root, encoding: 'utf8'}));
    const sync = () => JSON.parse(ae.context.kfSync(JSON.stringify(produced), '{}', 'false'));
    return {ae, produced, sync, info: () => JSON.parse(ae.context.kfInfo('true'))};
}

test('final: pairing sends stored previous device and caps real host font output', async t => {
    const host = realHost(Array.from({length: 5002}, (_, i) => ({familyName: 'Font ' + i,
        styleName: 'Regular', postScriptName: 'Font-' + i})));
    const f = await fixture(t);
    f.deps.evalScript = (script, cb) => { f.scripts.push(script); cb(JSON.stringify(host.info())); };
    await core.pair({serverUrl: f.serverUrl, code: CODE, previousDeviceId: 'd_0123456789ab'}, f.deps);
    assert.deepEqual(f.requests[0].body.info.fonts, host.info().fonts.slice(0, 5000));
    assert.equal(f.requests[0].body.previous_device_id, 'd_0123456789ab');
    assert.deepEqual(f.scripts, ['kfInfo("true")']);
});

test('final: panel sends its stored deviceId on re-pair', async () => {
    const p = panelHarness('en_US');
    p.nodes['pairing-code'].value = CODE;
    await p.nodes.settings.handlers.submit({preventDefault: () => {}});
    assert.equal(p.pairs[0].previousDeviceId, 'd1');
    assert.equal(p.storage.deviceId, 'd2');
});

test('final: start-up requests fonts, later polls omit them', async t => {
    const host = realHost();
    const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(
        vm.runInContext(script, host.ae.context))}, route: (req, reply) => {
        if (req.path.endsWith('/spec')) { reply(200, host.produced); return true; }
    }});
    // oneJob records scripts only through the fixture callback, so observe calls explicitly below.
    const calls = [];
    const g = await fixture(t);
    g.deps.evalScript = (script, cb) => { calls.push(script); cb(vm.runInContext(script, host.ae.context)); };
    await runner(g).start();
    assert.deepEqual(calls, ['kfInfo("true")', 'kfInfo("false")']);
    assert.ok(g.requests.find(req => req.path === '/api/ae/info').body.info.fonts);
    assert.equal(f.result.result.applied, true);
});

test('final: actual interrupted ids are distinct from actual hand edits in panel status', async t => {
    const host = realHost();
    assert.equal(host.sync().applied, true);
    const comp = host.ae.context.app.project.items[3];
    for (let i = 1; i <= comp.layers.length; i++) {
        const layer = comp.layers[i];
        if (layer.comment.startsWith('keepframe:kf:a;')) layer.comment = 'keepframe:kf:a';
        if (layer.comment.startsWith('keepframe:kf:b;'))
            layer.property('ADBE Transform Group').property('ADBE Opacity').setValue(83);
    }
    const actual = host.sync();
    assert.deepEqual(actual.interrupted, ['kf:a']);
    const f = await oneJob(t, {deps: {evalScript: (script, cb) => cb(JSON.stringify(
        script.startsWith('kfInfo(') ? host.info() : actual))}});
    assert.deepEqual(f.result.result, actual);
    assert.ok(f.statuses.some(s => s.message.includes(
        'a previous sync was interrupted — overwrite to finish it: kf:a')));
    assert.ok(f.statuses.some(s => s.message.includes('edited by hand: kf:b')));
    assert.equal(f.statuses.some(s => s.message.includes('edited by hand: kf:a')), false);
});

for (const suffix of ['/spec', '/assets/a.png']) {
    test('final: job HTTP failures retain safe server JSON error ' + suffix, async t => {
        const f = await oneJob(t, {route: (req, reply) => {
            if (req.path.endsWith(suffix)) {
                reply(404, {error: 'file not found: e1.tex1.png ' + TOKEN + ' ' + CODE + ' ' + 'x'.repeat(3000)});
                return true;
            }
        }});
        assert.equal(f.result.ok, false);
        assert.ok(f.result.error.startsWith('file not found: e1.tex1.png [redacted] [redacted]'));
        assert.ok(f.result.error.length <= 1800);
        assert.ok(f.statuses.some(s => s.message.startsWith('Sync failed: file not found: e1.tex1.png')));
        assertPrivate(f);
    });
}

for (const route of ['/api/ae/info', '/api/ae/next?wait=25']) {
    test('final: connection HTTP failures retain server JSON error ' + route, async t => {
        const f = await fixture(t, (req, reply) => {
            if (req.path === route) { reply(400, {error: 'invalid server data for ' + route}); return true; }
        });
        let r;
        f.deps.sleep = () => { r.stop(); return Promise.resolve(); };
        r = runner(f);
        await r.start();
        assert.ok(f.statuses.some(s => s.message.includes('invalid server data for ' + route)));
    });
}

test('final: JPEG and WebP spec assets keep their real cache suffix', () => {
    for (const name of ['a.jpg', 'a.webp']) {
        const destination = core.assetCachePath('/tmp', 'demo', {...spec.assets[0], name}, path);
        assert.equal(path.extname(destination), path.extname(name));
    }
});
