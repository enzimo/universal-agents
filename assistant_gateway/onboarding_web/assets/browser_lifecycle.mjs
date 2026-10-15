/** A page owns its timers, animation frames, listeners, and pending requests. */
export function createPageLifecycle(browser) {
  const timeouts = new Set();
  const intervals = new Set();
  const frames = new Set();
  const controller = new browser.AbortController();
  let disposed = false;
  const lifecycle = {
    signal: controller.signal,
    timeout(callback, delay, ...args) {
      if (disposed) return null;
      const id = browser.setTimeout(() => {
        timeouts.delete(id);
        if (!disposed) callback(...args);
      }, delay);
      timeouts.add(id);
      return id;
    },
    interval(callback, delay, ...args) {
      if (disposed) return null;
      const id = browser.setInterval(() => {
        if (!disposed) callback(...args);
      }, delay);
      intervals.add(id);
      return id;
    },
    frame(callback) {
      if (disposed) return null;
      const id = browser.requestAnimationFrame((time) => {
        frames.delete(id);
        if (!disposed) callback(time);
      });
      frames.add(id);
      return id;
    },
    clearTimeout(id) { browser.clearTimeout(id); timeouts.delete(id); },
    clearInterval(id) { browser.clearInterval(id); intervals.delete(id); },
    cancelFrame(id) { browser.cancelAnimationFrame(id); frames.delete(id); },
    listen(target, event, callback, options = {}) {
      if (disposed) return;
      const resolved = typeof options === "boolean" ? { capture: options } : options;
      target.addEventListener(event, callback, { ...resolved, signal: controller.signal });
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      controller.abort();
      for (const id of timeouts) browser.clearTimeout(id);
      for (const id of intervals) browser.clearInterval(id);
      for (const id of frames) browser.cancelAnimationFrame(id);
      timeouts.clear(); intervals.clear(); frames.clear();
    },
  };
  return lifecycle;
}

export function createRequestClient(browser, lifecycle) {
  return {
    fetch(url, options = {}) {
      const signal = options.signal
        ? AbortSignal.any([options.signal, lifecycle.signal])
        : lifecycle.signal;
      return browser.fetch(url, { ...options, signal });
    },
  };
}
