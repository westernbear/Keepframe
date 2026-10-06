(function (root, factory) {
    const core = factory();
    if (typeof module === 'object' && module.exports) module.exports = core;
    if (typeof window !== 'undefined') root.KeepframeCore = core;
}(typeof window !== 'undefined' ? window : this, function () {
    'use strict';
    const EXTENSION_VERSION = '1.0.0';
    const NOT_PAIRED = 'Not paired: enter a new code from the Keepframe web page';

    function validateServerUrl(value) {
        if (typeof value !== 'string') return {ok: false, error: 'Enter a server URL'};
        value = value.trim();
        // Check the original path too: URL normalizes /../ and drops empty ?/#.
        if (!/^https?:\/\/[^/\\\s?#]+\/?$/i.test(value))
            return {ok: false, error: 'Use an HTTP(S) server URL without a path or query'};
        try {
            const url = new URL(value);
            if (url.username || url.password || value.includes('@'))
                return {ok: false, error: 'URL credentials are not allowed'};
            const host = url.hostname.toLowerCase();
            const ip = host.split('.').map(Number);
            const ipv4 = ip.length === 4 && ip.every(n => Number.isInteger(n) && n >= 0 && n <= 255);
            if (url.protocol === 'http:' && !(host === 'localhost' || host === '[::1]' ||
                host.endsWith('.ts.net') || (ipv4 && (ip[0] === 127 ||
                (ip[0] === 100 && ip[1] >= 64 && ip[1] <= 127)))))
                return {ok: false, error: 'HTTP requires loopback or Tailscale; otherwise use HTTPS'};
            return {ok: true, url: url.origin};
        } catch (_) { return {ok: false, error: 'Invalid server URL'}; }
    }

    function redact(value, secrets) {
        let text = String(value);
        (secrets || []).filter(Boolean).forEach(secret => { text = text.split(secret).join('[redacted]'); });
        return text.replace(/KF-[A-Z0-9]{4}-[A-Z0-9]{4}/gi, '[redacted]');
    }

    function createLog(now, secrets) {
        const entries = [];
        return {entries, add: function (message) {
            entries.push(new Date(now()).toISOString() + ' ' + redact(message, secrets));
            if (entries.length > 200) entries.shift();
        }, text: () => entries.join('\n')};
    }

    function failure(message, properties) { return Object.assign(new Error(message), properties); }
    function safeError(error, secrets) { return redact(error.message || error, secrets).slice(0, 1800); }
    function updateMessage(serverUrl) { return 'Update the Keepframe extension: ' + serverUrl + '/ae/keepframe.zxp'; }
    function networkError(error) {
        const reasons = {ECONNREFUSED: 'Server refused the connection', ENOTFOUND: 'Server host was not found',
            EAI_AGAIN: 'Server host lookup failed', ECONNRESET: 'Connection was interrupted',
            ETIMEDOUT: 'Request timed out', CERT_HAS_EXPIRED: 'Server certificate has expired',
            DEPTH_ZERO_SELF_SIGNED_CERT: 'Server certificate is not trusted'};
        return failure(reasons[error.code] || 'Network request failed', {network: true});
    }

    function request(context, method, route, body, extraHeaders, file) {
        const deps = context.deps;
        return new Promise((resolve, reject) => {
            const url = new URL(context.serverUrl + route);
            const headers = Object.assign({'X-Keepframe-Extension': EXTENSION_VERSION}, extraHeaders);
            if (context.token) headers.Authorization = 'Bearer ' + context.token;
            const data = body === undefined ? undefined : JSON.stringify(body);
            if (data !== undefined) {
                headers['Content-Type'] = 'application/json';
                headers['Content-Length'] = Buffer.byteLength(data);
            }
            let req, output, response, settled = false;
            const finish = (error, value) => {
                if (settled) return;
                settled = true;
                if (req) context.requests.delete(req);
                if (error) {
                    if (response) response.destroy();
                    if (req) req.destroy();
                    if (output && !output.closed) {
                        output.once('close', () => reject(error));
                        output.destroy();
                    } else reject(error);
                } else resolve(value);
            };
            try {
                // CEP's window.URL is Chromium's URL, not Node's URL class. Pass a string to Node.
                req = (url.protocol === 'https:' ? deps.https : deps.http).request(url.href, {method, headers}, res => {
                    response = res;
                    res.on('error', error => finish(networkError(error)));
                    res.on('aborted', () => finish(networkError({code: 'ECONNRESET'})));
                    if (file && res.statusCode === 200) {
                        const hash = deps.crypto.createHash('sha256');
                        output = deps.fs.createWriteStream(file, {flags: 'w'});
                        output.on('error', error => finish(failure(safeError(error, context.secrets))));
                        res.on('data', chunk => hash.update(chunk));
                        // Wait for close, so Windows permits renaming/removing the file.
                        output.on('close', () => {
                            if (!settled) finish(null, {status: res.statusCode, headers: res.headers, sha256: hash.digest('hex')});
                        });
                        res.pipe(output);
                        return;
                    }
                    const chunks = []; let size = 0;
                    res.on('data', chunk => {
                        size += chunk.length;
                        if (size > 8 * 1024 * 1024) finish(failure('Server response is too large'));
                        else chunks.push(chunk);
                    });
                    res.on('end', () => {
                        if (res.statusCode < 200 || res.statusCode >= 300) {
                            // Do not reflect server error bodies: a pair response may echo credentials.
                            const message = res.statusCode === 401 ? NOT_PAIRED : res.statusCode === 426 ?
                                updateMessage(context.serverUrl) : 'Server returned HTTP ' + res.statusCode;
                            finish(failure(message, {status: res.statusCode}));
                        } else finish(null, {status: res.statusCode, headers: res.headers,
                            text: Buffer.concat(chunks).toString('utf8')});
                    });
                });
                context.requests.add(req);
                req.on('error', error => finish(networkError(error)));
                req.setTimeout(route.startsWith('/api/ae/next?') ? 35000 : 30000,
                    () => finish(networkError({code: 'ETIMEDOUT'})));
                req.end(data);
            } catch (error) { finish(failure(safeError(error, context.secrets))); }
        });
    }

    function parseJson(text, label, secrets) {
        try { return JSON.parse(text); }
        catch (_) { throw failure(label + ': ' + redact(text, secrets).slice(0, 300)); }
    }

    function hostCall(deps, script, secrets) {
        return new Promise((resolve, reject) => {
            try { deps.evalScript(script, resolve); }
            catch (error) { reject(failure(safeError(error, secrets))); }
        }).then(raw => {
            const result = parseJson(String(raw), 'Invalid AE response', secrets);
            if (!result || typeof result !== 'object' || typeof result.ok !== 'boolean')
                throw failure('Invalid AE response: ' + redact(raw, secrets).slice(0, 300));
            if (!result.ok) throw failure(redact(result.error || 'AE call failed', secrets),
                {line: Number.isInteger(result.line) ? result.line : undefined});
            return result;
        });
    }

    function deviceInfo(info, deps) {
        return {ae_version: info.ae_version, extension_version: EXTENSION_VERSION,
            os: deps.os.platform() + ' ' + deps.os.release(), fonts: info.fonts};
    }

    function contextFor(options, deps) {
        const validated = validateServerUrl(options.serverUrl);
        if (!validated.ok) throw failure(validated.error);
        return {deps, serverUrl: validated.url, token: options.token, requests: new Set(),
            secrets: [options.token, options.code]};
    }

    async function pair(options, deps) {
        const context = contextFor(options, deps);
        const info = await hostCall(deps, 'kfInfo()', context.secrets);
        let response;
        try { response = await request(context, 'POST', '/api/ae/pair', {code: options.code, info: deviceInfo(info, deps)}); }
        catch (error) {
            if (error.status === 401) throw failure('Pairing code is invalid or expired');
            throw error;
        }
        // Never include a malformed pairing body (which contains the new token) in errors.
        let value;
        try { value = JSON.parse(response.text); } catch (_) { throw failure('Invalid pairing response'); }
        if (!value || typeof value.token !== 'string' || !value.token ||
            typeof value.device_id !== 'string' || !value.device_id) throw failure('Invalid pairing response');
        return {serverUrl: context.serverUrl, deviceId: value.device_id, token: value.token, serverName: value.server_name};
    }

    function validateProject(project) {
        if (typeof project !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(project)) throw failure('Invalid project cache name');
    }

    function assetCachePath(documentsDir, project, asset, path) {
        validateProject(project);
        if (!asset || typeof asset.name !== 'string' || /[\/\\\x00-\x1f:]/.test(asset.name) ||
            asset.name.includes('..') || !/\.(png|glb)$/.test(asset.name)) throw failure('Invalid asset name or extension');
        if (typeof asset.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(asset.sha256)) throw failure('Invalid asset SHA-256');
        const folder = path.resolve(documentsDir, 'Keepframe', project, 'assets');
        const destination = path.resolve(folder, asset.sha256 + path.extname(asset.name));
        if (path.dirname(destination) !== folder) throw failure('Invalid asset cache path');
        return destination;
    }

    function fileHash(file, deps) {
        return new Promise((resolve, reject) => {
            const hash = deps.crypto.createHash('sha256'), stream = deps.fs.createReadStream(file);
            stream.on('error', reject);
            stream.on('data', chunk => hash.update(chunk));
            stream.on('end', () => resolve(hash.digest('hex')));
        });
    }

    function createRunner(options, deps) {
        const context = contextFor(options, deps);
        let running = false, promise, wakeStop, stopped, abortHost, backoff = 0, terminal = false, outcome = false;
        const log = message => deps.log(redact(message, context.secrets));
        const status = (message, details) => {
            const safeDetails = details && JSON.parse(JSON.stringify(details,
                (key, value) => typeof value === 'string' ? redact(value, context.secrets) : value));
            if (running || terminal) deps.setStatus(redact(message, context.secrets), safeDetails);
        };
        const send = async (method, route, body, headers, file) => {
            const response = await request(context, method, route, body, headers, file);
            backoff = 0;
            return response;
        };
        function stopFor(error) {
            if (error.status !== 401 && error.status !== 426) return false;
            if (terminal) return true;
            terminal = true;
            if (abortHost) abortHost(error);
            stop();
            status(error.message, error.status === 426 ? {downloadUrl: context.serverUrl + '/ae/keepframe.zxp'} : {});
            log(error.message);
            return true;
        }
        async function pause(error) {
            const seconds = Math.min(30, Math.pow(2, backoff++));
            const message = 'Not connected: ' + safeError(error, context.secrets) + ' (retrying in ' + seconds + ' s)';
            status(message); log(message);
            await wait(seconds * 1000, stopped);
        }
        async function wait(ms, end) {
            const sleeping = deps.sleep(ms);
            try { await Promise.race([sleeping, end]); }
            finally { if (sleeping.cancel) sleeping.cancel(); }
        }

        async function sync(job, progress) {
            const prefix = '/api/ae/jobs/' + encodeURIComponent(job.id);
            const response = await send('GET', prefix + '/spec');
            const spec = parseJson(response.text, 'Invalid sync spec', context.secrets);
            if (!spec || !Array.isArray(spec.assets)) throw failure('Invalid sync spec assets');
            if (spec.project !== job.project || spec.scene !== job.scene || spec.version !== job.version)
                throw failure('Invalid project, scene or version in sync spec');
            validateProject(spec.project);
            // Validate every name before creating even the first directory.
            const paths = spec.assets.map(asset => assetCachePath(deps.documentsDir, spec.project, asset, deps.path));
            const assets = {};
            progress.total = spec.assets.length;
            for (let i = 0; i < paths.length; i++) {
                const asset = spec.assets[i], destination = paths[i], folder = deps.path.dirname(destination);
                progress.stage = 'downloading ' + asset.name; progress.done = i;
                status('Syncing ' + job.project + ' / ' + job.scene + ' ' + job.version + '…', {job, progress});
                let cached = false;
                try { cached = await fileHash(destination, deps) === asset.sha256; }
                catch (error) { if (error.code !== 'ENOENT') throw error; }
                if (!cached) {
                    await deps.fs.promises.mkdir(folder, {recursive: true});
                    const temporary = destination + '.' + deps.crypto.randomBytes(8).toString('hex') + '.part';
                    try {
                        const downloaded = await send('GET', prefix + '/assets/' + encodeURIComponent(asset.name),
                            undefined, undefined, temporary);
                        if (downloaded.sha256 !== asset.sha256 || downloaded.headers['x-keepframe-sha256'] !== asset.sha256)
                            throw failure('SHA-256 mismatch for ' + asset.name);
                        await deps.fs.promises.rename(temporary, destination);
                    } finally {
                        try { await deps.fs.promises.unlink(temporary); }
                        catch (error) { if (error.code !== 'ENOENT') throw error; }
                    }
                }
                assets[asset.name] = destination;
            }
            progress.stage = 'syncing'; progress.done = progress.total;
            status('Syncing ' + job.project + ' / ' + job.scene + ' ' + job.version + '…', {job, progress});
            if (!running) throw failure('Disconnected before sync');
            // JSON literals only, including ES3-safe escapes for line/paragraph separators.
            const literal = value => JSON.stringify(value).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
            return new Promise((resolve, reject) => {
                abortHost = reject;
                hostCall(deps, 'kfSync(' + literal(response.text) + ',' + literal(JSON.stringify(assets)) + ',' +
                    literal(job.params && job.params.force ? 'true' : 'false') + ')', context.secrets).then(resolve, reject);
            }).finally(() => { abortHost = undefined; });
        }

        async function runJob(job) {
            const prefix = '/api/ae/jobs/' + encodeURIComponent(job.id);
            const progress = {stage: 'syncing', done: 0, total: 0};
            let finished = false, finishHeartbeat;
            const ended = new Promise(resolve => { finishHeartbeat = resolve; });
            const message = 'Syncing ' + job.project + ' / ' + job.scene + ' ' + job.version + '…';
            status(message, {job, progress}); log(message);
            const heartbeat = async () => {
                while (!finished && !terminal) {
                    // Schedule independently of response latency, so a slow progress request cannot delay the next beat.
                    send('POST', prefix + '/progress', Object.assign({}, progress)).catch(error => {
                        if (finished || stopFor(error)) return;
                        status('Not connected: ' + safeError(error, context.secrets) + ' (retrying in 15 s)', {job, progress});
                        log('Progress failed: ' + safeError(error, context.secrets));
                    });
                    await wait(15000, ended);
                }
            };
            const beating = heartbeat();
            let payload;
            try {
                if (job.kind !== 'sync') throw failure(job.kind + ' is not supported by this extension version');
                payload = {ok: true, result: await sync(job, progress)};
            } catch (error) {
                payload = {ok: false, error: safeError(error, context.secrets)};
                if (Number.isInteger(error.line)) payload.line = error.line;
                stopFor(error);
            }
            try {
                // Keep the result until acknowledged; do not drop a completed AE sync on transient network loss.
                while (true) {
                    try { await send('POST', prefix + '/result', payload); break; }
                    catch (error) {
                        if (stopFor(error) || !running) return;
                        if (!error.network) throw error;
                        await pause(error);
                    }
                }
                if (terminal) return;
                outcome = true;
                let text;
                if (!payload.ok) text = 'Sync failed: ' + payload.error + (payload.line === undefined ? '' : ' (line ' + payload.line + ')');
                else if (payload.result.applied === false) text = 'AE layers were edited by hand: ' +
                    (payload.result.hand_edited || []).join(', ') + ' — overwrite from the web page';
                else {
                    const count = value => Array.isArray(value) ? value.length : Number(value) || 0;
                    text = 'Synced ' + job.version + ': ' + count(payload.result.created) + ' created, ' +
                        count(payload.result.updated) + ' updated, ' + count(payload.result.deleted) + ' deleted';
                }
                status(text, {job, finished: true}); log(text);
            } finally { finished = true; finishHeartbeat(); await beating; }
        }

        async function loop() {
            let announced = false;
            while (running) {
                try {
                    const info = await hostCall(deps, 'kfInfo()', context.secrets);
                    if (!running) break;
                    if (!announced) {
                        await send('POST', '/api/ae/info', {info: deviceInfo(info, deps)});
                        announced = true;
                        continue;
                    }
                    if (!outcome) status('Connected · ' + new URL(context.serverUrl).host);
                    const response = await send('GET', '/api/ae/next?wait=25', undefined, {
                        'X-Keepframe-Project': encodeURIComponent(info.project_name || ''),
                        'X-Keepframe-Project-Saved': info.project_saved ? '1' : '0'});
                    if (!running) break;
                    if (response.status === 204) continue;
                    const value = parseJson(response.text, 'Invalid job response', context.secrets);
                    if (response.status !== 200 || !value || !value.job || typeof value.job.id !== 'string')
                        throw failure('Invalid job response');
                    await runJob(value.job);
                } catch (error) {
                    if (!running || stopFor(error)) break;
                    outcome = false;
                    await pause(error);
                }
            }
        }
        function start() {
            if (promise) return promise;
            running = true;
            stopped = new Promise(resolve => { wakeStop = resolve; });
            promise = loop().finally(() => { running = false; });
            return promise;
        }
        function stop() {
            running = false;
            if (wakeStop) wakeStop();
            context.requests.forEach(req => req.destroy());
        }
        return {start, stop};
    }

    return {EXTENSION_VERSION, validateServerUrl, pair, createRunner, assetCachePath, createLog, redact};
}));
