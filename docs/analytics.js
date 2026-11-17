/* TickerDesk visitor analytics (PostHog), OFF until a key is set.
 *
 * TO TURN IT ON: paste the PostHog "Project API key" (it starts with phc_)
 * between the quotes below, commit, and push. That is the only change
 * needed. The key is public by design (PostHog says so): it can only SEND
 * events, never read them. See docs_dev/ANALYTICS.md.
 *
 * With the key empty, nothing loads, nothing is sent, and tdTrack() does
 * nothing.
 */
var TD_POSTHOG_KEY = "";
/* US cloud. If the PostHog project is on EU cloud, change both hosts here
 * AND in the Content-Security-Policy in docs/index.html. */
var TD_POSTHOG_HOST = "https://us.i.posthog.com";
var TD_POSTHOG_ASSETS = "https://us-assets.i.posthog.com";

(function (w, d) {
  "use strict";
  var KEY = String(TD_POSTHOG_KEY || "").trim();
  var nav = w.navigator || {};

  // Do Not Track and Global Privacy Control both switch analytics off.
  function optedOut() {
    var dnt = nav.doNotTrack || w.doNotTrack || nav.msDoNotTrack;
    return dnt === "1" || dnt === "yes" || nav.globalPrivacyControl === true;
  }

  var ON = !!KEY && /^phc_[A-Za-z0-9_-]+$/.test(KEY) && !optedOut();

  // Strip the hash and every query parameter except utm_*: sign-in links
  // put access tokens in the hash, and Stripe adds a session id on return.
  function cleanUrl(u) {
    if (!u || typeof u !== "string") return u;
    try {
      var url = new URL(u);
      var keep = new URLSearchParams();
      url.searchParams.forEach(function (v, k) {
        if (/^utm_(source|medium|campaign|term|content)$/.test(k)) keep.set(k, v);
      });
      var q = keep.toString();
      return url.origin + url.pathname + (q ? "?" + q : "");
    } catch (_) {
      return String(u).split("#")[0].split("?")[0];
    }
  }
  var URL_PROPS = [
    "$current_url",
    "$referrer",
    "$initial_referrer",
    "$initial_current_url",
    "$session_entry_url",
    "$session_entry_referrer",
  ];
  function scrub(props) {
    if (!props) return props;
    for (var i = 0; i < URL_PROPS.length; i++) {
      if (typeof props[URL_PROPS[i]] === "string") props[URL_PROPS[i]] = cleanUrl(props[URL_PROPS[i]]);
    }
    return props;
  }

  function load() {
    // Minimal PostHog loader stub (same shape as the official snippet):
    // calls made before array.js arrives are queued and replayed.
    if (w.posthog && (w.posthog.__loaded || w.posthog.__SV)) return;
    var ph = (w.posthog = w.posthog || []);
    ph._i = [];
    ph.init = function (token, config, name) {
      function stub(target, method) {
        target[method] = function () {
          target.push([method].concat(Array.prototype.slice.call(arguments, 0)));
        };
      }
      var s = d.createElement("script");
      s.type = "text/javascript";
      s.crossOrigin = "anonymous";
      s.async = true;
      s.src = TD_POSTHOG_ASSETS + "/static/array.js";
      (d.head || d.getElementsByTagName("head")[0] || d.documentElement).appendChild(s);
      ["capture", "identify", "reset", "register"].forEach(function (m) {
        stub(ph, m);
      });
      ph._i.push([token, config, name]);
    };
    ph.__SV = 1;
    ph.init(KEY, {
      api_host: TD_POSTHOG_HOST,
      // No cookies. The anonymous id lives in sessionStorage, which the
      // browser deletes when the tab closes, so a visitor is never
      // recognised across visits until they sign in.
      persistence: "sessionStorage",
      person_profiles: "identified_only",
      respect_dnt: true,
      // Only the funnel events below are sent: no automatic click capture
      // (so nothing typed into a form is ever read), no automatic page
      // views (we send our own, cleaned), no session recording, no
      // surveys, no heatmaps.
      autocapture: false,
      capture_pageview: false,
      capture_pageleave: false,
      capture_dead_clicks: false,
      capture_heatmaps: false,
      disable_session_recording: true,
      disable_surveys: true,
      enable_heatmaps: false,
      mask_all_text: true,
      mask_all_element_attributes: true,
      // Settings in the PostHog dashboard must never be able to switch on
      // more collection than this code allows: no remote config / flag
      // calls, no error capture (page URLs + stack traces), no web-vitals
      // timing, no extra scripts pulled in at runtime.
      advanced_disable_flags: true,
      advanced_disable_decide: true,
      capture_exceptions: false,
      capture_performance: false,
      disable_external_dependency_loading: true,
      before_send: function (ev) {
        if (ev) {
          scrub(ev.properties);
          scrub(ev.$set);
          scrub(ev.$set_once);
        }
        return ev;
      },
    });
  }

  function ph() {
    return ON && w.posthog && typeof w.posthog.capture === "function" ? w.posthog : null;
  }

  // Remove anything that looks like an email from event properties. Events
  // must never carry personal data; this is a backstop, not the plan.
  function safeProps(props) {
    var out = {};
    if (!props || typeof props !== "object") return out;
    Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v === undefined || v === null) return;
      if (/email|name|phone/i.test(k)) return;
      if (typeof v === "string" && /@/.test(v)) return;
      out[k] = v;
    });
    return out;
  }

  // tdTrack(event, props, opts): the one analytics call used everywhere.
  // No key, Do Not Track, or PostHog blocked: does nothing. Never throws.
  function tdTrack(event, props, opts) {
    try {
      var p = ph();
      if (!p || !event) return;
      if (opts) p.capture(String(event), safeProps(props), opts);
      else p.capture(String(event), safeProps(props));
    } catch (_) {
      /* analytics must never break the page */
    }
  }

  // Signed-in users are known by their Supabase user id only (never their
  // email). Pass null on sign-out to forget the id on this tab.
  var identified = null;
  function tdIdentify(userId) {
    try {
      var p = ph();
      if (!p) return;
      if (userId) {
        if (identified === userId) return;
        identified = String(userId);
        p.identify(identified);
      } else if (identified) {
        identified = null;
        p.reset();
      }
    } catch (_) {
      /* never throws */
    }
  }

  function referrerDomain() {
    try {
      if (!d.referrer) return "";
      var h = new URL(d.referrer).hostname;
      return h === w.location.hostname ? "" : h;
    } catch (_) {
      return "";
    }
  }

  function pageViewProps() {
    var props = { path: w.location.pathname, referrer_domain: referrerDomain() };
    try {
      var q = new URLSearchParams(w.location.search);
      ["utm_source", "utm_medium", "utm_campaign"].forEach(function (k) {
        var v = q.get(k);
        if (v) props[k] = v.slice(0, 100);
      });
    } catch (_) {
      /* old browser: send the page view without UTM tags */
    }
    return props;
  }

  w.tdTrack = tdTrack;
  w.tdIdentify = tdIdentify;
  w.tdAnalyticsOn = function () {
    return ON;
  };
  w.tdAnalyticsCleanUrl = cleanUrl;

  if (!ON) return;
  try {
    load();
    tdTrack("page_view", pageViewProps());
  } catch (_) {
    /* never throws */
  }
})(window, document);
