/* Content script for kisskh.* — injects a floating "Download to Plex" button.
 *
 * kisskh is an Angular SPA. The DOM (including the title element) is rendered
 * client-side after the shell HTML loads, and the markup differs per mirror.
 * So rather than hunt for a <h1>, we:
 *   - always show a fixed-position button
 *   - re-derive the current show title/year on each click from the URL
 *     + document.title + any visible title element we can find
 *   - hide the button on non-show pages (home, search, login)
 */
(() => {
  const BTN_ID = "udb-trigger-fab";
  const STATUS_ID = "udb-trigger-fab-status";

  const log = (...a) => console.debug("[udb-trigger]", ...a);

  /** Decide if we're on a show/movie page (vs home / search / login / list). */
  function isShowPage() {
    const p = decodeURIComponent(location.pathname || "");
    if (!p || p === "/" || p === "") return false;

    // Non-content routes
    if (/^\/(search|login|signup|profile|settings|faq|dmca|about|contact|tos|privacy|notification|history|watchlist)(\/|$)/i.test(p)) return false;

    const segs = p.split("/").filter(Boolean);
    if (segs.length === 0) return false;

    // Category listing with no slug (e.g. just "/Drama" or "/Movies")
    if (segs.length === 1 && /^(Drama|Movies|TVSeries|Anime|KShow|Hollywood|Bollywood|Latest|Popular|Top|Ongoing|Completed)$/i.test(segs[0])) return false;

    // Require a "slug" segment — i.e. something long with letters
    // (avoids false positives like /page/2)
    const slug = segs[segs.length - 1];
    if (!/[a-z]/i.test(slug)) return false;
    if (slug.length < 3) return false;

    // Require either the URL to have a known show-type prefix, OR a drama-info
    // element to be present in the DOM (kisskh renders one on show pages).
    const looksLikeShowUrl = /^(Drama|Movies|TVSeries|Anime|KShow|Hollywood|Bollywood)/i.test(segs[0] || "");
    const hasShowDom =
      !!document.querySelector(
        "app-drama-info, app-drama, app-watch, app-episode, .drama-info, .drama-details, .watch-container"
      );

    return looksLikeShowUrl || hasShowDom;
  }

  /** Try many strategies to get the show title + year. */
  function extractTitle() {
    // 1. Visible title elements (ordered by specificity)
    const selectors = [
      "h1",
      "mat-card-title",
      ".drama-title",
      ".title",
      "[class*='title']",
      "app-drama-info h2, app-drama-info h1, app-drama-info .name",
    ];
    let rawTitle = null;
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent && el.textContent.trim().length > 1) {
        rawTitle = el.textContent.trim();
        break;
      }
    }

    // 2. Open Graph / document title fallback
    if (!rawTitle) {
      const og = document.querySelector('meta[property="og:title"]');
      if (og && og.content) rawTitle = og.content.trim();
    }
    if (!rawTitle) {
      // strip " | kisskh" style suffixes
      rawTitle = document.title.replace(/\s*[\|\-–—]\s*kisskh.*/i, "").trim();
    }

    // 3. URL slug fallback (last path segment, decoded + de-slugified)
    let slugTitle = null;
    const segs = location.pathname.split("/").filter(Boolean);
    if (segs.length) {
      slugTitle = decodeURIComponent(segs[segs.length - 1])
        .replace(/[-_]+/g, " ")
        .replace(/\bep(isode)?\s*\d+\b.*/i, "") // trim /.../Episode-3
        .trim();
    }

    const name =
      (rawTitle || slugTitle || "")
        .replace(/\(\d{4}\)/, "")
        .replace(/\s+/g, " ")
        .trim();

    // Year — try many signals in descending order of reliability.
    let year = null;

    // (a) year embedded in the raw title element: "Show Name (2025)"
    let ym = rawTitle && rawTitle.match(/\((\d{4})\)/);

    // (b) explicit <time datetime="YYYY-..."> element
    if (!ym) {
      const t = document.querySelector("time[datetime]");
      if (t) {
        const m = (t.getAttribute("datetime") || "").match(/(\d{4})/);
        if (m) ym = m;
      }
    }

    // (c) elements whose class or id hints at release/aired/year
    if (!ym) {
      const hinted = document.querySelectorAll(
        "[class*='release' i], [class*='aired' i], [class*='year' i], [id*='release' i], [id*='year' i]"
      );
      for (const el of hinted) {
        const txt = (el.innerText || el.textContent || "").trim();
        const m = txt.match(/(19|20)\d{2}/);
        if (m) { ym = m; break; }
      }
    }

    // (d) body text patterns: "Released: 2026", "Release Date: Apr 2026",
    //     "Aired: 2026", "Year: 2026", "First aired 2026"
    if (!ym) {
      const body = document.body.innerText || "";
      ym =
        body.match(/(?:Released?|Release\s*Date|Aired|First\s*aired|Year)[:\s]+[^\n]*?((?:19|20)\d{2})/i) ||
        body.match(/\((\d{4})\)/);
    }

    // (e) Open Graph / meta tags
    if (!ym) {
      const metas = [
        'meta[property="og:video:release_date"]',
        'meta[property="video:release_date"]',
        'meta[name="release_date"]',
        'meta[itemprop="datePublished"]',
      ];
      for (const sel of metas) {
        const m = document.querySelector(sel);
        if (m && m.content) {
          const mm = m.content.match(/(\d{4})/);
          if (mm) { ym = mm; break; }
        }
      }
    }

    if (ym) {
      const y = parseInt(ym[1] || ym[0], 10);
      // Sanity bound. Kisskh ranges ~1960..current+2.
      const nowY = new Date().getFullYear();
      if (y >= 1950 && y <= nowY + 2) year = y;
    }

    // Detect ongoing-vs-completed so we can auto-register the show for daily
    // rescans. Kisskh shows a "Status: Ongoing|Completed" line in the detail
    // pane; we also accept various equivalents.
    let ongoing = false;
    const body = document.body.innerText || "";
    if (/Status[:\s]+Ongoing/i.test(body) || /\bOngoing\b/i.test(body.split("\n").slice(0, 40).join("\n"))) {
      ongoing = true;
    }
    if (/Status[:\s]+Completed/i.test(body)) {
      ongoing = false;
    }

    return { name, year, raw: rawTitle, ongoing };
  }

  function ensureFab() {
    let btn = document.getElementById(BTN_ID);
    if (btn) return btn;

    btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.className = "udb-trigger-fab";
    btn.innerHTML = "⬇ <span>Download to Plex</span>";
    btn.title = "Queue this show on the Umbrel server";

    const status = document.createElement("div");
    status.id = STATUS_ID;
    status.className = "udb-trigger-fab-status";

    document.body.appendChild(btn);
    document.body.appendChild(status);

    btn.addEventListener("click", onClick);
    return btn;
  }

  function setStatus(text, cls) {
    const el = document.getElementById(STATUS_ID);
    if (!el) return;
    el.textContent = text;
    el.className = "udb-trigger-fab-status " + (cls || "");
    if (text) {
      clearTimeout(el._hideTimer);
      el._hideTimer = setTimeout(() => {
        el.textContent = "";
        el.className = "udb-trigger-fab-status";
      }, 6000);
    }
  }

  /** Ask the user for a year via an inline prompt. Resolves to int or null. */
  function askForYear(defaultName) {
    return new Promise((resolve) => {
      // Remove any previous prompt
      const old = document.getElementById("udb-trigger-year-prompt");
      if (old) old.remove();

      const wrap = document.createElement("div");
      wrap.id = "udb-trigger-year-prompt";
      wrap.className = "udb-trigger-year-prompt";
      wrap.innerHTML = `
        <div class="udb-trigger-year-prompt-inner">
          <div class="udb-trigger-year-prompt-label">
            Couldn't auto-detect year for<br><b></b><br>
            <span>Enter release year so udb can pick the right match:</span>
          </div>
          <div class="udb-trigger-year-prompt-row">
            <input type="number" min="1950" max="2099" step="1" placeholder="YYYY" />
            <button class="ok">Queue</button>
            <button class="cancel">Cancel</button>
          </div>
        </div>
      `;
      wrap.querySelector("b").textContent = defaultName;
      document.body.appendChild(wrap);

      const input = wrap.querySelector("input");
      const okBtn = wrap.querySelector("button.ok");
      const cancelBtn = wrap.querySelector("button.cancel");

      const done = (val) => {
        wrap.remove();
        resolve(val);
      };
      okBtn.addEventListener("click", () => {
        const v = parseInt(input.value, 10);
        if (isNaN(v) || v < 1950 || v > 2099) {
          input.focus();
          input.style.outline = "2px solid #ff5252";
          return;
        }
        done(v);
      });
      cancelBtn.addEventListener("click", () => done(null));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") okBtn.click();
        if (e.key === "Escape") cancelBtn.click();
      });
      setTimeout(() => input.focus(), 10);
    });
  }

  async function onClick() {
    const btn = document.getElementById(BTN_ID);
    const title = extractTitle();
    if (!title.name) {
      setStatus("✗ couldn't determine show title", "err");
      return;
    }

    // If we couldn't scrape a year, ask for it inline — submitting without a
    // year to kisskh (which returns many matches per keyword) drops udb into
    // an interactive "Select one of the above:" prompt and the job fails.
    let year = title.year;
    if (!year) {
      setStatus("No year detected — asking…", "pending");
      year = await askForYear(title.name);
      if (!year) {
        setStatus("✗ cancelled (year required)", "err");
        return;
      }
    }

    btn.disabled = true;
    setStatus(`Queueing: ${title.name} (${year})…`, "pending");

    const payload = {
      name: title.name,
      year,
      series_type: 2, // KissKh
      // Always pin a resolution so udb never drops to an interactive prompt
      // when 720 isn't available — the KissKh client's alternate_resolution_
      // selector falls back automatically.
      resolution: "720",
      // If the page looks like an ongoing show, auto-register it for daily
      // rescans so new episodes get downloaded without re-clicking.
      watch: !!title.ongoing,
      source_url: location.href,
    };

    try {
      const resp = await chrome.runtime.sendMessage({ type: "enqueue", payload });
      if (resp && resp.ok) {
        const watchNote = payload.watch ? " · watching" : "";
        setStatus(`✓ queued as job #${resp.job_id}${watchNote}`, "ok");
      } else {
        setStatus(`✗ ${resp?.error || "failed"}`, "err");
      }
    } catch (e) {
      setStatus(`✗ ${e.message}`, "err");
    } finally {
      setTimeout(() => {
        btn.disabled = false;
      }, 1500);
    }
  }

  function updateVisibility() {
    const btn = ensureFab();
    btn.style.display = isShowPage() ? "" : "none";
  }

  // Initial + watch for SPA navigation
  updateVisibility();

  // Angular route changes don't trigger popstate in all cases; patch history.
  const fire = () => updateVisibility();
  ["pushState", "replaceState"].forEach((fn) => {
    const orig = history[fn];
    history[fn] = function () {
      const r = orig.apply(this, arguments);
      window.dispatchEvent(new Event("udb-locationchange"));
      return r;
    };
  });
  window.addEventListener("popstate", fire);
  window.addEventListener("udb-locationchange", fire);

  // Plus a light DOM observer for initial render — debounced so we don't
  // re-evaluate on every mutation.
  let moTimer = null;
  const mo = new MutationObserver(() => {
    if (moTimer) return;
    moTimer = setTimeout(() => {
      moTimer = null;
      updateVisibility();
    }, 300);
  });
  mo.observe(document.body, { childList: true, subtree: true });

  log("udb-trigger content script loaded on", location.href);
})();
