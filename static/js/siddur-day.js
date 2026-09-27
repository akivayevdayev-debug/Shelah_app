// "Today in the siddur" (Prayers redesign; research brief (a)4): which
// seasonal and occasional texts apply, per service -- the Kol Yaakob
// "Today's Prayers guidance" model: reminders beside the siddur, never text
// hidden or rewritten inside it.
//
// The server answers for a given Hebrew day and Israel/Diaspora
// (/api/siddur/v2/day, a pure function of its URL). Which Hebrew day it is
// depends on the user's own sunset, so that choice is made here: after
// today's sunset (from the zmanim panel's location, when it has one) the
// Hebrew day is tomorrow's civil date. Without a known sunset the user
// flips it with the "after sunset" switch -- a guess from the clock would be
// wrong somewhere every evening.

const ISRAEL_KEY = "shelah.siddur.il";

// Israel's rough bounding box, for a first guess from the zmanim location.
const ISRAEL_BOX = Object.freeze({ south: 29.45, north: 33.35, west: 34.2, east: 35.9 });

export function civilDate(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
}

export function addDays(ymd, days) {
    const [y, m, d] = ymd.split("-").map(Number);
    return civilDate(new Date(y, m - 1, d + days));
}

// { date: civil date whose daytime is the Hebrew day now, afterSunset }.
// afterSunset is null when there's no sunset to go by; `forceEvening`
// (the manual switch) wins over both.
export function hebrewDayFor(now, sunset = null, forceEvening = null) {
    const today = civilDate(now);
    let afterSunset = null;
    if (forceEvening !== null && forceEvening !== undefined) {
        afterSunset = Boolean(forceEvening);
    } else if (sunset instanceof Date && !Number.isNaN(sunset.getTime()) && civilDate(sunset) === today) {
        afterSunset = now >= sunset;
    }
    return { date: afterSunset ? addDays(today, 1) : today, afterSunset };
}

