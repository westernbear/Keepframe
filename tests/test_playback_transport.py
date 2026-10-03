"""Exercise async transport/cache behavior without downloading another browser."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_original_only_cache_and_stopped_transport_reject_late_frame(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    source = Path("keepframe/web/static/js/playback.js").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    # The module's shared URL builder is also part of the versioning contract.
    api = Path("keepframe/web/static/js/api.js").read_text()
    src = api[api.index('function reviewFrameUrl('):api.index('function reviewAssetUrl(')]
    script = tmp_path / "playback.mjs"
    script.write_text('''import assert from 'node:assert/strict';
const urls = [];
function withQuery(url, params) { return url + '?' + new URLSearchParams(params); }
class FakeImage {
  complete = true; naturalWidth = 640;
  addEventListener() {} decode() { return Promise.resolve(); }
  set src(url) { urls.push(url); }
}
globalThis.Image = FakeImage;
let callback;
globalThis.requestAnimationFrame = fn => { callback = fn; return 1; };
globalThis.cancelAnimationFrame = () => {};
''' + src + source + '''
let version = 'v1', sceneId = 's';
const previews = createPreviewCache({project:'p',scene:() => sceneId,version:() => version,kinds:['orig']});
previews.prefetch(0, 8);
assert.equal(await previews.wait(0), true);
assert.ok(urls.length > 1 && urls.every(url => url.startsWith('/frame/orig/') && url.includes('v=v1')));
version = 'v2';
await previews.wait(0);
assert.ok(urls.at(-1).includes('v=v2'));
sceneId = 's2';
await previews.wait(0);
assert.ok(urls.at(-1).includes('scene=s2'));
let frame = 0, finish;
const transport = createFrameTransport({
 getFrame:()=>frame, setFrameIndex:f=>frame=f, getFps:()=>30, getFrameCount:()=>10,
 prefetch:()=>{}, isReady:()=>false, wait:()=>new Promise(r=>finish=r), showFrame:()=>{}, canPlay:()=>true
});
transport.setPlaying(true); callback(1); callback(50);
transport.setPlaying(false); frame = 5; transport.setPlaying(true);
finish(true); await Promise.resolve();
assert.equal(frame, 5, 'stale wait must not reset the frame after a seek/restart');
transport.setPlaying(false);
console.log('transport/cache assertions passed');
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
