(function () {
    'use strict';
    const strings = {
        en: {
            server: 'Server URL', code: 'Pairing code', pair: 'Pair', disconnect: 'Disconnect',
            status: 'Status', current: 'Current job', log: 'Log', copy: 'Copy log', version: 'Extension',
            build: '{version} · build {build}',
            idle: 'No current job', disconnected: 'Not connected: enter a code from the Keepframe web page',
            pairing: 'Pairing…', paired: 'Paired with {host}', forgotten: 'Disconnected: stored pairing forgotten',
            unavailable: 'Open this panel in After Effects with CEP enabled', copied: 'Log copied',
            copyFailed: 'Could not copy the log', storageFailed: 'Could not save settings: pair again',
            download: 'Download extension', connected: 'Connected · {host}',
            syncing: 'Syncing {project} / {scene} {version}…',
            synced: 'Synced {version}: {created} created, {updated} updated, {deleted} deleted',
            edited: 'AE layers were edited by hand: {ids} — overwrite from the web page',
            syncInterrupted: 'a previous sync was interrupted — overwrite to finish it: {ids}',
            retry: 'Not connected: {reason} (retrying in {seconds} s)', failed: 'Sync failed: {reason}',
            hostTimeout: 'After Effects did not finish within {minutes} min. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again.',
            notPaired: 'Not paired: enter a new code from the Keepframe web page',
            update: 'Update the Keepframe extension: {url}', downloading: 'Downloading {name}', stageSync: 'Syncing',
            invalidCode: 'Pairing code is invalid or expired', invalidUrl: 'Enter a server URL',
            urlShape: 'Use an HTTP(S) server URL without a path or query', urlCredentials: 'URL credentials are not allowed',
            httpPolicy: 'HTTP requires loopback or Tailscale; otherwise use HTTPS', urlInvalid: 'Invalid server URL',
            network: 'Network request failed', refused: 'Server refused the connection', notFound: 'Server host was not found',
            lookup: 'Server host lookup failed', interrupted: 'Connection was interrupted', timedOut: 'Request timed out',
            expired: 'Server certificate has expired', untrusted: 'Server certificate is not trusted'
        },
        ko: {
            server: '서버 URL', code: '페어링 코드', pair: '페어링', disconnect: '연결 해제',
            status: '연결 상태', current: '현재 작업', log: '로그', copy: '로그 복사', version: '확장',
            build: '{version} · 빌드 {build}',
            idle: '진행 중인 작업 없음', disconnected: '연결 안 됨: Keepframe 웹 페이지의 코드를 입력하세요',
            pairing: '페어링 중…', paired: '{host}에 페어링됨', forgotten: '연결 해제됨: 저장된 페어링 삭제',
            unavailable: 'CEP가 활성화된 After Effects에서 이 패널을 여세요', copied: '로그 복사됨',
            copyFailed: '로그를 복사할 수 없습니다', storageFailed: '설정을 저장할 수 없습니다: 다시 페어링하세요',
            download: '확장 다운로드', connected: '연결됨 · {host}',
            syncing: '{project} / {scene} {version} 동기화 중…',
            synced: '{version} 동기화됨: 생성 {created}개, 업데이트 {updated}개, 삭제 {deleted}개',
            edited: 'AE 레이어가 수동으로 수정되었습니다: {ids} — 웹 페이지에서 덮어쓰세요',
            syncInterrupted: '이전 동기화가 중단되었습니다 — 덮어써서 완료하세요: {ids}',
            retry: '연결 안 됨: {reason} ({seconds}초 후 재시도)', failed: '동기화 실패: {reason}',
            hostTimeout: 'After Effects가 {minutes}분 안에 끝내지 못했습니다. 큰 장면을 아직 처리 중이거나 대화상자를 기다리는 중일 수 있습니다. AE가 응답하면 다시 보내세요.',
            notPaired: '페어링 안 됨: Keepframe 웹 페이지의 새 코드를 입력하세요',
            update: 'Keepframe 확장을 업데이트하세요: {url}', downloading: '{name} 다운로드 중', stageSync: '동기화 중',
            invalidCode: '페어링 코드가 올바르지 않거나 만료되었습니다', invalidUrl: '서버 URL을 입력하세요',
            urlShape: '경로나 쿼리 없이 HTTP(S) 서버 URL을 입력하세요', urlCredentials: 'URL에 인증 정보를 넣을 수 없습니다',
            httpPolicy: 'HTTP는 루프백 또는 Tailscale에서만 사용하세요. 그 외에는 HTTPS를 사용하세요', urlInvalid: '올바르지 않은 서버 URL',
            network: '네트워크 요청 실패', refused: '서버가 연결을 거부했습니다', notFound: '서버 호스트를 찾을 수 없습니다',
            lookup: '서버 호스트 조회 실패', interrupted: '연결이 끊겼습니다', timedOut: '요청 시간 초과',
            expired: '서버 인증서가 만료되었습니다', untrusted: '신뢰할 수 없는 서버 인증서'
        }
    };
    const cep = window.__adobe_cep__, core = window.KeepframeCore;
    let locale = 'en', environmentError = false;
    try {
        const raw = cep.getHostEnvironment();
        const environment = typeof raw === 'string' ? JSON.parse(raw) : raw;
        if ((environment.appUILocale || '').toLowerCase().startsWith('ko')) locale = 'ko';
    } catch (_) { environmentError = true; }
    const table = strings[locale];
    const el = id => document.getElementById(id);
    const t = (key, values) => table[key].replace(/\{(\w+)\}/g, (match, name) => values[name]);
    document.documentElement.lang = locale;
    document.querySelectorAll('[data-i18n]').forEach(node => { node.textContent = table[node.dataset.i18n]; });
    el('version').textContent = t('build', {version: core.EXTENSION_VERSION, build: core.HOST_BUILD});
    el('current-job').textContent = table.idle;
    el('status').textContent = table.disconnected;
    const secrets = [], ring = core.createLog(Date.now, secrets);
    let activeRunner, generation = 0, token = '';

    function translate(message) {
        if (message.includes('\n')) return message.split('\n').map(translate).join('\n');
        const key = Object.keys(strings.en).find(name => strings.en[name] === message);
        if (key) return table[key];
        let match;
        if ((match = /^Connected · (.*)$/.exec(message))) return t('connected', {host: match[1]});
        if ((match = /^Syncing (.*) \/ (.*) (.*)…$/.exec(message)))
            return t('syncing', {project: match[1], scene: match[2], version: match[3]});
        if ((match = /^Synced (.*): (\d+) created, (\d+) updated, (\d+) deleted$/.exec(message)))
            return t('synced', {version: match[1], created: match[2], updated: match[3], deleted: match[4]});
        if ((match = /^AE layers were edited by hand: (.*) — overwrite from the web page$/.exec(message)))
            return t('edited', {ids: match[1]});
        if ((match = /^a previous sync was interrupted — overwrite to finish it: (.*)$/.exec(message)))
            return t('syncInterrupted', {ids: match[1]});
        if ((match = /^Not connected: (.*) \(retrying in (\d+) s\)$/.exec(message)))
            return t('retry', {reason: translate(match[1]), seconds: match[2]});
        if ((match = /^After Effects did not finish within (\d+(?:\.\d+)?) min\. It may still be working on a large scene or waiting for a dialog — wait until AE responds, then send again\.$/.exec(message)))
            return t('hostTimeout', {minutes: match[1]});
        if (message.startsWith('Sync failed: ')) return t('failed', {reason: translate(message.slice(13))});
        if (message.startsWith('Update the Keepframe extension: '))
            return t('update', {url: message.slice('Update the Keepframe extension: '.length)});
        return message; // AE and server diagnostics retain their original wording.
    }
    function log(message) {
        ring.add(translate(message));
        el('log').value = ring.text();
        el('log').scrollTop = el('log').scrollHeight;
    }
    function setStatus(message, details) {
        el('status').textContent = translate(core.redact(message, secrets));
        const update = el('update-link');
        const url = details && details.downloadUrl;
        update.hidden = !url;
        if (url) update.href = url;
        if (details && details.job) {
            const job = details.job, progress = details.progress;
            let text = job.project + ' / ' + job.scene + ' · ' + job.version;
            if (progress) {
                const stage = progress.stage.startsWith('downloading ') ?
                    t('downloading', {name: progress.stage.slice(12)}) : table.stageSync;
                text += '\n' + stage + ' · ' + progress.done + '/' + progress.total;
            }
            el('current-job').textContent = core.redact(text, secrets);
        }
    }
    function showError(error) {
        const message = core.redact(error.message || error, secrets);
        const validated = core.validateServerUrl(el('server-url').value);
        const details = message.startsWith('Update the Keepframe extension: ') && validated.ok ?
            {downloadUrl: validated.url + '/ae/keepframe.zxp'} : undefined;
        setStatus(message, details); log(message);
    }
    let deps;
    try {
        if (environmentError) throw new Error(table.unavailable);
        const os = require('os'), path = require('path');
        deps = {http: require('http'), https: require('https'), dns: require('dns'), fs: require('fs'), path,
            crypto: require('crypto'), os, documentsDir: path.join(os.homedir(), 'Documents'),
            evalScript: (script, callback) => cep.evalScript(script, callback), now: Date.now, log, setStatus,
            setTimeout, clearTimeout,
            sleep: ms => {
                let timer;
                const promise = new Promise(resolve => { timer = setTimeout(resolve, ms); });
                promise.cancel = () => clearTimeout(timer);
                return promise;
            }};
    } catch (_) {
        setStatus(table.unavailable);
        el('pair').disabled = true; el('disconnect').disabled = true;
        return;
    }

    function connect(serverUrl) {
        const instance = core.createRunner({serverUrl, token}, Object.assign({}, deps, {
            setStatus: (message, details) => { if (activeRunner === instance) setStatus(message, details); }}));
        activeRunner = instance;
        instance.start().catch(error => { if (activeRunner === instance) showError(error); });
    }
    function stop() {
        const previous = activeRunner;
        activeRunner = undefined;
        if (previous) previous.stop();
    }
    el('settings').addEventListener('submit', async event => {
        event.preventDefault();
        const current = ++generation;
        stop();
        const code = el('pairing-code').value.trim(), serverUrl = el('server-url').value;
        secrets.push(code);
        el('pair').disabled = true;
        setStatus(table.pairing);
        try {
            if (current !== generation) return;
            const paired = await core.pair({serverUrl, code, previousDeviceId: localStorage.getItem('deviceId') || undefined}, deps);
            if (current !== generation) return;
            secrets.push(paired.token);
            try {
                localStorage.setItem('serverUrl', paired.serverUrl);
                localStorage.setItem('deviceId', paired.deviceId);
                localStorage.setItem('token', paired.token);
            } catch (_) { throw new Error(table.storageFailed); }
            token = paired.token;
            el('server-url').value = paired.serverUrl;
            el('pairing-code').value = '';
            log(t('paired', {host: new URL(paired.serverUrl).host}));
            connect(paired.serverUrl);
        } catch (error) { if (current === generation) showError(error); }
        finally { if (current === generation) el('pair').disabled = false; }
    });
    el('disconnect').addEventListener('click', () => {
        generation++;
        stop(); token = '';
        el('pairing-code').value = ''; el('pair').disabled = false;
        el('current-job').textContent = table.idle;
        try { localStorage.removeItem('token'); localStorage.removeItem('deviceId'); }
        catch (_) { showError(new Error(table.storageFailed)); return; }
        setStatus(table.forgotten); log(table.forgotten);
    });
    el('copy-log').addEventListener('click', () => {
        try {
            el('log').focus(); el('log').select();
            if (!document.execCommand('copy')) throw new Error(table.copyFailed);
            el('copy-notice').textContent = table.copied;
        } catch (_) { el('copy-notice').textContent = table.copyFailed; }
    });
    window.addEventListener('beforeunload', stop);
    try {
        const serverUrl = localStorage.getItem('serverUrl') || '';
        el('server-url').value = serverUrl;
        token = localStorage.getItem('token') || '';
        if (token) { secrets.push(token); connect(serverUrl); }
    } catch (error) { showError(error); }
}());
