(async () => {
  const delay = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (fn, label) => { for (let i=0;i<150;i++) { if(fn()) return; await delay(20); } throw new Error(label); };
  const assert = (condition, label) => { if(!condition) throw new Error(label); };
  const originalFetch = window.fetch;
  const observed = [];
  let fail = false;
  window.fetch = async (input, options={}) => {
    const url = new URL(input, location.origin);
    if (url.pathname === '/api/analysis-overlay') {
      observed.push(url.search);
      // Intentionally ignore abort and return old responses after a new request.
      const opts = {...options}; delete opts.signal;
      const response = await originalFetch(input, opts);
      await delay(url.searchParams.get('frame') === '1' ? 450 : 25);
      if(fail) throw new Error('injected overlay outage');
      return response;
    }
    if (url.pathname === '/api/state' && url.searchParams.get('v') === 'v1') {
      const response = await originalFetch(input, options);
      await delay(450);
      return response;
    }
    return originalFetch(input, options);
  };
  try {
    document.querySelector('#step-fwd').click();
    await delay(100);
    document.querySelector('#step-fwd').click();
    await waitFor(() => document.querySelector('#orig-overlay').dataset.frame === '2' && document.querySelector('#orig-overlay path'), 'frame 2 overlay');
    await delay(500);
    assert(document.querySelector('#orig-overlay').dataset.frame === '2', 'stale frame response won');
    const select = document.querySelector('#version-select');
    select.value='v1'; select.dispatchEvent(new Event('change'));
    await delay(50);
    select.value='v2'; select.dispatchEvent(new Event('change'));
    await waitFor(() => !document.querySelector('#review-root').classList.contains('is-loading') && document.querySelector('#orig-overlay').dataset.version === 'v2', 'version switch');
    await delay(550);
    assert(select.value === 'v2' && document.querySelector('#orig').src.includes('v=v2'), 'stale state response won');
    assert(document.querySelector('#orig-overlay').dataset.version === 'v2', 'stale overlay version');
    fail = true;
    document.querySelector('#step-fwd').click();
    await waitFor(() => document.querySelector('#overlay-status').textContent.includes('원본 재생'), 'overlay failure message');
    assert(document.querySelectorAll('#orig-overlay path').length === 0, 'stale layer visible on failure');
    assert(document.querySelector('#orig').dataset.frame === '3', 'original stalled on overlay failure');
    document.querySelector('#play-btn').click();
    await waitFor(() => Number(document.querySelector('#orig').dataset.frame) > 5, 'playback despite overlay outage');
    document.querySelector('#play-btn').click();
    const recon = performance.getEntriesByType('resource').filter(e => e.name.includes('/frame/recon/'));
    assert(recon.length === 0, 'reconstruction requested');
    return {rapidSeek:'pass',versionRace:'pass',overlayFailurePlayback:'pass',reconstructionRequests:recon.length,overlayRequests:observed.length};
  } finally { window.fetch = originalFetch; }
})()
