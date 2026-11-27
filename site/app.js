/* Atlas site: theme, copy buttons, venue-page filter, and the catalogue
   (search, filters, asset drawer) over catalog/instruments.json. */
(function () {
  "use strict";

  var CLASS_LABELS = {
    equity: "Equity", commodity: "Commodity", forex: "FX", index: "Index",
    pre_market: "Pre-IPO", crypto: "Crypto", unknown: "Unclassified"
  };
  var RWA = { equity: 1, commodity: 1, forex: 1, index: 1, pre_market: 1 };
  var PAGE_SIZE = 50;
  var DAY_MS = 86400000;

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n) { return n.toLocaleString("en-US"); }
  function day(n) { return n ? new Date(n * DAY_MS).toISOString().slice(0, 10) : ""; }
  function sanitize(ticker) { return String(ticker || "").toUpperCase().replace(/[^A-Z0-9]/g, ""); }
  function tail(symbol) { var i = symbol.indexOf(":"); return i < 0 ? symbol : symbol.slice(i + 1); }
  function storageGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function storageSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } }

  /* ---------------- Theme ---------------- */
  var toggle = $("#theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      storageSet("atlas-theme", next);
    });
  }

  /* ---------------- Copy buttons ---------------- */
  $all("[data-copy], [data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy");
      if (!text) {
        var target = document.getElementById(button.getAttribute("data-copy-target"));
        text = target ? target.textContent : "";
      }
      var done = function () {
        var label = button.textContent;
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = label; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
      }
    });
  });

  /* ---------------- Venue pages: filter the static table ---------------- */
  $all("[data-table-filter]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-table-filter"));
    if (!table) return;
    var rows = $all("tbody tr", table);
    var text = rows.map(function (row) { return row.textContent.toLowerCase(); });
    var timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var tokens = input.value.toLowerCase().split(/\s+/).filter(Boolean);
        rows.forEach(function (row, i) {
          var hit = tokens.every(function (t) { return text[i].indexOf(t) >= 0; });
          row.hidden = !hit;
        });
      }, 80);
    });
  });

  var panel = $("[data-catalogue]");
  if (!panel) return;

  /* ---------------- Catalogue data ---------------- */
  var data = null;        // columnar instruments payload
  var assets = {};        // asset id -> {i, t, n, c, s, g, l}
  var assetsByTicker = {};
  var hay = [];           // lower-case search text per row
  var tickers = [];       // sanitized venue ticker per row
  var loading = null;

  function load() {
    if (loading) return loading;
    loading = Promise.all([
      fetch("catalog/instruments.json").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch("catalog/assets.json").then(function (r) { return r.ok ? r.json() : { assets: [] }; }).catch(function () { return { assets: [] }; })
    ]).then(function (payloads) {
      data = payloads[0];
      payloads[1].assets.forEach(function (asset) {
        assets[asset.i] = asset;
        [asset.t].concat(asset.l || []).forEach(function (t) {
          var key = sanitize(t);
          (assetsByTicker[key] = assetsByTicker[key] || []).push(asset.i);
        });
      });
      var n = data.count;
      hay = new Array(n);
      tickers = new Array(n);
      for (var i = 0; i < n; i++) {
        var venue = data.venues[data.v[i]];
        var asset = data.a[i] ? assets[data.a[i]] : null;
        tickers[i] = sanitize(tail(data.sym[i]));
        hay[i] = (data.id[i] + " " + data.iid[i] + " " + data.sym[i] + " " + data.a[i] + " " + data.uid[i] + " " +
          (asset ? asset.n + " " + asset.t : "") + " " + venue.venue + " " + venue.slug + " " +
          data.classes[data.c[i]]).toLowerCase();
      }
      populateVenues();
      return data;
    });
    return loading;
  }

  /* ---------------- State ---------------- */
  var els = {
    q: $("#q"), venue: $("#venue"), type: $("#type"), status: $("#status"),
    count: $("#count"), results: $("#results"), pager: $("#pager"), pagerTop: $("#pager-top"), reset: $("#reset")
  };
  var chips = $all(".filter-chip[data-class]", panel);
  var state = { q: "", classes: [], venue: "", type: "", status: "", page: 0 };

  function readUrl() {
    var params = new URLSearchParams(location.search);
    state.q = params.get("q") || "";
    state.classes = (params.get("class") || "").split(",").filter(function (c) { return CLASS_LABELS[c]; });
    state.venue = params.get("venue") || "";
    state.type = params.get("type") || "";
    state.status = params.get("status") || "";
    state.page = Math.max(0, (parseInt(params.get("page"), 10) || 1) - 1);
    return params;
  }

  function writeUrl(extra) {
    var params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.classes.length) params.set("class", state.classes.join(","));
    if (state.venue) params.set("venue", state.venue);
    if (state.type) params.set("type", state.type);
    if (state.status) params.set("status", state.status);
    if (state.page) params.set("page", String(state.page + 1));
    var theme = new URLSearchParams(location.search).get("theme");
    if (theme) params.set("theme", theme);
    Object.keys(extra || {}).forEach(function (key) { if (extra[key]) params.set(key, extra[key]); });
    var query = params.toString();
    try {
      history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
    } catch (e) { /* file:// or sandboxed */ }
  }

  function syncControls() {
    els.q.value = state.q;
    els.type.value = state.type;
    els.status.value = state.status;
    if (data) els.venue.value = state.venue;
    chips.forEach(function (chip) {
      chip.setAttribute("aria-pressed", state.classes.indexOf(chip.getAttribute("data-class")) >= 0 ? "true" : "false");
    });
  }

  function populateVenues() {
    var groups = { DEX: [], CEX: [] };
    data.venues.forEach(function (venue, i) { (groups[venue.kind] || groups.CEX).push([venue, i]); });
    var html = '<option value="">All venues</option>';
    ["DEX", "CEX"].forEach(function (kind) {
      if (!groups[kind].length) return;
      html += '<optgroup label="' + kind + '">';
      groups[kind].forEach(function (pair) {
        html += '<option value="' + esc(pair[0].slug) + '">' + esc(pair[0].venue + " · " + pair[0].product) + "</option>";
      });
      html += "</optgroup>";
    });
    els.venue.innerHTML = html;
    els.venue.value = state.venue;
  }

  /* ---------------- Search ---------------- */
  function tokens() { return state.q.toLowerCase().split(/\s+/).filter(Boolean); }

  function score(i, toks) {
    var total = 0;
    var asset = data.a[i] ? assets[data.a[i]] : null;
    var fields = [tickers[i].toLowerCase(), data.a[i].toLowerCase(), data.id[i].toLowerCase(),
      data.iid[i].toLowerCase(), data.sym[i].toLowerCase(), data.uid[i].toLowerCase(),
      asset ? asset.n.toLowerCase() : ""];
    for (var t = 0; t < toks.length; t++) {
      var tok = toks[t], best = 3;
      var clean = tok.replace(/[^a-z0-9]/g, "");
      for (var f = 0; f < fields.length; f++) {
        var field = fields[f];
        if (field === tok || (f === 0 && field === clean)) { best = 0; break; }
        if (best > 1 && field.indexOf(tok) === 0) best = 1;
      }
      if (best === 3) best = 2;
      total += best;
    }
    return total;
  }

  function matches(i, toks, venueIndex, typeIndex, skipClass) {
    if (venueIndex >= 0 && data.v[i] !== venueIndex) return false;
    if (typeIndex >= 0 && data.t[i] !== typeIndex) return false;
    if (state.status === "active" && data.ed[i]) return false;
    if (state.status === "delisted" && !data.ed[i]) return false;
    if (!skipClass && state.classes.length && state.classes.indexOf(data.classes[data.c[i]]) < 0) return false;
    for (var t = 0; t < toks.length; t++) if (hay[i].indexOf(toks[t]) < 0) return false;
    return true;
  }

  function run() {
    if (!data) return;
    var toks = tokens();
    var venueIndex = -1;
    if (state.venue) {
      venueIndex = data.venues.findIndex(function (v) { return v.slug === state.venue; });
      if (venueIndex < 0) state.venue = "";
    }
    var typeIndex = state.type ? data.types.indexOf(state.type) : -1;
    var hits = [];
    var classCounts = {};
    var live = 0;
    for (var i = 0; i < data.count; i++) {
      if (!matches(i, toks, venueIndex, typeIndex, true)) continue;
      var klass = data.classes[data.c[i]];
      classCounts[klass] = (classCounts[klass] || 0) + 1;
      if (state.classes.length && state.classes.indexOf(klass) < 0) continue;
      hits.push(i);
      if (!data.ed[i]) live++;
    }
    if (toks.length) {
      var scores = {};
      hits.forEach(function (i) { scores[i] = score(i, toks); });
      // The payload is pre-sorted live-first, RWA-first, so the index breaks ties.
      hits.sort(function (a, b) { return scores[a] - scores[b] || (!!data.ed[a] - !!data.ed[b]) || a - b; });
    }
    chips.forEach(function (chip) {
      var klass = chip.getAttribute("data-class");
      var holder = chip.querySelector(".count");
      if (!holder) { holder = document.createElement("span"); holder.className = "count"; chip.appendChild(holder); }
      holder.textContent = fmt(classCounts[klass] || 0);
    });
    var pages = Math.max(1, Math.ceil(hits.length / PAGE_SIZE));
    if (state.page >= pages) state.page = pages - 1;
    var start = state.page * PAGE_SIZE;
    var slice = hits.slice(start, start + PAGE_SIZE);
    els.count.textContent = hits.length
      ? "Showing " + fmt(start + 1) + "–" + fmt(start + slice.length) + " of " + fmt(hits.length) + " instruments · " + fmt(live) + " live"
      : "No instruments match. Try a ticker (TSLA), an Atlas ID (perpetual-XAU-USDT:USDT) or fewer filters.";
    els.results.innerHTML = slice.length ? slice.map(function (i) { return rowHtml(i, toks); }).join("")
      : '<tr><td colspan="6" class="muted">Nothing here yet.</td></tr>';
    renderPager(els.pager, pages);
    renderPager(els.pagerTop, pages);
  }

  function highlight(text, toks) {
    if (!toks.length || !text) return esc(text);
    var lower = text.toLowerCase();
    var marks = new Array(text.length);
    toks.forEach(function (tok) {
      var from = 0, at;
      while (tok && (at = lower.indexOf(tok, from)) >= 0) {
        for (var k = at; k < at + tok.length; k++) marks[k] = true;
        from = at + tok.length;
      }
    });
    var out = "", open = false;
    for (var c = 0; c < text.length; c++) {
      if (marks[c] && !open) { out += "<mark>"; open = true; }
      if (!marks[c] && open) { out += "</mark>"; open = false; }
      out += esc(text[c]);
    }
    return out + (open ? "</mark>" : "");
  }

  function assetButton(i) {
    var id = data.a[i];
    if (id) {
      var asset = assets[id];
      return '<button type="button" class="asset-chip" data-asset="' + esc(id) + '" title="' + esc(asset ? asset.n : id) + '">' + esc(id) + "</button>";
    }
    if (!tickers[i]) return '<span class="muted">—</span>';
    return '<button type="button" class="asset-chip" data-ticker="' + esc(tickers[i]) + '" title="Not resolved to an Atlas asset yet: show every listing of this ticker">' + esc(tickers[i]) + "?</button>";
  }

  function rowHtml(i, toks) {
    var venue = data.venues[data.v[i]];
    var klass = data.classes[data.c[i]];
    var status = data.ed[i]
      ? '<span class="status dead">delisted ' + day(data.ed[i]) + "</span>"
      : '<span class="status live">live</span>' + (data.fc[i] ? ' <span class="muted status">since ' + day(data.fc[i]) + "</span>" : "");
    var unified = data.uid[i] && data.uid[i] !== data.iid[i]
      ? '<br><small class="muted"><code>' + highlight(data.uid[i], toks) + "</code></small>" : "";
    return "<tr>" +
      '<td data-label="Venue"><span class="venue-cell">' + esc(venue.venue) + "<small>" + esc(venue.product) + "</small></span></td>" +
      '<td data-label="Native"><code>' + highlight(data.id[i], toks) + "</code></td>" +
      '<td data-label="Atlas ID" class="wrap"><code>' + highlight(data.iid[i], toks) + "</code>" + unified + "</td>" +
      '<td data-label="Asset">' + assetButton(i) + "</td>" +
      '<td data-label="Class"><span class="dot cls-' + klass + '"></span>' + CLASS_LABELS[klass] + "</td>" +
      '<td data-label="Listed">' + status + "</td>" +
      "</tr>";
  }

  function renderPager(holder, pages) {
    if (!holder) return;
    if (pages <= 1) { holder.innerHTML = ""; return; }
    holder.innerHTML =
      '<button type="button" data-page="prev"' + (state.page === 0 ? " disabled" : "") + ' aria-label="Previous page">←</button>' +
      "<span>Page " + fmt(state.page + 1) + " of " + fmt(pages) + "</span>" +
      '<button type="button" data-page="next"' + (state.page >= pages - 1 ? " disabled" : "") + ' aria-label="Next page">→</button>';
  }

  function update(resetPage) {
    if (resetPage) state.page = 0;
    writeUrl();
    run();
  }

  var timer = null;
  els.q.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { state.q = els.q.value.trim(); update(true); }, 90);
  });
  els.venue.addEventListener("change", function () { state.venue = els.venue.value; update(true); });
  els.type.addEventListener("change", function () { state.type = els.type.value; update(true); });
  els.status.addEventListener("change", function () { state.status = els.status.value; update(true); });
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var klass = chip.getAttribute("data-class");
      var at = state.classes.indexOf(klass);
      if (at >= 0) state.classes.splice(at, 1); else state.classes.push(klass);
      syncControls();
      update(true);
    });
  });
  els.reset.addEventListener("click", function () {
    state = { q: "", classes: [], venue: "", type: "", status: "", page: 0 };
    syncControls();
    update(true);
  });
  [els.pager, els.pagerTop].forEach(function (holder) {
    if (!holder) return;
    holder.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-page]");
      if (!button || button.disabled) return;
      state.page += button.getAttribute("data-page") === "next" ? 1 : -1;
      update(false);
      if (holder === els.pager) panel.scrollIntoView({ block: "start" });
    });
  });

  // Hero search and in-page filter links (?class=…#catalogue) without a reload.
  var heroForm = $("[data-hero-search]");
  if (heroForm) {
    heroForm.addEventListener("submit", function (event) {
      event.preventDefault();
      state = { q: $("#hero-q").value.trim(), classes: [], venue: "", type: "", status: "", page: 0 };
      syncControls();
      load().then(function () { update(true); });
      $("#catalogue").scrollIntoView({ block: "start" });
    });
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest('a[href^="?"]');
    if (!link || !/#catalogue$/.test(link.getAttribute("href"))) return;
    event.preventDefault();
    try { history.replaceState(null, "", link.getAttribute("href")); } catch (e) { /* ignore */ }
    readUrl();
    syncControls();
    load().then(function () { els.venue.value = state.venue; run(); });
    $("#catalogue").scrollIntoView({ block: "start" });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "/" && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) {
      event.preventDefault();
      els.q.focus();
    }
  });

  /* ---------------- Asset drawer ---------------- */
  var drawer = $("#drawer");
  var backdrop = $("#drawer-backdrop");
  var lastFocus = null;

  function listingRows(indices) {
    var sorted = indices.slice().sort(function (a, b) {
      return (!!data.ed[a] - !!data.ed[b]) || data.v[a] - data.v[b] || (data.id[a] < data.id[b] ? -1 : 1);
    });
    return '<div class="table-wrap"><table class="results"><thead><tr><th>Venue</th><th>Native ID</th><th>Atlas ID</th><th>Listed</th></tr></thead><tbody>' +
      sorted.map(function (i) {
        var venue = data.venues[data.v[i]];
        var klass = data.classes[data.c[i]];
        var status = data.ed[i] ? '<span class="status dead">delisted ' + day(data.ed[i]) + "</span>" : '<span class="status live">live</span>';
        var unified = data.uid[i] && data.uid[i] !== data.iid[i] ? '<br><small class="muted"><code>' + esc(data.uid[i]) + "</code></small>" : "";
        return "<tr>" +
          '<td data-label="Venue"><span class="venue-cell">' + esc(venue.venue) + "<small>" + esc(venue.product) + "</small></span></td>" +
          '<td data-label="Native" title="' + esc(CLASS_LABELS[klass]) + '"><span class="dot cls-' + klass + '"></span><code>' + esc(data.id[i]) + "</code></td>" +
          '<td data-label="Atlas ID" class="wrap"><code>' + esc(data.iid[i]) + "</code>" + unified + "</td>" +
          '<td data-label="Listed">' + status + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  function summary(indices) {
    var venues = {}, live = 0;
    indices.forEach(function (i) { if (!data.ed[i]) { live++; venues[data.venues[data.v[i]].venue] = 1; } });
    return '<p class="kv"><span><b>' + fmt(Object.keys(venues).length) + "</b> venues live</span><span><b>" + fmt(live) +
      "</b> live listings</span><span><b>" + fmt(indices.length - live) + "</b> delisted</span></p>";
  }

  function openDrawer(kicker, title, sub, body, param) {
    $("#drawer-kicker").textContent = kicker;
    $("#drawer-title").textContent = title;
    $("#drawer-sub").innerHTML = sub;
    $("#drawer-body").innerHTML = body;
    lastFocus = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("drawer-open");
    $("#drawer-close").focus();
    writeUrl(param);
  }

  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove("drawer-open");
    writeUrl();
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function showAsset(id) {
    var asset = assets[id];
    var own = [], sameTicker = [];
    var names = {};
    if (asset) [asset.t].concat(asset.l || []).forEach(function (t) { names[sanitize(t)] = 1; });
    else names[sanitize(id)] = 1;
    for (var i = 0; i < data.count; i++) {
      if (data.a[i] === id) own.push(i);
      else if (!data.a[i] && names[tickers[i]]) sameTicker.push(i);
    }
    var klass = asset ? asset.c : (own.length ? data.classes[data.c[own[0]]] : "unknown");
    var sub = esc(asset ? asset.n : "Atlas asset") + " · " + esc(CLASS_LABELS[klass] || klass);
    if (asset && asset.s) sub += ' · <a href="https://coinmarketcap.com/currencies/' + esc(asset.s) + '/" rel="noopener">CoinMarketCap</a>';
    var body = summary(own);
    if (asset && (asset.g || asset.l)) {
      body += '<p class="kv">' + (asset.g ? "<span>Categories <b>" + esc(asset.g.join(", ")) + "</b></span>" : "") +
        (asset.l ? "<span>Also listed as <b>" + esc(asset.l.join(", ")) + "</b></span>" : "") + "</p>";
    }
    body += "<section><h3>Listings resolved to " + esc(id) + "</h3>" + (own.length ? listingRows(own) : '<p class="muted">No listing is resolved to this asset yet.</p>') + "</section>";
    if (sameTicker.length) {
      body += "<section><h3>Same ticker, not yet resolved</h3><p class=\"muted small\">Venues whose listings Atlas symbology has not resolved yet. They likely, but not certainly, trade the same asset.</p>" + listingRows(sameTicker) + "</section>";
    }
    openDrawer(RWA[klass] ? "Real-world asset" : "Asset", id, sub, body, { asset: id });
  }

  function showTicker(ticker) {
    var key = sanitize(ticker);
    var groups = {}, unresolved = [];
    for (var i = 0; i < data.count; i++) {
      if (tickers[i] !== key) continue;
      if (data.a[i]) (groups[data.a[i]] = groups[data.a[i]] || []).push(i);
      else unresolved.push(i);
    }
    var body = summary(unresolved.concat.apply(unresolved, Object.keys(groups).map(function (k) { return groups[k]; })));
    var known = (assetsByTicker[key] || []).filter(function (id) { return !groups[id]; });
    Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; }).forEach(function (id) {
      var asset = assets[id];
      body += '<section><h3><button type="button" class="asset-chip" data-asset="' + esc(id) + '">' + esc(id) + "</button> " +
        esc(asset ? asset.n : "") + "</h3>" + listingRows(groups[id]) + "</section>";
    });
    if (unresolved.length) {
      body += "<section><h3>Not resolved to an Atlas asset yet</h3>" + listingRows(unresolved) + "</section>";
    }
    if (known.length) {
      body += '<section><h3>Other Atlas assets with this ticker</h3><div class="chips">' + known.map(function (id) {
        return '<button type="button" class="asset-chip" data-asset="' + esc(id) + '" title="' + esc(assets[id].n) + '">' + esc(id) + "</button>";
      }).join("") + "</div></section>";
    }
    var resolvedGroups = Object.keys(groups).length;
    var sub = resolvedGroups > 1 ? fmt(resolvedGroups) + " different assets share this ticker" : "Every listing of this venue ticker";
    openDrawer("Ticker", key, esc(sub), body, { ticker: key });
  }

  drawer.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-asset]");
    if (chip) showAsset(chip.getAttribute("data-asset"));
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeDrawer();
    if (event.key === "Tab" && !drawer.hidden) {
      var focusable = $all("button, a[href], input, select", drawer).filter(function (el) { return !el.disabled; });
      if (!focusable.length) return;
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-asset], [data-ticker]");
    if (!chip || drawer.contains(chip)) return;
    var asset = chip.getAttribute("data-asset");
    var ticker = chip.getAttribute("data-ticker");
    load().then(function () { if (asset) showAsset(asset); else showTicker(ticker); });
  });

  /* ---------------- Start ---------------- */
  var initial = readUrl();
  syncControls();
  load().then(function () {
    run();
    if (initial.get("asset")) showAsset(initial.get("asset"));
    else if (initial.get("ticker")) showTicker(initial.get("ticker"));
  }).catch(function () {
    els.count.textContent = "The catalogue could not be loaded. The raw snapshots are on the data files page.";
  });
})();
