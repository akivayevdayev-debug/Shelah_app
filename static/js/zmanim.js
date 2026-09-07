import { setState } from "./state.js";

function normalizeDailyRef(value) {
    const ref = String(value || "").trim();
    return ref || null;
}

function collectRefsFromPayload(payload) {
    if (!payload || typeof payload !== "object") {
        return [];
    }

    const refs = new Set();

    const addRef = (candidate) => {
        const normalized = normalizeDailyRef(candidate);
        if (normalized) {
            refs.add(normalized);
        }
    };

    addRef(payload?.daf_yomi?.ref);
    addRef(payload?.rambam?.ref);
    addRef(payload?.parasha?.ref);
    addRef(payload?.parasha_ref);

    const values = Object.values(payload);
    for (const value of values) {
        if (typeof value === "string") {
            addRef(value);
            continue;
        }
        if (Array.isArray(value)) {
            for (const item of value) {
                if (typeof item === "string") {
                    addRef(item);
                } else if (item && typeof item === "object") {
                    addRef(item.ref);
                    addRef(item.title);
                }
            }
            continue;
        }
        if (value && typeof value === "object") {
            addRef(value.ref);
        }
    }

    return Array.from(refs).slice(0, 9);
}

async function fetchDailyStudyRefs() {
    const response = await fetch("/api/daily-study", {
        method: "GET",
        credentials: "same-origin",
    });
    if (!response.ok) {
        return [];
    }

    const payload = await response.json().catch(() => ({}));
    return collectRefsFromPayload(payload);
}

function postPrewarmToServiceWorker(refs) {
    if (!("serviceWorker" in navigator) || !Array.isArray(refs) || !refs.length) {
        return;
    }

    const message = {
        type: "PREWARM_DAILY",
        refs,
    };

    if (navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage(message);
        return;
    }

    navigator.serviceWorker.ready
        .then((registration) => {
            if (registration?.active) {
                registration.active.postMessage(message);
            }
        })
        .catch(() => {
            // Ignore service worker readiness failures.
        });
}

async function prefetchRefText(ref) {
    const encoded = encodeURIComponent(ref);
    await fetch(`/api/text/${encoded}?autotranslate=0`, {
        method: "GET",
        credentials: "same-origin",
    }).catch(() => null);
}

export async function prewarmDailyStudy() {
    const refs = await fetchDailyStudyRefs();
    if (!refs.length) {
        return [];
    }

    postPrewarmToServiceWorker(refs);
    await Promise.allSettled(refs.map((ref) => prefetchRefText(ref)));

    setState({
        dailyStudy: {
            refs,
            prewarmedAt: new Date().toISOString(),
        },
    });

    return refs;
}

export function installDailyPrewarm() {
    const run = () => {
        window.setTimeout(() => {
            void prewarmDailyStudy();
        }, 2200);
    };

    if (document.readyState === "complete") {
        run();
    } else {
        window.addEventListener("load", run, { once: true });
    }
}

// plan.md §19 Phase 2 -- the zman-clock rendering that used to live entirely
// inline in templates/index.html (initZmanim/fetchZmanimAPI/startCountdown
// and friends). Unrelated to prewarmDailyStudy() above; the two features
// share this file only because plan.md's Phase 2 goal ("give zmanim.js real
// ownership of ... zmanim rendering") named this filename before anyone
// noticed it was already taken by the daily-study prefetch feature.
//
// Dependency contract (§19.9 constraint 3 -- no undeclared inline global):
// every function below that needs one of the inline classic-script globals
// (t, isHebrewMode, translateHolidayName, formatOmerLabel,
// formatWeeklyShabbatLabel, translateShabbatWarning, escapeHtml) takes an
// explicit `deps` object carrying them, rather than reaching for `window.*`
// itself. `installZmanim(deps)` is the one production entry point (called
// once from main.js, which builds `deps` from `window.*` at that single
// wiring boundary); every other export below also takes `deps` explicitly
// so each can be unit-tested in isolation without going through install.
//
// zmanimData / countdownInterval / currentZmanimLocationLabel and the two
// ZMANIM_LOCATION_*_KEY constants are the module's own state now -- they
// used to be bare `let`/`const` at the top of the inline classic script.

