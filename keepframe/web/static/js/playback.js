import { reviewFrameUrl } from "/static/js/api.js?v=20260921t";

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

function createPreviewCache({
  project,
  scene,
  version,
  prefetchAhead = PREFETCH_AHEAD,
  cacheLimit = PREVIEW_CACHE_LIMIT,
}) {
  const cache = new Map();

  function currentVersion() {
    return typeof version === "function" ? version() : version;
  }

  function src(kind, index) {
    return reviewFrameUrl(kind, index, project, scene, currentVersion());
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
      entry("orig", from + d);
      entry("recon", from + d);
    }
  }

  function isReady(index) {
    return isImageDecoded(entry("orig", index).img) && isImageDecoded(entry("recon", index).img);
  }

  async function wait(index) {
    const [origOk, reconOk] = await Promise.all([
      entry("orig", index).ready,
      entry("recon", index).ready,
    ]);
    return origOk && reconOk;
  }

  return { prefetch, isReady, wait, src };
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
  createPreviewCache,
};
