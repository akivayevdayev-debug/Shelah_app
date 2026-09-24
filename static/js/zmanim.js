import { setState } from "./state.js";

function normalizeDailyRef(value) {
    const ref = String(value || "").trim();
    return ref || null;
}

// Split out of collectRefsFromPayload() so this per-value shape dispatch
// isn't nested inside that function's own loop (SonarCloud javascript:S3776).
function addRefsFromArrayItem(item, addRef) {
    if (typeof item === "string") {
        addRef(item);
    } else if (item && typeof item === "object") {
        addRef(item.ref);
        addRef(item.title);
    }
}

function addRefsFromValue(value, addRef) {
    if (typeof value === "string") {
        addRef(value);
        return;
    }
    if (Array.isArray(value)) {
        for (const item of value) {
            addRefsFromArrayItem(item, addRef);
        }
        return;
    }
    if (value && typeof value === "object") {
        addRef(value.ref);
    }
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

    for (const value of Object.values(payload)) {
        addRefsFromValue(value, addRef);
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
    zFastStart: 'Fast Starts',
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
    zFastEnd: 'Fast Ends',
    zHavdalah: 'Havdalah',
    zMidnight: 'Chatzot HaLailah (Midnight)',
};

const ZMAN_ROW_BY_KEY = {
    'Dawn (16.1° / 72m)': 'zRowDawn',
    'Fast Starts': 'zRowFastStart',
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
    'Fast Ends': 'zRowFastEnd',
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
    setZmanRowVisibility('zRowFastStart', hasRealZman(zmanim['Fast Starts']));
    setZmanRowVisibility('zRowFastEnd', hasRealZman(zmanim['Fast Ends']));
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

    const match = /^(\d{1,2}:\d{2})(?:\s*([AP]M))?$/i.exec(raw);
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

    const isoMap = zmanimData.metadata?.zmanim_iso || {};

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
            const match = /(\d{1,2}):(\d{2})\s+(AM|PM)/.exec(val);
            if (!match) return;
            let h = Number.parseInt(match[1]);
            let m = Number.parseInt(match[2]);
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

function renderZmanClockFields(z, deps) {
    Object.entries(ZMAN_FIELD_MAP).forEach(([elementId, zmanKey]) => {
        const targetEl = document.getElementById(elementId);
        if (!targetEl) return;
        targetEl.innerText = formatZmanClockDisplay(z[zmanKey] || 'N/A', deps);
    });
}

// The GRA and Baal HaTanya "latest" zmanim are one combined row when both
// resolve to the same clock time, two rows otherwise.
const SHEMA_GRA_BHT_ROW = {
    graKey: 'Latest Shema (GRA)',
    bhtKey: 'Latest Shema (Baal HaTanya)',
    graRowId: 'shemaGraRow',
    bhtRowId: 'shemaBhtRow',
    labelId: 'shemaGraLabel',
    combinedLabel: ['Latest Shema (GRA / Baal HaTanya)', 'סוף זמן שמע (גר״א / בעל התניא)'],
    splitLabel: ['Latest Shema (GRA)', 'סוף זמן שמע (גר״א)'],
};

const SHACHARIT_GRA_BHT_ROW = {
    graKey: 'Latest Shacharit (GRA)',
    bhtKey: 'Latest Shacharit (Baal HaTanya)',
    graRowId: 'shacharitGraRow',
    bhtRowId: 'shacharitBhtRow',
    labelId: 'shacharitGraLabel',
    combinedLabel: ['Latest Shacharit (GRA / Baal HaTanya)', 'סוף זמן תפילת שחרית (גר״א / בעל התניא)'],
    splitLabel: ['Latest Shacharit (GRA)', 'סוף זמן תפילת שחרית (גר״א)'],
};

function renderGraBhtRow(z, deps, row) {
    const gra = z[row.graKey] || 'N/A';
    const bht = z[row.bhtKey] || 'N/A';
    const graRow = document.getElementById(row.graRowId);
    const bhtRow = document.getElementById(row.bhtRowId);
    const graLabel = document.getElementById(row.labelId);
    if (!graRow || !bhtRow || !graLabel) return;

    const combined = gra !== 'N/A' && gra === bht;
    bhtRow.classList.toggle('hidden', combined);
    graLabel.innerText = combined ? deps.t(...row.combinedLabel) : deps.t(...row.splitLabel);
}

function setHebrewRtlStyle(el, enabled) {
    if (enabled) {
        el.classList.add('font-hebrew');
        el.setAttribute('dir', 'rtl');
    } else {
        el.classList.remove('font-hebrew');
        el.removeAttribute('dir');
    }
}

function renderHolidayName(meta, deps) {
    const holidayName = document.getElementById('holidayName');
    if (!holidayName) return;
    const text = deps.translateHolidayName(meta.holiday || 'Regular Day');
    holidayName.innerText = text;
    setHebrewRtlStyle(holidayName, deps.isHebrewMode() && text !== 'Regular Day');
}

function renderShabbatWeekName(meta, deps) {
    const shabbatWeekName = document.getElementById('shabbatWeekName');
    if (!shabbatWeekName) return;
    const text = deps.formatWeeklyShabbatLabel(meta);
    shabbatWeekName.innerText = text;
    setHebrewRtlStyle(shabbatWeekName, deps.isHebrewMode() && text.includes('שבת'));
}

function renderOmerRow(meta, deps) {
    const omerRow = document.getElementById('omerRow');
    const omerCount = document.getElementById('omerCount');
    const omerHint = document.getElementById('omerHint');
    if (!omerRow || !omerCount || !omerHint) return;

    if (meta.omer_day) {
        omerCount.innerText = deps.formatOmerLabel(meta);
        omerRow.classList.remove('hidden');
        omerHint.classList.add('hidden');
    } else {
        omerRow.classList.add('hidden');
        omerHint.classList.remove('hidden');
    }
}

function renderShabbatWarning(meta, deps) {
    const warningEl = document.getElementById('zmanimWarning');
    if (!warningEl) return;

    if (meta.shabbat_warning) {
        warningEl.innerText = deps.translateShabbatWarning(meta.shabbat_warning);
        warningEl.classList.remove('hidden');
    } else {
        warningEl.classList.add('hidden');
    }
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
    if (!zmanimData?.metadata) return;
    const meta = zmanimData.metadata;
    const z = zmanimData.zmanim || {};

    renderZmanClockFields(z, deps);
    applyOptionalZmanRows(z);
    renderGraBhtRow(z, deps, SHEMA_GRA_BHT_ROW);
    renderGraBhtRow(z, deps, SHACHARIT_GRA_BHT_ROW);
    renderHolidayName(meta, deps);
    renderShabbatWeekName(meta, deps);
    renderOmerRow(meta, deps);
    renderShabbatWarning(meta, deps);

    startCountdown(deps);
}

// A failed/errored fetch used to leave the panel exactly as it started --
// "--:--" placeholders and (on first load) a shimmering #zmanLoc skeleton,
// forever, with no indication anything went wrong. Self-recovers with a
// couple of automatic retries, then falls back to a visible retry control
// so the panel never gets permanently stuck.
const ZMANIM_RETRY_DELAYS_MS = [3000, 8000];
let zmanimRetryAttempts = 0;
let zmanimRetryTimer = null;

function clearZmanimLoadError() {
    zmanimRetryAttempts = 0;
    if (zmanimRetryTimer) {
        clearTimeout(zmanimRetryTimer);
        zmanimRetryTimer = null;
    }
    const warningEl = document.getElementById('zmanimWarning');
    if (warningEl?.dataset.zmanimLoadError === '1') {
        warningEl.classList.add('hidden');
        warningEl.textContent = '';
        delete warningEl.dataset.zmanimLoadError;
    }
}

function showZmanimLoadError(location, deps) {
    const warningEl = document.getElementById('zmanimWarning');
    if (warningEl) {
        warningEl.dataset.zmanimLoadError = '1';
        warningEl.classList.remove('hidden');
        warningEl.textContent = '';

        const msg = document.createElement('span');
        msg.textContent = deps.t
            ? deps.t("Couldn't load zmanim times.", 'לא ניתן היה לטעון את הזמנים.')
            : "Couldn't load zmanim times.";

        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'underline font-semibold ms-1';
        retryBtn.textContent = deps.t ? deps.t('Retry', 'נסה שוב') : 'Retry';
        retryBtn.addEventListener('click', () => {
            clearZmanimLoadError();
            fetchZmanimAPI(location, deps);
        });

        warningEl.append(msg, retryBtn);
    }

    // Stop the location label from shimmering forever if this is the
    // first-ever load (it starts as a loading skeleton, not "--:--").
    const locEl = document.getElementById('zmanLoc');
    if (locEl?.classList.contains('sk-line')) {
        locEl.classList.remove('sk-line');
        locEl.style.removeProperty('width');
        locEl.style.removeProperty('height');
        locEl.innerText = deps.t ? deps.t('Unavailable', 'לא זמין') : 'Unavailable';
    }
}

function scheduleZmanimRetry(location, deps) {
    showZmanimLoadError(location, deps);
    if (zmanimRetryAttempts >= ZMANIM_RETRY_DELAYS_MS.length) return; // manual retry only from here on
    const delay = ZMANIM_RETRY_DELAYS_MS[zmanimRetryAttempts];
    zmanimRetryAttempts += 1;
    zmanimRetryTimer = setTimeout(() => {
        zmanimRetryTimer = null;
        fetchZmanimAPI(location, deps);
    }, delay);
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
                clearZmanimLoadError();
            } else {
                scheduleZmanimRetry(location, deps);
            }
        })
        .catch(() => {
            scheduleZmanimRetry(location, deps);
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
            // A corrupt cache entry is not an error: treat it as "no cached location"
            // (cachedLocation is still null; it is only assigned after a clean parse).
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
