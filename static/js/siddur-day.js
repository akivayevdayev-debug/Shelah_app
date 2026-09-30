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

import { sitePath, routeValue } from "./siddur-reader.js";

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

// ─── Where today's additions live ───────────────────────────────────────────
//
// The day answer says WHAT changes (Hallel, Ya'aleh VeYavo, a fast's
// Selichot); this says WHERE the siddur has it, as links into the page on
// screen's own table of contents. A target the toc doesn't have is dropped,
// so a rite without (say) a Chanukah service just shows the reminder.

function tocPage(toc, serviceSlug, sectionSlug = null) {
    for (const occasion of toc?.occasions || []) {
        for (const service of occasion.services) {
            if (service.slug !== serviceSlug) continue;
            const section = sectionSlug ? service.sections.find((s) => s.slug === sectionSlug) : null;
            if (sectionSlug && !section) return null;
            const entry = section || service;
            return {
                service: serviceSlug,
                section: section ? sectionSlug : null,
                title: entry.title,
                // A service with one section has no section pages of its own.
                single: service.sections.length === 1,
            };
        }
    }
    return null;
}

const FESTIVAL_SONG = Object.freeze({
    pesach: "song-for-passover",
    shavuot: "song-for-shavuot",
    sukkot: "song-for-sukkot",
    "shemini-atzeret": "song-for-shemini-atzeret",
});

const FESTIVALS = Object.freeze(Object.keys(FESTIVAL_SONG));

// The page a reminder points at, as [service, section] candidates in order
// of preference (the first the toc has wins).
function targetsFor(key, day, active) {
    const festival = FESTIVALS.some((k) => active.has(k));
    switch (key) {
    case "hallel":
        return [["rosh-chodesh", "hallel"]];
    case "yaaleh":
        if (festival) return [["shalosh-regalim", "amidah"]];
        return active.has("rosh-chodesh") ? [["rosh-chodesh", "rosh-hodesh"]] : [];
    case "al-hanissim":
        if (active.has("chanukah")) return [["chanukah", "shacharit"]];
        return active.has("purim") || active.has("shushan-purim") ? [["purim", "purim-day"]] : [];
    case "aneinu":
        return day.fastDay === "tisha-beav" ? [["taaniyot", "mourning"]] : day.fastDay ? [["taaniyot", day.fastDay]] : [];
    case "musaf":
        if (active.has("shabbat")) return [["musaf-shabbat", null]];
        if (festival) return [["shalosh-regalim", "mussaf"]];
        return active.has("rosh-chodesh") ? [["rosh-chodesh", "mussaf"]] : [];
    case "omer":
        return [["sefirat-haomer", null]];
    default:
        return [];
    }
}

function resolved(toc, candidates) {
    for (const [service, section] of candidates) {
        const page = tocPage(toc, service, section);
        if (!page) continue;
        const sectionSlug = page.single ? null : page.section;
        return {
            service,
            section: page.section,
            value: routeValue(toc.rite.slug, service, sectionSlug),
            href: sitePath(toc.rite.slug, service, sectionSlug),
            title: page.title,
        };
    }
    return null;
}

// The page a reminder opens: { value, href, title } or null.
export function linkFor(key, day, toc) {
    if (!toc?.rite?.slug || !day) return null;
    return resolved(toc, targetsFor(key, day, new Set(day.occasions || [])));
}

// Pages the day adds that no reminder line covers: a fast's Selichot and
// Torah reading, the menorah, the Megillah, the festival's song. Named by the
// toc's own titles, so nothing is claimed that the siddur doesn't hold.
export function extraPages(day, toc) {
    if (!toc?.rite?.slug || !day) return [];
    const active = new Set(day.occasions || []);
    const wanted = [];
    if (day.fastDay === "tisha-beav") {
        wanted.push(["taaniyot", "mourning"]);
    } else if (day.fastDay) {
        wanted.push(["taaniyot", day.fastDay], ["taaniyot", "torah-reading-for-fast-days"]);
    }
    if (active.has("chanukah")) wanted.push(["chanukah", "menorah-lighting"]);
    if (active.has("purim")) wanted.push(["purim", "megillah-reading"], ["purim", "purim-day"]);
    const holiday = FESTIVALS.find((k) => active.has(k));
    if (holiday) wanted.push(["shalosh-regalim", FESTIVAL_SONG[holiday]]);
    const seen = new Set();
    const pages = [];
    for (const candidate of wanted) {
        const page = resolved(toc, [candidate]);
        if (!page || seen.has(page.value)) continue;
        seen.add(page.value);
        pages.push(page);
    }
    return pages;
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
function pageLink(page, isHebrew, escapeHtml, label = null) {
    const title = isHebrew ? page.title.he : page.title.en;
    // A bare "Open" names nothing out of context: the label carries the page.
    const aria = label ? ` aria-label="${escapeHtml(`${label}: ${title}`)}"` : "";
    return `<a class="siddur-today-link" href="${escapeHtml(page.href)}" data-siddur-path="${escapeHtml(page.value)}"${aria}>${escapeHtml(label || title)}</a>`;
}

export function todayCardMarkup(day, { kind = null, il = false, afterSunset = null, t, escapeHtml, isHebrew = false, headingId = "siddurTodayTitle", toc = null }) {
    const items = reminders(day, { kind, afterSunset, t });
    const covered = new Set();
    const rows = items.map((item) => {
        const page = linkFor(item.key, day, toc);
        if (page) covered.add(page.value);
        const open = page ? ` ${pageLink(page, isHebrew, escapeHtml, t("Open", "פתיחה"))}` : "";
        return `<li data-reminder="${item.key}">${escapeHtml(item.text)}${open}</li>`;
    });
    const list = items.length
        ? `<ul class="siddur-today-list">${rows.join("")}</ul>`
        : `<p class="siddur-today-empty">${escapeHtml(t("No seasonal changes today.", "אין שינויים מיוחדים היום."))}</p>`;
    // Service pages (Shacharit, Mincha...) read one Amidah; the extras belong
    // on the landing page and on the full list.
    const extras = kind === null ? extraPages(day, toc).filter((page) => !covered.has(page.value)) : [];
    const extra = extras.length
        ? `<p class="siddur-today-pages"><span class="siddur-today-pages-label">${escapeHtml(t("Also in the siddur today", "עוד בסידור היום"))}</span>${extras.map((page) => pageLink(page, isHebrew, escapeHtml)).join("")}</p>`
        : "";
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
            ${extra}
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
