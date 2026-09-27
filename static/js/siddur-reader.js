// Siddur markup and reader behavior (Prayers redesign, audit §2.5).
//
// One table of contents feeds every entry point (the Texts menu, the
// sidebar, the rite's landing page) -- audit N6's "one navigation model".
// A service reads as one continuous page in the siddur's own order, its
// sections addressable by path (/siddur/<rite>/<service>/<section>) and
// listed in a sticky section picker; scrolling moves the URL with
// replaceState, never a new history entry (audit U2/U4).
//
// Lines arrive typed (backend/siddur_lines.py). Each type has its own
// non-color cue (WCAG 1.4.1): instructions are smaller and set off by a
// dotted inline-start rule; conditional text by a solid rule and its label;
// a rubric that applies today gets a visible "Today" badge, not a tint.
// Line HTML is the server's sanitized subset (b/i/small/br); it is
// re-checked here before insertion.

const SAFE_TAG_RE = /<(?!\/?(?:b|i|small)>|br>)/g;

export function safeLineHtml(html) {
    return String(html || "").replace(SAFE_TAG_RE, "&lt;");
}

export function sitePath(rite, service = null, section = null) {
    return `/${["siddur", rite, service, section].filter(Boolean).join("/")}`;
}

export function routeValue(rite, service = null, section = null) {
    return [rite, service, section].filter(Boolean).join("/");
}

// "edot-hamizrach/shacharit/amida" -> parts, or null when malformed.
const VALUE_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*(?:\/[a-z0-9]+(?:-[a-z0-9]+)*){0,2}$/;

export function parseValue(value) {
    const text = String(value || "");
    if (!VALUE_RE.test(text)) return null;
    const [rite, service = null, section = null] = text.split("/");
    return { rite, service, section };
}

// Where a route value points in the toc: { occasion, service, section,
// index } (section null for a multi-section service opened without one),
// or null when the toc has no such page.
export function locate(toc, { service, section }) {
    if (!service) return { occasion: null, service: null, section: null, index: -1 };
    const services = [];
    for (const occasion of toc.occasions) {
        for (const entry of occasion.services) services.push({ occasion, entry });
    }
    const index = services.findIndex(({ entry }) => entry.slug === service);
    if (index === -1) return null;
    const { occasion, entry } = services[index];
    if (section) {
        if (entry.sections.length === 1) return null;
        const found = entry.sections.find((s) => s.slug === section);
        if (!found) return null;
        return { occasion, service: entry, section: found, index, services };
    }
    return { occasion, service: entry, section: entry.sections.length === 1 ? entry.sections[0] : null, index, services };
}

function titleFor(entry, isHebrew) {
    return isHebrew ? entry.title.he : entry.title.en;
}

// The label a view gets (tab title, Recent, bookmarks).
export function viewLabel(toc, where, isHebrew) {
    const rite = isHebrew ? toc.rite.title.he : toc.rite.title.en;
    if (!where?.service) return isHebrew ? `סידור · ${rite}` : `Siddur · ${rite}`;
    const service = titleFor(where.service, isHebrew);
    if (where.section && where.service.sections.length > 1) return `${titleFor(where.section, isHebrew)} · ${service}`;
    return service;
}

// ─── Table of contents (menus) ──────────────────────────────────────────────

export function tocMarkup(toc, { escapeHtml, isHebrew = false, current = null, compact = false }) {
    const groups = toc.occasions.map((occasion, index) => {
        const links = occasion.services.map((service) => {
            const primary = titleFor(service, isHebrew);
            const secondary = isHebrew ? service.title.en : service.title.he;
            const isCurrent = current === service.slug;
            return `<li><a class="siddur-toc-link" href="${sitePath(toc.rite.slug, service.slug)}" data-siddur-path="${routeValue(toc.rite.slug, service.slug)}"${isCurrent ? ' aria-current="page"' : ""}>
                <span class="siddur-toc-name">${escapeHtml(primary)}</span>
                <span class="siddur-toc-alt" lang="${isHebrew ? "en" : "he"}" dir="${isHebrew ? "ltr" : "rtl"}">${escapeHtml(secondary)}</span>
            </a></li>`;
        }).join("");
        const heading = escapeHtml(isHebrew ? occasion.title.he : occasion.title.en);
        if (compact) {
            const open = index === 0 || occasion.services.some((s) => s.slug === current);
            return `<details class="siddur-toc-group"${open ? " open" : ""}><summary class="siddur-toc-occasion">${heading}</summary><ul class="siddur-toc-list">${links}</ul></details>`;
        }
        return `<section class="siddur-toc-group"><h4 class="siddur-toc-occasion">${heading}</h4><ul class="siddur-toc-list">${links}</ul></section>`;
    }).join("");
    const rite = escapeHtml(isHebrew ? toc.rite.title.he : toc.rite.title.en);
    return `<nav class="siddur-toc${compact ? " siddur-toc-compact" : ""}" aria-label="${escapeHtml(isHebrew ? "סידור" : "Siddur")}">
        <a class="siddur-toc-rite" href="${sitePath(toc.rite.slug)}" data-siddur-path="${toc.rite.slug}">${rite}</a>
        ${groups}
    </nav>`;
}