const ZMANIM_LOCATION_CACHE_KEY = "Sh'elahLastLocation";
const ZMANIM_LOCATION_LABEL_KEY = "ShelahZmanimLocationLabel";

let zmanimData = null;
let countdownInterval = null;
let currentZmanimLocationLabel = null;

const ZMAN_FIELD_MAP = {
    zDawn: 'Dawn (16.1° / 72m)',
    zTalit: 'Earliest Tallit/Tefillin (10.2°)',
    zSunrise: 'Sunrise',
    zShemaGra: 'Latest Shema (GRA)',
    zShemaBht: 'Latest Shema (Baal HaTanya)',
    zShacharitGra: 'Latest Shacharit (GRA)',
    zShacharitBht: 'Latest Shacharit (Baal HaTanya)',
    zChatzot: 'Chatzot (Midday)',
    zMincha: 'Earliest Mincha (Mincha Gedola)',
    zMusaf: 'Latest Musaf',
    zPlag: 'Plag HaMincha',
    zCandles: 'Candle Lighting',
    zSunset: 'Sunset',
    zMaariv: 'Arvit (Maariv)',
    zNight: 'Nightfall (3 Stars)',
    zHavdalah: 'Havdalah',
    zMidnight: 'Chatzot HaLailah (Midnight)',
};

const ZMAN_ROW_BY_KEY = {
    'Dawn (16.1° / 72m)': 'zRowDawn',
    'Earliest Tallit/Tefillin (10.2°)': 'zRowTalit',
    'Sunrise': 'zRowSunrise',
    'Latest Shema (GRA)': 'shemaGraRow',
    'Latest Shema (Baal HaTanya)': 'shemaBhtRow',
    'Latest Shacharit (GRA)': 'shacharitGraRow',
    'Latest Shacharit (Baal HaTanya)': 'shacharitBhtRow',
    'Chatzot (Midday)': 'zRowChatzot',
    'Earliest Mincha (Mincha Gedola)': 'zRowMincha',
    'Latest Musaf': 'zRowMusaf',
    'Plag HaMincha': 'zRowPlag',
    'Candle Lighting': 'zRowCandles',
    'Sunset': 'zRowSunset',
    'Arvit (Maariv)': 'zRowMaariv',
    'Nightfall (3 Stars)': 'zRowNight',
    'Havdalah': 'zRowHavdalah',
    'Chatzot HaLailah (Midnight)': 'zRowMidnight',
};

export function hasRealZman(value) {
    const raw = String(value || '').trim();
    return Boolean(raw && raw !== 'N/A' && raw !== '--:--');
}

export function setZmanRowVisibility(rowId, visible) {
    const row = document.getElementById(rowId);
    if (!row) return;
    row.classList.toggle('hidden', !visible);
}

export function applyOptionalZmanRows(zmanim) {
    setZmanRowVisibility('zRowMusaf', hasRealZman(zmanim['Latest Musaf']));
    setZmanRowVisibility('zRowCandles', hasRealZman(zmanim['Candle Lighting']));
    setZmanRowVisibility('zRowHavdalah', hasRealZman(zmanim['Havdalah']));
}

export function setZmanimLocationLabel(label, timezone) {
    const labelEl = document.getElementById('zmanLoc');
    if (!labelEl) return;
    const normalizedLabel = String(label || '').trim();
    const fallbackTimezone = String(timezone || '').trim();
    labelEl.classList.remove('sk-line');
    labelEl.style.removeProperty('width');
    labelEl.style.removeProperty('height');
    labelEl.innerText = normalizedLabel || fallbackTimezone || 'Local';
    if (fallbackTimezone && fallbackTimezone !== normalizedLabel) {
        labelEl.setAttribute('title', fallbackTimezone);
    } else {
        labelEl.removeAttribute('title');
    }
}

