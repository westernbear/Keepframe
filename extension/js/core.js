(function (root, factory) {
    const core = factory();
    if (typeof module === 'object' && module.exports) module.exports = core;
    if (typeof window !== 'undefined') root.KeepframeCore = core;
}(typeof window !== 'undefined' ? window : this, function () {
    'use strict';
    const EXTENSION_VERSION = '1.1.0';
    const HOST_BUILD = "dev";
    const HOST_TIMEOUT_MS = 10 * 60 * 1000;
    const FRAME_WAIT_MS = 60000;
    const RENDER_WAIT_MS = 10 * 60 * 1000;
    const NOT_PAIRED = 'Not paired: enter a new code from the Keepframe web page';

    function localAddress(address) {
        const ip = address.split('.').map(Number);
        if (/^\d+\.\d+\.\d+\.\d+$/.test(address) && ip.every(n => n >= 0 && n <= 255))
            return ip[0] === 127 || (ip[0] === 100 && ip[1] >= 64 && ip[1] <= 127);
        try {
            const host = new URL('http://[' + address + ']').hostname.toLowerCase();
            return host === '[::1]' || host.startsWith('[fd7a:115c:a1e0:');
        } catch (_) { return false; }
    }

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
            if (url.protocol === 'http:' && !(host === 'localhost' || host.endsWith('.ts.net') ||
                localAddress(host.replace(/^\[|\]$/g, ''))))
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
    const literal = value => JSON.stringify(value).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    function safeError(error, secrets) { return redact(error.message || error, secrets).slice(0, 1800); }
    function updateMessage(serverUrl) { return 'Update the Keepframe extension: ' + serverUrl + '/ae/keepframe.zxp'; }
    function networkError(error) {
        const reasons = {ECONNREFUSED: 'Server refused the connection', ENOTFOUND: 'Server host was not found',
            EAI_AGAIN: 'Server host lookup failed', ECONNRESET: 'Connection was interrupted',
            ETIMEDOUT: 'Request timed out', CERT_HAS_EXPIRED: 'Server certificate has expired',
            DEPTH_ZERO_SELF_SIGNED_CERT: 'Server certificate is not trusted'};
        return failure(reasons[error.code] || 'Network request failed', {network: true});
    }

    async function request(context, method, route, body, extraHeaders, file, upload) {
        const deps = context.deps;
        const url = new URL(context.serverUrl + route), host = url.host;
        if (url.protocol === 'http:') {
            const address = await new Promise((resolve, reject) => {
                deps.dns.lookup(url.hostname.replace(/^\[|\]$/g, ''), {}, (error, address) => {
                    if (error) reject(networkError(error)); else resolve(address);
                });
            });
            if (!localAddress(address)) throw failure(
                'Not connected: http is only allowed to this machine or your Tailscale network (got ' + address + ')');
            url.hostname = address.includes(':') ? '[' + address + ']' : address;
        }
        return new Promise((resolve, reject) => {
            const headers = Object.assign({'X-Keepframe-Extension': EXTENSION_VERSION}, extraHeaders);
            if (url.protocol === 'http:') headers.Host = host;
            if (context.token) headers.Authorization = 'Bearer ' + context.token;
            const data = body === undefined ? undefined : JSON.stringify(body);
            if (data !== undefined) {
                headers['Content-Type'] = 'application/json';
                headers['Content-Length'] = Buffer.byteLength(data);
            }
            if (upload) {
                headers['Content-Type'] = 'image/png';
                headers['Content-Length'] = upload.size;
            }
            let req, input, output, response, settled = false, inputClosed = false;
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
                    } else if (input && !inputClosed) {
                        input.once('close', () => reject(error));
                        input.destroy();
                    } else reject(error);
                } else if (input && !inputClosed) {
                    input.once('close', () => resolve(value));
                    input.destroy();
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
                        let received = 0;
                        output = deps.fs.createWriteStream(file.path, {flags: 'w'});
                        output.on('error', error => finish(failure(safeError(error, context.secrets))));
                        res.on('data', chunk => {
                            if (settled) return;
                            received += chunk.length;
                            if (received > file.bytes) return finish(failure('Asset exceeds declared byte count (' + file.bytes + ')'));
                            hash.update(chunk);
                            if (!output.write(chunk)) res.pause();
                        });
                        output.on('drain', () => { if (!settled) res.resume(); });
                        res.on('end', () => {
                            if (settled) return;
                            if (received !== file.bytes) return finish(failure(
                                'Asset byte count mismatch (expected ' + file.bytes + ', received ' + received + ')'));
                            output.end();
                        });
                        // Wait for close, so Windows permits renaming/removing the file.
                        output.on('close', () => {
                            if (!settled) finish(null, {status: res.statusCode, headers: res.headers, sha256: hash.digest('hex')});
                        });
                        return;
                    }
                    const chunks = []; let size = 0;
                    res.on('data', chunk => {
                        if (settled) return;
                        size += chunk.length;
                        if (size > 32 * 1024 * 1024) {
                            chunks.length = 0;
                            finish(failure('Server JSON response exceeds 32 MiB'));
                        }
                        else chunks.push(chunk);
                    });
                    res.on('end', () => {
                        if (settled) return;
                        if (res.statusCode < 200 || res.statusCode >= 300) {
                            let message = res.statusCode === 401 ? NOT_PAIRED : res.statusCode === 426 ?
                                updateMessage(context.serverUrl) : 'Server returned HTTP ' + res.statusCode;
                            // Pair responses may echo credentials; only authenticated work routes expose diagnostics.
                            if (res.statusCode !== 401 && res.statusCode !== 426 &&
                                (route.startsWith('/api/ae/jobs/') || /^\/api\/ae\/(next|info)(?:\?|$)/.test(route))) {
                                try {
                                    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
                                    if (typeof body.error === 'string' && body.error.trim())
                                        message = safeError({message: body.error}, context.secrets);
                                } catch (_) {}
                            }
                            finish(failure(message, {status: res.statusCode}));
                        } else finish(null, {status: res.statusCode, headers: res.headers,
                            text: Buffer.concat(chunks).toString('utf8')});
                    });
                });
                context.requests.add(req);
                req.on('error', error => finish(networkError(error)));
                req.setTimeout(route.startsWith('/api/ae/next?') ? 35000 : 30000,
                    () => finish(networkError({code: 'ETIMEDOUT'})));
                if (upload) {
                    input = deps.fs.createReadStream(upload.path, {start: 0, end: upload.size - 1});
                    input.on('close', () => { inputClosed = true; });
                    input.on('error', error => finish(failure('Could not read frame upload (' + (error.code || 'unknown error') + ')')));
                    let read = 0;
                    input.on('data', chunk => { read += chunk.length; });
                    input.on('end', () => {
                        if (read !== upload.size) finish(failure('Frame upload shrank (expected ' +
                            upload.size + ' bytes, read ' + read + ')'));
                    });
                    input.pipe(req);
                } else req.end(data);
            } catch (error) { finish(failure(upload ? 'Could not upload frame (' + (error.code || 'unknown error') + ')' :
                safeError(error, context.secrets))); }
        });
    }

    function parseJson(text, label, secrets) {
        try { return JSON.parse(text); }
        catch (_) { throw failure(label + ': ' + redact(text, secrets).slice(0, 300)); }
    }

    function hostCall(context, script) {
        const deps = context.deps, secrets = context.secrets;
        const timeout = script.startsWith('kfInfo(') ? 30000 : HOST_TIMEOUT_MS;
        return new Promise((resolve, reject) => {
            let settled = false;
            const finish = (error, raw) => {
                if (settled) return;
                settled = true;
                deps.clearTimeout(timer);
                context.abortHost = undefined;
                if (error) reject(error); else resolve(raw);
            };
            const timer = deps.setTimeout(() => finish(failure('After Effects did not finish within ' +
                (timeout / 60000) + ' min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.',
                {hostTimeout: true})), timeout);
            context.abortHost = error => finish(error);
            try { deps.evalScript(script, raw => finish(null, raw)); }
            catch (error) { finish(failure(safeError(error, secrets))); }
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
            os: deps.os.platform() + ' ' + deps.os.release(), fonts: info.fonts.slice(0, 5000),
            host_build: info.host_build, panel_build: HOST_BUILD};
    }

    function contextFor(options, deps) {
        const validated = validateServerUrl(options.serverUrl);
        if (!validated.ok) throw failure(validated.error);
        return {deps, serverUrl: validated.url, token: options.token, requests: new Set(),
            secrets: [options.token, options.code]};
    }

    async function pair(options, deps) {
        const context = contextFor(options, deps);
        const info = await hostCall(context, 'kfInfo("true")');
        let response;
        try { response = await request(context, 'POST', '/api/ae/pair', {code: options.code, info: deviceInfo(info, deps),
            previous_device_id: options.previousDeviceId}); }
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
            asset.name.includes('..') || !/\.(png|jpg|webp|glb|mp4|mov)$/.test(asset.name)) throw failure('Invalid asset name or extension');
        if (typeof asset.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(asset.sha256)) throw failure('Invalid asset SHA-256');
        if (!Number.isSafeInteger(asset.bytes) || asset.bytes < 0) throw failure('Invalid asset byte count');
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
        let running = false, promise, wakeStop, stopped, backoff = 0, terminal = false, outcome = false;
        const log = message => deps.log(redact(message, context.secrets));
        const status = (message, details) => {
            const safeDetails = details && JSON.parse(JSON.stringify(details,
                (key, value) => typeof value === 'string' ? redact(value, context.secrets) : value));
            if (running || terminal) deps.setStatus(redact(message, context.secrets), safeDetails);
        };
        const send = async (method, route, body, headers, file, upload) => {
            const response = await request(context, method, route, body, headers, file, upload);
            backoff = 0;
            return response;
        };
        function stopFor(error) {
            if (error.status !== 401 && error.status !== 426) return false;
            if (terminal) return true;
            terminal = true;
            stop(error);
            status(error.message, error.status === 426 ? {downloadUrl: context.serverUrl + '/ae/keepframe.zxp'} : {});
            log(error.message);
            return true;
        }
        async function pause(error) {
            const seconds = Math.min(30, Math.pow(2, backoff++));
            const message = 'Not connected: ' + safeError(error, context.secrets).replace(/^Not connected: /, '') +
                ' (retrying in ' + seconds + ' s)';
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
                            undefined, undefined, {path: temporary, bytes: asset.bytes});
                        if (downloaded.sha256 !== asset.sha256 || downloaded.headers['x-keepframe-sha256'] !== asset.sha256)
                            throw failure('SHA-256 mismatch for ' + asset.name);
                        await deps.fs.promises.rename(temporary, destination);
                    } finally {
                        try { await deps.fs.promises.unlink(temporary); }
                        catch (error) {
                            if (error.code !== 'ENOENT') log('Could not remove temporary asset file: ' + (error.code || 'unknown error'));
                        }
                    }
                }
                assets[asset.name] = destination;
            }
            progress.stage = 'syncing'; progress.done = progress.total;
            status('Syncing ' + job.project + ' / ' + job.scene + ' ' + job.version + '…', {job, progress});
            if (!running) throw failure('Disconnected before sync');
            // JSON literals only, including ES3-safe escapes for line/paragraph separators.
            const result = await hostCall(context, 'kfSync(' + literal(response.text) + ',' + literal(JSON.stringify(assets)) + ',' +
                literal(job.params && job.params.force ? 'true' : 'false') + ')');
            if (result.timings) log('Sync timings: ' + ['validate', 'read', 'hash', 'assets', 'write', 'order', 'total']
                .map(stage => stage + ' ' + result.timings[stage] + ' ms').join(', '));
            return result;
        }

        async function renderFrames(job, progress) {
            const params = job.params, path = deps.path;
            if (!/^j_[0-9a-f]{16}$/.test(job.id) || !params || typeof params.tag !== 'string' ||
                !Array.isArray(params.frames) || !params.frames.length || params.frames.length > 16 ||
                params.frames.some(f => !Number.isSafeInteger(f) || f < 0 || f > 999999) ||
                new Set(params.frames).size !== params.frames.length) throw failure('Invalid render job');
            const folder = path.resolve(deps.os.tmpdir(), 'keepframe-' + job.id);
            const prefix = '/api/ae/jobs/' + encodeURIComponent(job.id);
            const invalidFrame = reason => {
                if (reason) log('Invalid frame from AE: ' + reason);
                throw failure('Invalid frame from AE');
            };
            const realpath = file => new Promise((resolve, reject) => {
                deps.fs.realpath.native(file, (error, resolved) => error ? reject(error) : resolve(resolved));
            });
            const normalize = file => (deps.platform || process.platform) === 'win32' ? file.toLowerCase() : file;
            const frameNotReady = error => ['ENOENT', 'EBUSY', 'EPERM', 'EACCES'].includes(error.code);
            const frameStat = async file => {
                let stat;
                try { stat = await deps.fs.promises.lstat(file); }
                catch (error) { if (frameNotReady(error)) return; invalidFrame('not a regular file'); }
                if (stat.isSymbolicLink()) invalidFrame('symlink');
                if (!stat.isFile()) invalidFrame('not a regular file');
                if (stat.size > 25 * 1024 * 1024) invalidFrame('frame larger than 25 MB');
                return stat;
            };
            try {
                if (!running) throw failure('Disconnected before render');
                const rendered = await hostCall(context, 'kfRender(' + literal(JSON.stringify({
                    job: job.id, tag: params.tag, frames: params.frames})) + ')');
                const renderDeadline = deps.now() + RENDER_WAIT_MS;
                if (!Array.isArray(rendered.frames) || rendered.frames.length !== params.frames.length)
                    throw failure('Invalid frame from AE');
                const seen = new Set(), files = [];
                let canonicalFolder;
                try {
                    const stat = await deps.fs.promises.lstat(folder);
                    if (stat.isSymbolicLink()) invalidFrame('symlink');
                    if (!stat.isDirectory()) invalidFrame('not a regular file');
                    canonicalFolder = normalize(await realpath(folder));
                } catch (_) { throw failure('Invalid frame from AE'); }
                // Validate the entire list before opening any upload stream.
                for (const frame of rendered.frames) {
                    const name = frame && 'frame_' + String(frame.frame).padStart(4, '0') + '.png';
                    if (!frame || !params.frames.includes(frame.frame) || seen.has(frame.frame) ||
                        typeof frame.path !== 'string' || !path.isAbsolute(frame.path)) throw failure('Invalid frame from AE');
                    let parent;
                    try {
                        parent = normalize(await realpath(path.dirname(frame.path)));
                    } catch (_) { invalidFrame('frame outside the job folder'); }
                    if (parent !== canonicalFolder) invalidFrame('frame outside the job folder');
                    if (path.basename(frame.path) !== name) invalidFrame();
                    await frameStat(frame.path);
                    seen.add(frame.frame);
                    files.push({frame: frame.frame, path: frame.path, name});
                }
                // AE can finish writing only after evalScript returns. Poll on the panel's event loop.
                for (let i = 0; i < files.length; i++) {
                    const file = files[i], deadline = Math.min(renderDeadline, deps.now() + FRAME_WAIT_MS);
                    let previous = 0;
                    while (deps.now() < deadline) {
                        if (!running) throw failure('Disconnected before render');
                        const stat = await frameStat(file.path);
                        if (stat && stat.size >= 20 && stat.size === previous) {
                            let handle;
                            try {
                                handle = await deps.fs.promises.open(file.path, 'r');
                                const head = Buffer.alloc(8), tail = Buffer.alloc(12);
                                const first = await handle.read(head, 0, 8, 0);
                                const last = await handle.read(tail, 0, 12, stat.size - 12);
                                if (first.bytesRead === 8 && last.bytesRead === 12 &&
                                    head.equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) &&
                                    tail.equals(Buffer.from([0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])) &&
                                    (await handle.stat()).size === stat.size && deps.now() < deadline) file.size = stat.size;
                            } catch (error) { if (!frameNotReady(error)) invalidFrame('not a regular file'); }
                            finally { if (handle) await handle.close(); }
                            if (file.size !== undefined) break;
                        }
                        previous = stat ? stat.size : 0;
                        await wait(Math.min(250, Math.max(0, deadline - deps.now())), stopped);
                    }
                    if (file.size === undefined) throw failure('AE did not write frame ' + file.frame + ' within 60 s');
                    progress.done = i + 1;
                    status('Rendering ' + files.length + ' frames of ' + job.project + ' / ' + job.scene + ' ' + job.version + '…', {job, progress});
                    await send('POST', prefix + '/progress', Object.assign({}, progress));
                }
                progress.stage = 'uploading'; progress.done = 0; progress.total = files.length;
                for (let i = 0; i < files.length; i++) {
                    if (!running) throw failure('Disconnected before upload');
                    status('Uploading frame ' + (i + 1) + '/' + files.length + '…', {job, progress});
                    const stat = await frameStat(files[i].path);
                    if (!stat || stat.size < files[i].size) throw failure('Frame upload shrank before upload');
                    await send('PUT', prefix + '/files/' + files[i].name,
                        undefined, undefined, undefined, files[i]);
                    progress.done = i + 1;
                    await send('POST', prefix + '/progress', Object.assign({}, progress));
                }
                return {frames: params.frames};
            } finally {
                try {
                    if (deps.fs.promises.rm) {
                        await deps.fs.promises.rm(folder, {recursive: true, force: true, maxRetries: 3, retryDelay: 200});
                    } else {
                        for (let attempt = 0; ; attempt++) {
                            try { await deps.fs.promises.rmdir(folder, {recursive: true}); break; }
                            catch (error) {
                                if (attempt === 3 || !['EBUSY', 'EPERM', 'EACCES', 'ENOTEMPTY'].includes(error.code)) throw error;
                                await deps.sleep(200);
                            }
                        }
                    }
                }
                catch (error) {
                    if (error.code !== 'ENOENT') {
                        const message = 'Could not remove temporary frame folder (' + (error.code || 'unknown error') + ')';
                        log(message);
                    }
                }
            }
        }

        async function runJob(job, mismatch) {
            const prefix = '/api/ae/jobs/' + encodeURIComponent(job.id);
            const rendering = job.kind === 'render_frames';
            const total = rendering && job.params && Array.isArray(job.params.frames) ? job.params.frames.length : 0;
            const progress = {stage: rendering ? 'rendering' : 'syncing', done: 0, total};
            let finished = false, finishHeartbeat;
            const ended = new Promise(resolve => { finishHeartbeat = resolve; });
            const message = mismatch || (rendering ? 'Rendering ' + total + ' frames of ' : 'Syncing ') +
                job.project + ' / ' + job.scene + ' ' + job.version + '…';
            status(message, {job, progress}); log(message);
            const heartbeat = async () => {
                while (!finished && !terminal) {
                    // Schedule independently of response latency, so a slow progress request cannot delay the next beat.
                    request(context, 'POST', prefix + '/progress', Object.assign({}, progress)).catch(error => {
                        if (finished || stopFor(error)) return;
                        status('Not connected: ' + safeError(error, context.secrets) + ' (retrying in 15 s)', {job, progress});
                        log('Progress failed: ' + safeError(error, context.secrets));
                    });
                    await wait(15000, ended);
                }
            };
            const beating = mismatch ? Promise.resolve() : heartbeat();
            let payload;
            try {
                if (mismatch) throw failure(mismatch);
                const handlers = {sync, render_frames: renderFrames};
                if (!Object.prototype.hasOwnProperty.call(handlers, job.kind))
                    throw failure(job.kind + ' is not supported by this extension version');
                payload = {ok: true, result: await handlers[job.kind](job, progress)};
            } catch (error) {
                payload = {ok: false, error: safeError(error, context.secrets)};
                if (Number.isInteger(error.line)) payload.line = error.line;
                stopFor(error);
                if (error.hostTimeout || !running) { finished = true; finishHeartbeat(); await beating; }
            }
            try {
                // Keep the result until acknowledged; do not drop a completed AE sync on transient network loss.
                while (true) {
                    try { await send('POST', prefix + '/result', payload); break; }
                    catch (error) {
                        if (stopFor(error) || !running) return;
                        if (!error.network && error.status !== 429 && !(error.status >= 500 && error.status < 600)) {
                            outcome = true;
                            const message = error.status === 409 ? 'Result not posted: job already ended (HTTP 409)' :
                                'Result not posted: ' + safeError(error, context.secrets);
                            status(message, {job, finished: true}); log(message);
                            return;
                        }
                        await pause(error);
                    }
                }
                if (terminal) return;
                outcome = true;
                let text;
                if (!payload.ok) text = mismatch || (rendering ? 'Render failed: ' : 'Sync failed: ') + payload.error +
                    (payload.line === undefined ? '' : ' (line ' + payload.line + ')');
                else if (rendering) text = 'Rendered ' + payload.result.frames.length + ' frames — Keepframe is comparing them';
                else if (payload.result.applied === false) {
                    const interrupted = payload.result.interrupted || [];
                    const edited = (payload.result.hand_edited || []).filter(id => !interrupted.includes(id));
                    const messages = [];
                    if (interrupted.length) messages.push('a previous sync was interrupted — overwrite to finish it: ' + interrupted.join(', '));
                    if (edited.length) messages.push('AE layers were edited by hand: ' + edited.join(', ') + ' — overwrite from the web page');
                    text = messages.join('\n');
                }
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
                    const info = await hostCall(context, announced ? 'kfInfo("false")' : 'kfInfo("true")');
                    if (!running) break;
                    const mismatch = info.host_build === HOST_BUILD ? '' :
                        'The AE script (build ' + (info.host_build || 'unknown') + ') does not match the panel (build ' +
                        HOST_BUILD + ') — fully quit and restart After Effects';
                    if (mismatch) status(mismatch);
                    if (!announced) {
                        await send('POST', '/api/ae/info', {info: deviceInfo(info, deps)});
                        announced = true;
                        continue;
                    }
                    if (!outcome && !mismatch) status('Connected · ' + new URL(context.serverUrl).host);
                    const response = await send('GET', '/api/ae/next?wait=25', undefined, {
                        'X-Keepframe-Project': encodeURIComponent(info.project_name || ''),
                        'X-Keepframe-Project-Saved': info.project_saved ? '1' : '0'});
                    if (!running) break;
                    if (response.status === 204) continue;
                    const value = parseJson(response.text, 'Invalid job response', context.secrets);
                    if (response.status !== 200 || !value || !value.job || typeof value.job.id !== 'string')
                        throw failure('Invalid job response');
                    await runJob(value.job, mismatch);
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
        function stop(error) {
            running = false;
            if (context.abortHost) context.abortHost(error || failure('Disconnected before After Effects responded'));
            if (wakeStop) wakeStop();
            context.requests.forEach(req => req.destroy());
        }
        return {start, stop};
    }

    return {EXTENSION_VERSION, HOST_BUILD, HOST_TIMEOUT_MS, validateServerUrl, pair, createRunner, assetCachePath, createLog, redact};
}));