// ─── Rite landing page ──────────────────────────────────────────────────────

function attributionMarkup(toc, { t, escapeHtml }) {
    const { he, en } = toc.source;
    return `<footer class="siddur-attribution">${escapeHtml(t(
        `Text: ${toc.source.index} from Sefaria. Hebrew: ${he.title} (${he.license}). English: ${en.title} (${en.license}), where a translation exists.`,
        `הטקסט: ${toc.source.index} מספריא. עברית: ${he.title} (${he.license}). אנגלית: ${en.title} (${en.license}), היכן שיש תרגום.`,
    ))}</footer>`;
}

export function landingMarkup(toc, { t, escapeHtml, isHebrew = false }) {
    const occasions = toc.occasions.map((occasion) => `
        <section class="siddur-landing-group" aria-labelledby="siddur-occ-${occasion.slug}">
            <h2 class="siddur-landing-occasion" id="siddur-occ-${occasion.slug}">${escapeHtml(isHebrew ? occasion.title.he : occasion.title.en)}</h2>
            <ul class="siddur-landing-grid">${occasion.services.map((service) => `
                <li><a class="siddur-card" href="${sitePath(toc.rite.slug, service.slug)}" data-siddur-path="${routeValue(toc.rite.slug, service.slug)}">
                    <span class="siddur-card-he" lang="he" dir="rtl">${escapeHtml(service.title.he)}</span>
                    <span class="siddur-card-en" lang="en" dir="ltr">${escapeHtml(service.title.en)}</span>
                    <span class="siddur-card-gloss">${escapeHtml(service.gloss || "")}</span>
                </a></li>`).join("")}
            </ul>
        </section>`).join("");
    return `<article class="siddur-page siddur-landing">
        <div class="siddur-today-slot" data-siddur-today></div>
        ${occasions}
        ${attributionMarkup(toc, { t, escapeHtml })}
    </article>`;
}

// ─── Service page ───────────────────────────────────────────────────────────

const LAYOUTS_WITH_ENGLISH = new Set(["bilingual", "bilingual-reverse", "interleaved", "english"]);

function lineMarkup(line, sectionSlug, { layout, applyHebrew, today, t, escapeHtml }) {
    const he = line.he ? safeLineHtml(applyHebrew(line.he)) : "";
    const en = line.en ? safeLineHtml(line.en) : "";
    let body;
    if (layout === "english") {
        body = en ? `<span class="siddur-en" lang="en" dir="ltr">${en}</span>` : `<span class="siddur-he" lang="he" dir="rtl">${he}</span>`;
    } else {
        body = he ? `<span class="siddur-he" lang="he" dir="rtl">${he}</span>` : "";
        if (en && LAYOUTS_WITH_ENGLISH.has(layout)) body += `<span class="siddur-en" lang="en" dir="ltr">${en}</span>`;
        if (!body) body = `<span class="siddur-en" lang="en" dir="ltr">${en}</span>`;
    }
    const when = Array.isArray(line.when) ? line.when : [];
    const isToday = when.some((tag) => today.has(tag));
    const attrs = `id="siddur-${sectionSlug}-${line.n}" data-n="${line.n}"${when.length ? ` data-when="${when.join(" ")}"` : ""}${isToday ? " data-today" : ""}`;
    const badge = isToday ? `<span class="siddur-today-badge">${escapeHtml(t("Today", "היום"))}</span>` : "";
    const label = line.label ? `<span class="siddur-label" lang="he" dir="rtl">${escapeHtml(line.label)}</span>` : "";
    switch (line.t) {
        case "heading":
            return `<h3 class="siddur-line siddur-heading" ${attrs}>${body}</h3>`;
        case "instruction":
            return `<p class="siddur-line siddur-instruction" ${attrs}>${badge}${body}</p>`;
        case "conditional":
            return `<div class="siddur-line siddur-conditional" ${attrs}>${badge}${label}<p>${body}</p></div>`;
        default:
            return `<p class="siddur-line siddur-prayer" ${attrs}>${body}</p>`;
    }
}