// Sets the in-memory location label AND persists/renders it. Exists so
// templates/index.html's searchByCity() -- outside this phase's own
// boundary, but broken by moving currentZmanimLocationLabel into this
// module -- has one bridge call instead of reaching into module state
// directly (which ES module exports don't allow anyway).
export function setCurrentZmanimLocationLabel(label) {
    currentZmanimLocationLabel = label;
    localStorage.setItem(ZMANIM_LOCATION_LABEL_KEY, label);
    setZmanimLocationLabel(label, '');
}

// Same reasoning as setCurrentZmanimLocationLabel: searchByCity() used to
// write this cache key directly against the (now-deleted) inline constant.
export function cacheZmanimLocation(location) {
    localStorage.setItem(ZMANIM_LOCATION_CACHE_KEY, JSON.stringify(location));
}

export function formatCountdownDuration(msRemaining, deps) {
    const totalSeconds = Math.max(0, Math.floor(msRemaining / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (deps.isHebrewMode()) {
        if (hours > 0) return `${hours} ש׳ ${minutes} ד׳`;
        if (minutes > 0) return `${minutes} ד׳ ${seconds} ש׳`;
        return `${seconds} ש׳`;
    }

    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
}

export function formatZmanClockDisplay(value, deps) {
    const raw = String(value || '').trim();
    if (!raw) return 'N/A';
    if (!deps.isHebrewMode()) return raw;
    if (raw === 'N/A' || raw === '--:--') return raw;

    const match = raw.match(/^(\d{1,2}:\d{2})(?:\s*([AP]M))?$/i);
    if (!match) return raw;
    const timePart = match[1];
    const suffix = String(match[2] || '').toUpperCase();
    if (!suffix) return timePart;

    const localizedMeridiem = suffix === 'AM' ? 'לפנה"צ' : 'אחה"צ';
    return `${timePart} ${localizedMeridiem}`;
}

export function startCountdown(deps) {
    if (countdownInterval) clearTimeout(countdownInterval);
    const times = [];
    const now = new Date();
    if (!zmanimData) return;
    const nextZmanBadge = document.getElementById('nextZmanBadge');

    const isoMap = (zmanimData.metadata && zmanimData.metadata.zmanim_iso) || {};

    Object.entries(zmanimData.zmanim).forEach(([key, val]) => {
        if (!val || val === 'N/A') return;

        let t = null;
        const iso = isoMap[key];
        if (iso) {
            const parsed = new Date(iso);
            if (!Number.isNaN(parsed.getTime())) {
                t = parsed;
            }
        }

        // Fallback for older payloads without ISO timestamps.
        if (!t) {
            const match = val.match(/(\d+):(\d+)\s+(AM|PM)/);
            if (!match) return;
            let h = parseInt(match[1]);
            let m = parseInt(match[2]);
            if (match[3] === 'PM' && h !== 12) h += 12;
            if (match[3] === 'AM' && h === 12) h = 0;
            t = new Date();
            t.setHours(h, m, 0, 0);
        }

        if (t > now) {
            times.push({ name: key, t });
        }
    });

    document.querySelectorAll('[data-zman-row]').forEach((row) => {
        row.classList.remove('zman-next-highlight');
    });

    times.sort((a, b) => a.t - b.t);
    const nextZman = times.length > 0 ? times[0] : null;

    if (!nextZman) {
        if (nextZmanBadge) {
            nextZmanBadge.classList.add('hidden');
            nextZmanBadge.textContent = '';
        }
        countdownInterval = setTimeout(() => startCountdown(deps), 1000);
        return;
    }

    let highlightRowId = ZMAN_ROW_BY_KEY[nextZman.name];
    if (nextZman.name === 'Latest Shema (Baal HaTanya)' && document.getElementById('shemaBhtRow')?.classList.contains('hidden')) {
        highlightRowId = 'shemaGraRow';
    }
    if (nextZman.name === 'Latest Shacharit (Baal HaTanya)' && document.getElementById('shacharitBhtRow')?.classList.contains('hidden')) {
        highlightRowId = 'shacharitGraRow';
    }

    const row = highlightRowId ? document.getElementById(highlightRowId) : null;
    if (row) {
        row.classList.add('zman-next-highlight');
    }

    if (nextZmanBadge) {
        const rowLabel = row?.querySelector('span')?.textContent?.trim();
        const label = rowLabel || nextZman.name;
        const remaining = Math.max(0, nextZman.t.getTime() - now.getTime());
        const countdownLabel = formatCountdownDuration(remaining, deps);
        nextZmanBadge.innerHTML = `<span class="text-[1.03rem] leading-snug font-semibold text-[#31597d]">${deps.escapeHtml(label)} <span class="font-medium text-[#4f7596]">${deps.t('in', 'בעוד')} ${deps.escapeHtml(countdownLabel)}</span></span>`;
        nextZmanBadge.classList.remove('hidden');
    }

    countdownInterval = setTimeout(() => startCountdown(deps), 1000);
}

// The single "render current zmanimData to the DOM" function -- the
// reconciliation §19.9 constraint 1 requires between fetchZmanimAPI's own
// first-render logic and templates/index.html's toggleLanguage(), which
// duplicated about 80 lines of it for re-localizing the display on a
// language switch. Both now call this (fetchZmanimAPI internally; the
// (inline, unmoved) toggleLanguage() via window.ShelahModules).
//
// Reconciliation decisions, since the two original copies had drifted:
//  - Element-existence guards (`if (!targetEl) return`) come from
//    toggleLanguage's copy, which had them; fetchZmanimAPI's copy assumed
//    every element existed. The guarded version is a strict improvement
//    (no behavior change when elements exist, no crash when they don't).
//  - The GRA/BHT combined-row-or-split decision (equality check) and the
//    holiday/shabbat-name font-hebrew+dir toggling both used to run only
//    from fetchZmanimAPI (row decision) or only from toggleLanguage (font
//    toggling). Both are pure functions of already-fetched data/current
//    language, so it's safe to recompute them unconditionally on every
//    call -- idempotent on a language switch (data unchanged), correct on
//    a fresh fetch (first time the decision needs making). Kept as the
//    always-on canonical behavior rather than splitting them back out.
export function refreshZmanimDisplay(deps) {
    if (!zmanimData || !zmanimData.metadata) return;
    const meta = zmanimData.metadata;
    const z = zmanimData.zmanim || {};

    Object.entries(ZMAN_FIELD_MAP).forEach(([elementId, zmanKey]) => {
        const targetEl = document.getElementById(elementId);
        if (!targetEl) return;
        targetEl.innerText = formatZmanClockDisplay(z[zmanKey] || 'N/A', deps);
    });

    applyOptionalZmanRows(z);

    const shemaGra = z['Latest Shema (GRA)'] || 'N/A';
    const shemaBht = z['Latest Shema (Baal HaTanya)'] || 'N/A';
    const shemaGraRow = document.getElementById('shemaGraRow');
    const shemaBhtRow = document.getElementById('shemaBhtRow');
    const shemaGraLabel = document.getElementById('shemaGraLabel');
    if (shemaGraRow && shemaBhtRow && shemaGraLabel) {
        const combined = shemaGra !== 'N/A' && shemaGra === shemaBht;
        shemaBhtRow.classList.toggle('hidden', combined);
        shemaGraLabel.innerText = combined
            ? deps.t('Latest Shema (GRA / Baal HaTanya)', 'סוף זמן שמע (גר״א / בעל התניא)')
            : deps.t('Latest Shema (GRA)', 'סוף זמן שמע (גר״א)');
    }

    const shacharitGra = z['Latest Shacharit (GRA)'] || 'N/A';
    const shacharitBht = z['Latest Shacharit (Baal HaTanya)'] || 'N/A';
    const shacharitGraRow = document.getElementById('shacharitGraRow');
    const shacharitBhtRow = document.getElementById('shacharitBhtRow');
    const shacharitGraLabel = document.getElementById('shacharitGraLabel');
    if (shacharitGraRow && shacharitBhtRow && shacharitGraLabel) {
        const combined = shacharitGra !== 'N/A' && shacharitGra === shacharitBht;
        shacharitBhtRow.classList.toggle('hidden', combined);
        shacharitGraLabel.innerText = combined
            ? deps.t('Latest Shacharit (GRA / Baal HaTanya)', 'סוף זמן תפילת שחרית (גר״א / בעל התניא)')
            : deps.t('Latest Shacharit (GRA)', 'סוף זמן תפילת שחרית (גר״א)');
    }

    const holidayName = document.getElementById('holidayName');
    if (holidayName) {
        const text = deps.translateHolidayName(meta.holiday || 'Regular Day');
        holidayName.innerText = text;
        if (deps.isHebrewMode() && text !== 'Regular Day') {
            holidayName.classList.add('font-hebrew');
            holidayName.setAttribute('dir', 'rtl');
        } else {
            holidayName.classList.remove('font-hebrew');
            holidayName.removeAttribute('dir');
        }
    }

    const shabbatWeekName = document.getElementById('shabbatWeekName');
    if (shabbatWeekName) {
        const text = deps.formatWeeklyShabbatLabel(meta);
        shabbatWeekName.innerText = text;
        if (deps.isHebrewMode() && text.includes('שבת')) {
            shabbatWeekName.classList.add('font-hebrew');
            shabbatWeekName.setAttribute('dir', 'rtl');
        } else {
            shabbatWeekName.classList.remove('font-hebrew');
            shabbatWeekName.removeAttribute('dir');
        }
    }

    const omerRow = document.getElementById('omerRow');
    const omerCount = document.getElementById('omerCount');
    const omerHint = document.getElementById('omerHint');
    if (omerRow && omerCount && omerHint) {
        if (meta.omer_day) {
            omerCount.innerText = deps.formatOmerLabel(meta);
            omerRow.classList.remove('hidden');
            omerHint.classList.add('hidden');
        } else {
            omerRow.classList.add('hidden');
            omerHint.classList.remove('hidden');
        }
    }

    const warningEl = document.getElementById('zmanimWarning');
    if (warningEl) {
        if (meta.shabbat_warning) {
            warningEl.innerText = deps.translateShabbatWarning(meta.shabbat_warning);
            warningEl.classList.remove('hidden');
        } else {
            warningEl.classList.add('hidden');
        }
    }

    startCountdown(deps);
}

export function fetchZmanimAPI(location = null, deps = {}) {
    let url = '/api/zmanim';
    if (location && Number.isFinite(location.lat) && Number.isFinite(location.lon)) {
        const query = new URLSearchParams({
            lat: String(location.lat),
            lon: String(location.lon),
        });
        url = `/api/zmanim?${query.toString()}`;
    }

    return fetch(url)
        .then((r) => r.json())
        .then((data) => {
            if (!data.error) {
                zmanimData = data;
                const meta = data.metadata || {};
                setZmanimLocationLabel(meta.location_label || currentZmanimLocationLabel || meta.city || meta.timezone || '', meta.timezone);
                refreshZmanimDisplay(deps);
            }
        });
}

export function initZmanim(deps) {
    const cachedRaw = localStorage.getItem(ZMANIM_LOCATION_CACHE_KEY);
    let cachedLocation = null;
    if (cachedRaw) {
        try {
            const parsed = JSON.parse(cachedRaw);
            if (Number.isFinite(parsed?.lat) && Number.isFinite(parsed?.lon)) {
                cachedLocation = { lat: parsed.lat, lon: parsed.lon };
            }
        } catch (_) {
            cachedLocation = null;
        }
    }

    const savedLocationLabel = localStorage.getItem(ZMANIM_LOCATION_LABEL_KEY);
    if (savedLocationLabel) {
        currentZmanimLocationLabel = savedLocationLabel;
    }

    // No GPS permission prompt: a location the user picked earlier via
    // the city search (cached here in localStorage) wins. Otherwise the
    // server resolves a location from the session cookie set by an
    // earlier /set_location call, falling back to IP-geolocation,
    // falling back to a fixed default -- see get_engine() in app.py.
    return fetchZmanimAPI(cachedLocation, deps);
}

export function installZmanim(deps) {
    return initZmanim(deps);
}
