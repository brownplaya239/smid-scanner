/* Off-main-thread loader for the heavy scored-signals dataset.
 *
 * uoa_signals_scored.json is ~19.7 MB raw (1.7 MB gzipped). Fetching and
 * JSON.parse-ing it on the main thread froze the UI for ~0.5-1s the first
 * time a user opened the Options Flow → Tracked Signals subtab. Doing the
 * fetch + parse here keeps the page responsive; only the final parsed
 * array crosses back to the main thread (one structured-clone hand-off,
 * far cheaper than the synchronous parse it replaces).
 *
 * Protocol: main thread posts { url }; worker replies { ok, signals } or
 * { ok:false, error }. The main thread has a plain-fetch fallback if the
 * worker can't be created (older browsers / blocked workers).
 */
self.onmessage = function (e) {
  var url = (e && e.data && e.data.url) || "./reports/uoa_signals_scored.json";
  fetch(url, { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (d) {
      self.postMessage({
        ok: true,
        signals: (d && d.signals) ? d.signals : [],
      });
    })
    .catch(function (err) {
      self.postMessage({ ok: false, error: String(err && err.message || err) });
    });
};