export function serviceMarkup(toc, where, payload, {
    layout = "hebrew", applyHebrew = (s) => s, today = new Set(), t, escapeHtml, isHebrew = false,
}) {
    const { service, occasion, index, services } = where;
    const rite = toc.rite.slug;
    const multi = service.sections.length > 1;
    const sectionsBySlug = new Map(payload.sections.map((s) => [s.slug, s]));
    const showEnglish = layout !== "hebrew";

    const picker = multi ? `
        <nav class="siddur-sections" aria-label="${escapeHtml(t("Sections", "חלקי התפילה"))}">
            <ol class="siddur-sections-list">${service.sections.map((sec, i) => `
                <li><a class="siddur-section-chip" href="${sitePath(rite, service.slug, sec.slug)}" data-siddur-section="${sec.slug}"${i === 0 ? ' aria-current="location"' : ""}>${escapeHtml(titleFor(sec, isHebrew))}</a></li>`).join("")}
            </ol>
            <p class="siddur-progress" aria-live="off"><span data-siddur-progress>1</span> / ${service.sections.length}</p>
        </nav>` : "";

    const sections = service.sections.map((sec) => {
        const data = sectionsBySlug.get(sec.slug) || { lines: [] };
        const noEnglish = showEnglish && !sec.english
            ? `<p class="siddur-note">${escapeHtml(t("No English translation for this section yet.", "אין עדיין תרגום אנגלי לחלק זה."))}</p>`
            : "";
        const lines = data.lines.map((line) => lineMarkup(line, sec.slug, { layout, applyHebrew, today, t, escapeHtml })).join("");
        const title = multi ? `<h2 class="siddur-section-title" id="siddur-sec-${sec.slug}-title">
                <span lang="he" dir="rtl">${escapeHtml(sec.title.he)}</span>
                <span class="siddur-section-alt" lang="en" dir="ltr">${escapeHtml(sec.title.en)}</span>
            </h2>` : "";
        return `<section class="siddur-section" id="siddur-sec-${sec.slug}" data-siddur-section-body="${sec.slug}"${multi ? ` aria-labelledby="siddur-sec-${sec.slug}-title"` : ""}>
            ${title}${noEnglish}<div class="siddur-lines">${lines}</div>
        </section>`;
    }).join("");

    const neighbor = (offset, rel, label) => {
        const other = services[index + offset];
        if (!other) return "";
        return `<a class="siddur-next-link" rel="${rel}" href="${sitePath(rite, other.entry.slug)}" data-siddur-path="${routeValue(rite, other.entry.slug)}">
            <span class="siddur-next-dir">${escapeHtml(label)}</span>
            <span class="siddur-next-name">${escapeHtml(titleFor(other.entry, isHebrew))}</span>
        </a>`;
    };

    return `<article class="siddur-page siddur-service" data-siddur-service="${service.slug}">
        <header class="siddur-service-head">
            <nav class="siddur-breadcrumb" aria-label="${escapeHtml(t("Breadcrumb", "מיקום"))}">
                <a href="${sitePath(rite)}" data-siddur-path="${rite}">${escapeHtml(t("Siddur", "סידור"))}</a>
                <span aria-hidden="true">›</span>
                <span>${escapeHtml(isHebrew ? occasion.title.he : occasion.title.en)}</span>
            </nav>
            <p class="siddur-service-gloss">${escapeHtml(isHebrew ? service.title.en : `${service.title.he} · ${service.gloss || ""}`)}</p>
            <div class="siddur-service-actions">
                <button type="button" class="siddur-chip" data-siddur-action="wake" aria-pressed="false" hidden>${escapeHtml(t("Keep screen on", "השאר מסך דלוק"))}</button>
                <a class="siddur-chip siddur-continue" data-siddur-continue hidden href="#"></a>
            </div>
        </header>
        <div class="siddur-today-slot" data-siddur-today></div>
        ${picker}
        ${sections}
        <nav class="siddur-next" aria-label="${escapeHtml(t("Other services", "תפילות נוספות"))}">
            ${neighbor(-1, "prev", t("Previous", "הקודם"))}${neighbor(1, "next", t("Next", "הבא"))}
        </nav>
        ${attributionMarkup(toc, { t, escapeHtml })}
    </article>`;
}

// ─── Behavior ───────────────────────────────────────────────────────────────

