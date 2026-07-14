# Importing your Vivino wines into Cork Dork

Vivino has no usable public read API for your own cellar. The website login is a
Rails/Devise **HttpOnly session cookie** (no bearer token, invisible to page
JavaScript), and the cellar itself is served by an **Inertia.js** endpoint on
`www.vivino.com` — *not* the `api.vivino.com` mobile backend. A Home Assistant
integration can't drive a browser to log in, so the reliable path is to export
from your own logged-in browser and import the CSV.

The trick: a script pasted into the browser console on `www.vivino.com` makes
**same-origin** requests, so the HttpOnly session cookie is attached
automatically — the script never needs to read it.

## Step 1 — Export from Vivino (browser console)

1. In a desktop browser, log in to <https://www.vivino.com> and open your
   **cellar** page (the URL looks like `https://www.vivino.com/sv/cellars/123456`).
2. Open the developer console: `F12` (or `Cmd+Option+I` on macOS) → **Console**.
3. Paste the whole script below and press **Enter**.
4. It downloads `vivino-wines.csv`. If it can't locate your wine list it prints
   the page's data structure instead — copy that console output and share it,
   and the script can be finalized in one step.

```js
(async () => {
  // ── Read the Inertia page payload embedded in the cellar page ──────
  const appEl = document.getElementById("app") || document.querySelector("[data-page]");
  if (!appEl || !appEl.dataset.page) {
    console.warn("No Inertia #app[data-page] found. Are you on your cellar page (www.vivino.com/.../cellars/<id>)?");
    return;
  }
  const page0 = JSON.parse(appEl.dataset.page);
  const version = page0.version;
  const basePath = location.pathname;            // e.g. /sv/cellars/5398850
  console.log("Inertia component:", page0.component, "| version present:", !!version);

  // ── Recursively find the array of wine/cellar records in props ─────
  const looksLikeWine = (o) =>
    o && typeof o === "object" &&
    (o.wine || o.vintage || (o.name && (o.winery || o.region)) ||
     (o.vintage && o.vintage.wine));
  const findWineArray = (node, path = "props") => {
    let best = null;
    const visit = (n, p) => {
      if (Array.isArray(n)) {
        if (n.length && n.filter(looksLikeWine).length >= Math.ceil(n.length / 2)) {
          if (!best || n.length > best.arr.length) best = { arr: n, path: p };
        }
        n.forEach((x, i) => visit(x, `${p}[${i}]`));
      } else if (n && typeof n === "object") {
        for (const k of Object.keys(n)) visit(n[k], `${p}.${k}`);
      }
    };
    visit(node, path);
    return best;
  };

  // ── Fetch every page via the Inertia XHR protocol ─────────────────
  const fetchPage = async (n) => {
    const url = `${basePath}?page=${n}`;
    const r = await fetch(url, {
      headers: { "X-Inertia": "true", "X-Inertia-Version": version || "", "Accept": "application/json" },
      credentials: "include",
    });
    if (r.status === 409) { console.warn("Inertia version changed — reload the page and re-run."); return null; }
    if (!r.ok) return null;
    return r.json();
  };

  const first = findWineArray(page0.props);
  if (!first) {
    console.warn("Could not locate the wine list in the page data. Structure follows:");
    console.log("props keys:", Object.keys(page0.props || {}));
    console.log(JSON.stringify(page0.props, null, 2).slice(0, 4000));
    console.log("^ Share this so the exporter can target the right field.");
    return;
  }
  console.log("Found wines at:", first.path, "— page 1 count:", first.arr.length);

  const all = [];
  const seen = new Set();
  const collect = (arr) => {
    for (const rec of arr) {
      const id = rec.id || (rec.vintage && rec.vintage.id) || JSON.stringify(rec).length + ":" + (rec.name || "");
      if (seen.has(id)) continue;
      seen.add(id); all.push(rec);
    }
  };
  collect(first.arr);

  for (let n = 2; n <= 100; n++) {
    const pg = await fetchPage(n);
    if (!pg || !pg.props) break;
    const found = findWineArray(pg.props);
    if (!found || !found.arr.length) break;
    const before = all.length;
    collect(found.arr);
    if (all.length === before) break;                 // no new records → done
    if (found.arr.length < first.arr.length) break;   // last (partial) page
  }
  console.log("Total wines collected:", all.length);

  // ── Map to Cork Dork CSV columns ──────────────────────────────────
  const TYPE = { 1: "red", 2: "white", 3: "sparkling", 4: "rosé", 7: "dessert" };
  const g = (o, ...ks) => { for (const k of ks) { if (o && o[k] != null) return o[k]; } return undefined; };
  const csvCell = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const cols = ["name","winery","vintage","type","region","country","grape variety",
                "rating","ratings count","user rating","alcohol","notes"];
  const rows = [cols.join(",")];
  let owned = 0, skipped = 0;
  for (const rec of all) {
    const vintage = rec.vintage || rec;
    const wine = vintage.wine || rec.wine || rec;
    const name = g(wine, "name") || g(vintage, "name");
    if (!name) continue;
    const count = g(rec, "cellar_count", "bottle_count", "count", "quantity", "amount");
    if (count != null && Number(count) <= 0) { skipped++; continue; }
    if (count != null) owned++;
    const winery = (wine.winery || {}).name || "";
    const region = wine.region || {};
    const country = (region.country || {}).name || "";
    const grapes = (wine.grapes || []).map((x) => x.name).filter(Boolean).join(", ");
    const stats = wine.statistics || vintage.statistics || {};
    const review = rec.user_review || rec.review || vintage.user_review || {};
    let year = parseInt(g(vintage, "year"), 10);
    if (!year || year < 1800) year = "";
    const uRating = g(review, "rating") || g(rec, "user_rating");
    rows.push([
      name, winery, year, TYPE[wine.type_id] || g(wine, "type") || "red",
      region.name || "", country, grapes,
      stats.ratings_average ? Number(stats.ratings_average).toFixed(1) : "",
      stats.ratings_count || "",
      uRating ? Math.round(uRating * 2) / 2 : "",
      wine.alcohol ? wine.alcohol + "%" : "",
      (g(review, "note") || "").replace(/\s+/g, " ").trim(),
    ].map(csvCell).join(","));
  }

  if (rows.length < 2) { console.warn("No wines mapped — share the structure printed above."); return; }
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "vivino-wines.csv";
  a.click();
  console.log(`Exported ${rows.length - 1} wines to vivino-wines.csv (owned=${owned}, skipped no-bottle=${skipped}).`);
})();
```

## Step 2 — Import into Cork Dork

1. Open your Cork Dork card → **📦 Inventory**.
2. Click **Import CSV** and choose the `vivino-wines.csv` file.
3. The wines are added to your cellar (unassigned). Open any wine and use
   **Move** to place it into a rack.

Tip: after importing, use **🍇 Vivino Batch Scan** to enrich the wines with the
latest ratings, prices, descriptions, and images from Vivino's public data.

## Notes

- The script only exports bottles you actually own (records with a positive
  bottle count); pure ratings without bottles are skipped.
- If the console prints a data structure instead of downloading a file, share
  that output — it means Vivino's field names differ from the defaults and the
  mapping can be pointed at the right keys.
- A fully automated (no-browser) version is possible with Playwright driving the
  login, then reusing the session cookie against the same Inertia endpoint —
  see `scripts/vivino_login_framework.py` for a starting template. Home
  Assistant itself can't run a browser, so this stays a local companion tool.
