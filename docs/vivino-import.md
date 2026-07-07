# Importing your Vivino wines into Cork Dork

Vivino has no usable public API for reading your own cellar: the website login
and the `api.vivino.com` mobile backend are separate auth systems, and a
server-side integration cannot obtain a token the API accepts (this was
verified end-to-end). The reliable way to bring your Vivino wines in is to
export them **from your own logged-in browser**, then import the file with the
built-in CSV importer.

## Step 1 — Export from Vivino (browser console)

1. In a desktop browser, log in to <https://www.vivino.com> and open your
   wines page (**My wines**).
2. Open the developer console: `F12` (or `Cmd+Option+I` on macOS) → **Console**.
3. Paste the whole script below and press **Enter**.
4. It downloads `vivino-wines.csv`. If it can't find your API token, it prints a
   short diagnostic instead — copy that output and share it.

```js
(async () => {
  // ── Find the api.vivino.com bearer token the web app uses ──────────
  const scan = (store) => {
    const hits = [];
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i), v = store.getItem(k) || "";
      if (/token|access|auth|jwt/i.test(k) && v.length > 20) hits.push(v);
      // token nested inside a JSON blob
      const m = v.match(/"(?:api_token|access_token|token)"\s*:\s*"([^"]{20,})"/);
      if (m) hits.push(m[1]);
    }
    return hits;
  };
  let candidates = [...scan(localStorage), ...scan(sessionStorage)];
  for (const g of ["__PRELOADED_STATE__", "__INITIAL_STATE__", "__APOLLO_STATE__"]) {
    try {
      const blob = JSON.stringify(window[g] || "");
      const m = blob.match(/"(?:api_token|access_token|token)"\s*:\s*"([^"]{20,})"/);
      if (m) candidates.push(m[1]);
    } catch (e) {}
  }
  candidates = [...new Set(candidates)];

  // ── Resolve the user id ───────────────────────────────────────────
  let uid = null;
  const um = document.documentElement.innerHTML.match(/"(?:user_id|id)":(\d{4,})/);
  if (um) uid = um[1];

  const tryFetch = async (url, token) => {
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    const r = await fetch(url, { headers, credentials: "include" });
    return { ok: r.ok, status: r.status, body: r.ok ? await r.json() : null };
  };

  // Find a working token + confirm the user id
  let token = null;
  for (const t of [null, ...candidates]) {
    const meUrl = uid ? `https://api.vivino.com/users/${uid}` : "https://api.vivino.com/users/me";
    const res = await tryFetch(meUrl, t);
    if (res.ok) {
      token = t;
      if (!uid && res.body) uid = (res.body.user || res.body).id;
      break;
    }
  }
  if (!uid) {
    console.warn("Could not determine your Vivino user id. Diagnostic:");
    console.log("localStorage keys:", Object.keys(localStorage));
    console.log("sessionStorage keys:", Object.keys(sessionStorage));
    return;
  }

  // ── Page through your wines ("My Wines" = user vintages) ──────────
  const records = [];
  for (let page = 1; page <= 60; page++) {
    let res = await tryFetch(`https://api.vivino.com/users/${uid}/vintages?page=${page}&per_page=50`, token);
    if (!res.ok) {
      // some accounts expose the cellar instead
      res = await tryFetch(`https://api.vivino.com/users/${uid}/cellar?page=${page}&per_page=50`, token);
    }
    if (!res.ok) {
      if (page === 1) {
        console.warn(`Vivino API returned ${res.status}. Token found: ${!!token}. Diagnostic:`);
        console.log("localStorage keys:", Object.keys(localStorage));
        console.log("sessionStorage keys:", Object.keys(sessionStorage));
        console.log("Share these key names (not values) so the script can be adjusted.");
      }
      break;
    }
    const batch = res.body.user_vintages || res.body.vintages || res.body.cellar || res.body.records || [];
    if (!batch.length) break;
    records.push(...batch);
    if (batch.length < 50) break;
  }
  if (!records.length) { console.warn("No wines returned."); return; }

  // ── Map to Cork Dork CSV columns ──────────────────────────────────
  const TYPE = { 1: "red", 2: "white", 3: "sparkling", 4: "rosé", 7: "dessert" };
  const csvCell = (v) => {
    const s = (v == null ? "" : String(v));
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const cols = ["name","winery","vintage","type","region","country","grape variety",
                "rating","ratings count","user rating","alcohol","notes"];
  const rows = [cols.join(",")];
  let owned = 0;
  for (const rec of records) {
    const vintage = rec.vintage || rec;
    const wine = vintage.wine || rec.wine || {};
    if (!wine.name) continue;
    // Only bottles you actually own (skip pure ratings without bottles)
    const count = rec.cellar_count ?? rec.bottle_count ?? rec.count;
    if (count != null && count <= 0) continue;
    if (count != null) owned++;
    const stats = wine.statistics || {};
    const region = wine.region || {};
    const country = (region.country || {}).name || "";
    const grapes = (wine.grapes || []).map((g) => g.name).filter(Boolean).join(", ");
    const review = rec.review || {};
    let year = parseInt(vintage.year, 10);
    if (!year || year < 1800) year = "";
    rows.push([
      wine.name, (wine.winery || {}).name || "", year, TYPE[wine.type_id] || "red",
      region.name || "", country, grapes,
      stats.ratings_average ? Number(stats.ratings_average).toFixed(1) : "",
      stats.ratings_count || "",
      review.rating ? Math.round(review.rating * 2) / 2 : "",
      wine.alcohol ? wine.alcohol + "%" : "",
      (review.note || "").replace(/\s+/g, " ").trim(),
    ].map(csvCell).join(","));
  }

  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "vivino-wines.csv";
  a.click();
  console.log(`Exported ${rows.length - 1} wines (${owned} with bottle counts) to vivino-wines.csv`);
})();
```

## Step 2 — Import into Cork Dork

1. Open your Cork Dork card → **📦 Inventory**.
2. Click **Import CSV** and choose the `vivino-wines.csv` file.
3. The wines are added to your cellar (unassigned). Open any wine and use
   **Move** to place it into a rack.

Tip: after importing, use **🍇 Vivino Batch Scan** to enrich the wines with the
latest ratings, prices, descriptions, and images from Vivino's public data.