// Keeps the section picker (aria-current + progress) on the section being
// read and calls onSection(slug) when it changes. The section counted as
// being read is the last one whose top has passed a line a third of the
// way down the viewport. Returns a stop() function.
export function trackSections(root, { onSection = () => {}, win = globalThis.window } = {}) {
    const bodies = Array.from(root.querySelectorAll("[data-siddur-section-body]"));
    const chips = new Map(Array.from(root.querySelectorAll("[data-siddur-section]")).map((chip) => [chip.dataset.siddurSection, chip]));
    const progress = root.querySelector("[data-siddur-progress]");
    if (bodies.length < 2 || !win) return () => {};
    let current = null;
    let frame = 0;

    const setCurrent = (slug, { quiet = false } = {}) => {
        if (slug === current) return;
        current = slug;
        chips.forEach((chip, key) => {
            if (key === slug) {
                chip.setAttribute("aria-current", "location");
                chip.scrollIntoView?.({ block: "nearest", inline: "nearest" });
            } else {
                chip.removeAttribute("aria-current");
            }
        });
        if (progress) progress.textContent = String(bodies.findIndex((b) => b.dataset.siddurSectionBody === slug) + 1);
        if (!quiet) onSection(slug);
    };

    // The first measure only syncs the picker: opening a page is not the
    // reader moving to a section.
    const measure = (quiet = false) => {
        frame = 0;
        const line = (win.innerHeight || 0) / 3;
        let slug = bodies[0].dataset.siddurSectionBody;
        for (const body of bodies) {
            if (body.getBoundingClientRect().top <= line) slug = body.dataset.siddurSectionBody;
            else break;
        }
        setCurrent(slug, { quiet });
    };
    const onScroll = () => {
        if (!frame) frame = win.requestAnimationFrame(() => measure(false));
    };
    win.addEventListener("scroll", onScroll, { passive: true, capture: true });
    measure(true);
    return () => {
        win.removeEventListener("scroll", onScroll, { capture: true });
        if (frame) win.cancelAnimationFrame(frame);
    };
}

export function scrollToSection(root, slug, { reduceMotion = false } = {}) {
    if (!/^[a-z0-9-]+$/.test(String(slug || ""))) return false;
    const target = root.querySelector(`[data-siddur-section-body="${slug}"]`);
    if (!target) return false;
    target.scrollIntoView({ block: "start", behavior: reduceMotion ? "auto" : "smooth" });
    return true;
}

// Where each service was left: { "<rite>/<service>": "<section>" }.
const POSITION_KEY = "shelah.siddur.position";

export function readPosition(key, storage = globalThis.localStorage) {
    try {
        const all = JSON.parse(storage?.getItem(POSITION_KEY) || "{}");
        return typeof all?.[key] === "string" ? all[key] : null;
    } catch (_) {
        return null;
    }
}

export function writePosition(key, section, storage = globalThis.localStorage) {
    try {
        const all = JSON.parse(storage?.getItem(POSITION_KEY) || "{}") || {};
        all[key] = section;
        storage?.setItem(POSITION_KEY, JSON.stringify(all));
    } catch (_) {
        // Storage off: nothing to resume next time.
    }
}

// Screen Wake Lock for praying from the screen (research brief §5). The
// browser drops the lock whenever the page is hidden, so it is re-taken on
// return while the user still wants it. A refused request (battery saver)
// just leaves the switch off.
// Asks the service worker to keep the whole siddur (table of contents and
// every service) for offline use -- a reader who opened it will want it with
// no signal. In the installed app, also asks for storage the browser won't
// evict: Chrome and Safari grant that there without a prompt, where
// Firefox's tab would stop the reader with a permission question.
export function keepOffline(toc, { nav = globalThis.navigator, win = globalThis.window } = {}) {
    const controller = nav?.serviceWorker?.controller;
    if (controller && toc?.rite?.slug && toc?.version) {
        controller.postMessage({
            type: "PRECACHE_SIDDUR",
            rite: toc.rite.slug,
            version: toc.version,
            services: (toc.occasions || []).flatMap((occasion) => occasion.services.map((service) => service.slug)),
        });
    }
    const installed = win?.matchMedia?.("(display-mode: standalone)")?.matches || nav?.standalone === true;
    if (installed && nav?.storage?.persist) {
        Promise.resolve().then(() => nav.storage.persist()).catch(() => {});
    }
}

export function createWakeLock({ nav = globalThis.navigator, doc = globalThis.document } = {}) {
    const supported = Boolean(nav?.wakeLock?.request);
    let wanted = false;
    let sentinel = null;

    async function acquire() {
        if (!supported || !wanted || doc?.visibilityState === "hidden") return false;
        try {
            sentinel = await nav.wakeLock.request("screen");
            sentinel.addEventListener?.("release", () => { sentinel = null; });
            return true;
        } catch (_) {
            wanted = false;
            sentinel = null;
            return false;
        }
    }
    const onVisible = () => {
        if (doc.visibilityState === "visible" && wanted && !sentinel) void acquire();
    };
    doc?.addEventListener?.("visibilitychange", onVisible);

    return {
        supported,
        get active() { return wanted; },
        async enable() {
            wanted = true;
            return acquire();
        },
        async disable() {
            wanted = false;
            const held = sentinel;
            sentinel = null;
            try { await held?.release(); } catch (_) { /* already released */ }
        },
        destroy() {
            doc?.removeEventListener?.("visibilitychange", onVisible);
        },
    };
}