export function guessIsrael(location) {
    const lat = Number(location?.lat);
    const lon = Number(location?.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return false;
    return lat >= ISRAEL_BOX.south && lat <= ISRAEL_BOX.north && lon >= ISRAEL_BOX.west && lon <= ISRAEL_BOX.east;
}

// The saved choice, or null when the user hasn't made one.
export function readIsrael(storage = globalThis.localStorage) {
    try {
        const value = storage?.getItem(ISRAEL_KEY);
        return value === "1" ? true : value === "0" ? false : null;
    } catch (_) {
        return null;
    }
}

export function writeIsrael(value, storage = globalThis.localStorage) {
    try {
        storage?.setItem(ISRAEL_KEY, value ? "1" : "0");
    } catch (_) {
        // Private mode / storage off: the choice lasts for this page only.
    }
}

// One request per (day, il); a failed one is forgotten so a retry refetches.
export function createDayClient(fetchImpl = globalThis.fetch) {
    const cache = new Map();
    return {
        get(date, il) {
            const key = `${date}|${il ? 1 : 0}`;
            if (!cache.has(key)) {
                const request = fetchImpl(`/api/siddur/v2/day?date=${encodeURIComponent(date)}&il=${il ? 1 : 0}`)
                    .then((response) => {
                        if (!response.ok) throw new Error(`day guidance ${response.status}`);
                        return response.json();
                    })
                    .catch((error) => {
                        cache.delete(key);
                        throw error;
                    });
                cache.set(key, request);
            }
            return cache.get(key);
        },
    };
}

// Which Amidah a service slug is (for the per-service reminders); null for
// anything else, where the full list shows.
const SERVICE_KIND = Object.freeze({
    shacharit: "shacharit",
    "shacharit-shabbat": "shacharit",
    mincha: "mincha",
    "mincha-shabbat": "mincha",
    arbit: "arbit",
    "arbit-shabbat": "arbit",
    "musaf-shabbat": "musaf",
});

export function serviceKind(serviceSlug) {
    return SERVICE_KIND[serviceSlug] || null;
}

function isYomTov(active) {
    if (["shavuot", "shemini-atzeret", "rosh-hashana", "yom-kippur"].some((key) => active.has(key))) return true;
    if (active.has("pesach") && !active.has("chol-hamoed-pesach")) return true;
    return active.has("sukkot") && !active.has("chol-hamoed-sukkot");
}

// The occasion keys that light up the siddur's own rubrics (line `when`
// tags, backend/siddur_lines.py): the day's occasions plus this season's
// form of Birkat HaShanim.
export function activeTags(day) {
    const tags = new Set(day?.occasions || []);
    if (day?.birkatHashanim) tags.add(day.birkatHashanim);
    return tags;
}

const NAMES = Object.freeze({
    mashiv: ["Mashiv HaRuach", "משיב הרוח"],
    morid: ["Morid HaTal", "מוריד הטל"],
});

function gevurotText(gevurot, kind, t) {
    const name = (key) => t(...NAMES[key]);
    if (kind) {
        return t(`In the Amidah: ${name(gevurot[kind])}`, `בעמידה: ${name(gevurot[kind])}`);
    }
    const before = gevurot.shacharit;
    const after = gevurot.musaf;
    if (before === after) return t(`In the Amidah: ${name(before)}`, `בעמידה: ${name(before)}`);
    return t(`${name(before)} until Musaf, then ${name(after)}`, `${name(before)} עד מוסף, ואחר כך ${name(after)}`);
}

// [{ key, text }] for the Today card, most consequential first. `kind`
// narrows it to one service (serviceKind); null lists everything.
export function reminders(day, { kind = null, afterSunset = null, t }) {
    if (!day) return [];
    const active = new Set(day.occasions || []);
    const weekdayAmidah = !active.has("shabbat") && !isYomTov(active);
    const items = [];
    const add = (key, text) => items.push({ key, text });

    if (day.aseretYemeiTeshuva && kind !== "musaf") {
        add("ayt", t(
            "Ten Days of Repentance: HaMelech HaKadosh, Zochrenu and the other Amidah changes",
            "עשרת ימי תשובה: המלך הקדוש, זכרנו ושאר השינויים בעמידה",
        ));
    }
    if (day.hallel?.kind !== "none" && (kind === null || kind === "shacharit")) {
        add("hallel", day.hallel.kind === "full"
            ? t("Full Hallel, with a beracha", "הלל שלם בברכה")
            : t("Half Hallel, without a beracha (Sephardic custom)", "חצי הלל, בלא ברכה (מנהג הספרדים)"));
    }
    if (day.yaalehVeyavo) add("yaaleh", t("Ya'aleh VeYavo in the Amidah and Birkat Hamazon", "יעלה ויבוא בעמידה ובברכת המזון"));
    if (day.alHanissim) add("al-hanissim", t("Al HaNissim in the Amidah and Birkat Hamazon", "על הניסים בעמידה ובברכת המזון"));
    if (day.alHanissimJerusalem) add("al-hanissim", t("In Jerusalem: Al HaNissim (Shushan Purim)", "בירושלים: על הניסים (שושן פורים)"));
    if (day.gevurot) add("gevurot", gevurotText(day.gevurot, kind, t));
    if (weekdayAmidah && kind !== "musaf" && day.birkatHashanim) {
        add("birkat-hashanim", day.birkatHashanim === "barech-alenu"
            ? t("Barech Alenu (the winter blessing for rain)", "ברך עלינו (נוסח החורף)")
            : t("Barechenu (the summer blessing)", "ברכנו (נוסח הקיץ)"));
    }
    if (day.tachanun && (kind === null || kind === "shacharit" || kind === "mincha")) {
        if (active.has("shabbat")) {
            if (kind !== "shacharit") {
                add("tachanun", day.tachanun.tzidkatcha
                    ? t("Tzidkatcha at Mincha", "צדקתך במנחה")
                    : t("No Tzidkatcha at Mincha", "אין אומרים צדקתך במנחה"));
            }
        } else {
            const says = kind ? day.tachanun[kind] : null;
            if (kind) {
                add("tachanun", says ? t("Tachanun is said", "אומרים תחנון") : t("No Tachanun today", "אין אומרים תחנון"));
            } else if (day.tachanun.shacharit === day.tachanun.mincha) {
                add("tachanun", day.tachanun.shacharit ? t("Tachanun is said", "אומרים תחנון") : t("No Tachanun today", "אין אומרים תחנון"));
            } else {
                add("tachanun", day.tachanun.shacharit
                    ? t("Tachanun at Shacharit, not at Mincha", "תחנון בשחרית, לא במנחה")
                    : t("No Tachanun at Shacharit; said at Mincha", "אין תחנון בשחרית; אומרים במנחה"));
            }
        }
    }
    if (day.aneinu && kind !== "musaf") add("aneinu", t("Fast day: Aneinu", "תענית: עננו"));
    if (day.musaf && kind === null) add("musaf", t("Musaf is said today", "אומרים מוסף"));
    const omer = afterSunset ? day.omer?.today : day.omer?.tonight;
    if (omer && (kind === null || kind === "arbit")) {
        add("omer", afterSunset
            ? t(`Tonight: day ${omer} of the Omer`, `הלילה: יום ${omer} לעומר`)
            : t(`Tonight after nightfall: day ${omer} of the Omer`, `הלילה אחר צאת הכוכבים: יום ${omer} לעומר`));
    }
    return items;
}

export function hebrewDateText(day, isHebrew) {
    const h = day?.hebrew;
    if (!h) return "";
    return isHebrew ? h.he : `${h.day} ${h.monthName} ${h.year}`;
}

// The Today card. `escapeHtml` escapes text; everything else is ours.
export function todayCardMarkup(day, { kind = null, il = false, afterSunset = null, t, escapeHtml, isHebrew = false, headingId = "siddurTodayTitle" }) {
    const items = reminders(day, { kind, afterSunset, t });
    const list = items.length
        ? `<ul class="siddur-today-list">${items.map((item) => `<li data-reminder="${item.key}">${escapeHtml(item.text)}</li>`).join("")}</ul>`
        : `<p class="siddur-today-empty">${escapeHtml(t("No seasonal changes today.", "אין שינויים מיוחדים היום."))}</p>`;
    const evening = afterSunset === null
        ? t("Before sunset", "לפני השקיעה")
        : afterSunset ? t("After sunset", "אחרי השקיעה") : t("Before sunset", "לפני השקיעה");
    return `
        <section class="siddur-today" aria-labelledby="${headingId}">
            <div class="siddur-today-head">
                <h2 id="${headingId}" class="siddur-today-title">${escapeHtml(t("Today in the siddur", "היום בסידור"))}</h2>
                <p class="siddur-today-date">${escapeHtml(hebrewDateText(day, isHebrew))}</p>
            </div>
            ${list}
            <div class="siddur-today-controls">
                <button type="button" class="siddur-chip" data-siddur-action="toggle-israel" aria-pressed="${il ? "true" : "false"}">
                    ${escapeHtml(il ? t("In Israel", "בארץ ישראל") : t("Outside Israel", "בחוץ לארץ"))}
                </button>
                <button type="button" class="siddur-chip" data-siddur-action="toggle-evening" aria-pressed="${afterSunset ? "true" : "false"}">
                    ${escapeHtml(evening)}
                </button>
            </div>
            ${items.some((item) => item.key === "tachanun") ? `<p class="siddur-today-note">${escapeHtml(t(
                "Days the calendar can't know (a mourner's house, a groom, a brit) can also change Tachanun.",
                "ימים שהלוח אינו יודע עליהם (בית אבל, חתן, ברית) משנים גם הם את התחנון.",
            ))}</p>` : ""}
        </section>`;
}
