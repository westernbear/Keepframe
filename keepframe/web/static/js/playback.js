import { reviewFrameUrl } from "/static/js/api.js?v=20261006g";

const PREFETCH_AHEAD = 4;
const PREVIEW_CACHE_LIMIT = 48;
const SEEK_DELAY_WHILE_PLAYING_MS = 40;
const SEEK_DELAY_MS = 32;
const FRAME_SKIP_SHIFT = 10;

function isImageDecoded(img) {
  return Boolean(img && img.complete && img.naturalWidth > 1);
}

function frameDurationSec(fps) {
  return 1 / Math.max(1, Number(fps) || 1);
}

function isDueForNextFrame(elapsedMs, fps) {
  return elapsedMs / 1000 >= frameDurationSec(fps);
}

function isLastPlaybackFrame(index, total) {
  return index >= total - 1;
}

function seekDelayMs(playing) {
  return playing ? SEEK_DELAY_WHILE_PLAYING_MS : SEEK_DELAY_MS;
}

function isSeekQueuedWhilePlaying(playing, seekTimer) {
  return playing && Boolean(seekTimer);
}

function frameStep(shiftKey) {
  return shiftKey ? FRAME_SKIP_SHIFT : 1;
}

function createFrameTransport({
  getFrame,
  setFrameIndex,
  getFps,
  getFrameCount,
  prefetch,
  isReady,
  wait,
  showFrame,
  canPlay,
  onPlayingChange,
  onFrameUnavailable,
}) {
  let playing = false;
  let raf = 0;
  let lastTick = 0;
  let waiting = false;
  let generation = 0;

  function isPlaying() {
    return playing;
  }

  function isWaiting() {
    return waiting;
  }

  function showNextPlaybackFrame(next) {
    setFrameIndex(next);
    showFrame(next);
  }

  function tick(ts) {
    if (!playing) return;
    if (waiting) {
      raf = requestAnimationFrame(tick);
      return;
    }
    if (!lastTick) lastTick = ts;
    if (isDueForNextFrame(ts - lastTick, getFps())) {
      lastTick = ts;
      const frame = getFrame();
      const total = getFrameCount();
      if (isLastPlaybackFrame(frame, total)) {
        setPlaying(false);
        return;
      }
      const next = frame + 1;
      prefetch(next, total);
      if (isReady(next)) {
        showNextPlaybackFrame(next);
      } else {
        waiting = true;
        const requestGeneration = generation;
        wait(next).then((ok) => {
          if (requestGeneration !== generation) return;
          waiting = false;
          if (!playing) return;
          lastTick = performance.now();
          if (!ok) {
            setPlaying(false);
            if (onFrameUnavailable) onFrameUnavailable();
            return;
          }
          showNextPlaybackFrame(next);
        });
      }
    }
    raf = requestAnimationFrame(tick);
  }

  function setPlaying(on) {
    generation++;
    playing = Boolean(on);
    lastTick = 0;
    waiting = false;
    cancelAnimationFrame(raf);
    raf = 0;
    if (onPlayingChange) onPlayingChange(playing);
    const allowed = !canPlay || canPlay();
    if (playing && allowed) {
      prefetch(getFrame(), getFrameCount());
      raf = requestAnimationFrame(tick);
    }
  }

  return { setPlaying, isPlaying, isWaiting };
}

function createPreviewCache({
  project,
  scene,
  version,
  prefetchAhead = PREFETCH_AHEAD,
  cacheLimit = PREVIEW_CACHE_LIMIT,
  kinds = ["orig", "recon"],
}) {
  const cache = new Map();

  function currentVersion() {
    return typeof version === "function" ? version() : version;
  }

  function current(value) {
    return typeof value === "function" ? value() : value;
  }

  function src(kind, index) {
    return reviewFrameUrl(kind, index, current(project), current(scene), currentVersion());
  }

  function touch(url) {
    const item = cache.get(url);
    if (!item) return null;
    cache.delete(url);
    cache.set(url, item);
    return item;
  }

  function evictIfNeeded() {
    while (cache.size > cacheLimit) {
      cache.delete(cache.keys().next().value);
    }
  }

  function waitUntilDecoded(img) {
    return new Promise((resolve) => {
      const finish = async () => {
        if (!isImageDecoded(img)) {
          resolve(false);
          return;
        }
        if (typeof img.decode === "function") {
          try {
            await img.decode();
          } catch {
            resolve(false);
            return;
          }
        }
        resolve(isImageDecoded(img));
      };
      if (isImageDecoded(img)) {
        finish();
        return;
      }
      img.addEventListener("load", finish, { once: true });
      img.addEventListener("error", () => resolve(false), { once: true });
    });
  }

  function entry(kind, index) {
    const url = src(kind, index);
    const cached = touch(url);
    if (cached) return cached;
    const img = new Image();
    img.decoding = "async";
    const ready = waitUntilDecoded(img);
    img.src = url;
    const item = { img, ready };
    cache.set(url, item);
    evictIfNeeded();
    return item;
  }

  function prefetch(from, total) {
    const n = Math.max(0, total | 0);
    for (let d = 0; d <= prefetchAhead && from + d < n; d++) {
      kinds.forEach((kind) => entry(kind, from + d));
    }
  }

  function isReady(index) {
    return kinds.every((kind) => isImageDecoded(entry(kind, index).img));
  }

  async function wait(index) {
    return (await Promise.all(kinds.map((kind) => entry(kind, index).ready))).every(Boolean);
  }

  return { prefetch, isReady, wait, src, image: (kind, index) => entry(kind, index).img, clear: () => cache.clear() };
}

export {
  PREFETCH_AHEAD,
  PREVIEW_CACHE_LIMIT,
  SEEK_DELAY_WHILE_PLAYING_MS,
  SEEK_DELAY_MS,
  FRAME_SKIP_SHIFT,
  isImageDecoded,
  frameDurationSec,
  isDueForNextFrame,
  isLastPlaybackFrame,
  seekDelayMs,
  isSeekQueuedWhilePlaying,
  frameStep,
  createFrameTransport,
  createPreviewCache,
};
