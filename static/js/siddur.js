// The siddur view (Prayers redesign): loads the checked-in siddur through
// /api/siddur/v2 and renders it into the reader, with the day's guidance.
//
// The classic script owns the reader chrome (view swap, skeleton, title,
// history entry, errors -- index.html displaySiddur) and calls open() here
// for the page body; main.js exposes it as window.ShelahSiddur. Everything
// this module needs from the classic script comes in as `deps`
// (main.js buildSiddurDeps), the same boundary zmanim.js uses.

import * as reader from "./siddur-reader.js";
import * as day from "./siddur-day.js";

export const DEFAULT_RITE = "edot-hamizrach";
const DAY_WAIT_MS = 1200;

// A page the siddur doesn't have (a bad slug, an unknown service): the
// reader shows "Back to the siddur" instead of Retry. Recognised by name
// across the module boundary.
export class SiddurNotFound extends Error {
    constructor(message) {
        super(message);
        this.name = "SiddurNotFound";
    }
}

export function createSiddur({ fetchImpl = globalThis.fetch, deps, storage = globalThis.localStorage, win = globalThis.window, nav = globalThis.navigator, doc = globalThis.document } = {}) {
    const tocs = new Map();
    const services = new Map();
    const dayClient = day.createDayClient(fetchImpl);
    const wake = reader.createWakeLock({ nav, doc });
    let eveningOverride = null;
    let stopTracking = () => {};
    let shown = null; // { root, prepared, container }

    const t = (en, he) => deps.t(en, he);
    const isHebrew = () => Boolean(deps.isHebrewMode());

    async function fetchJson(url) {
        const response = await fetchImpl(url);
        if (response.status === 404) throw new SiddurNotFound(url);
        if (!response.ok) throw new Error(`${url} ${response.status}`);
        return response.json();
    }

    // Cached promises; a failed load is dropped so a retry fetches again.
    function cached(map, key, load) {
        if (!map.has(key)) {
            map.set(key, load().catch((error) => {
                map.delete(key);
                throw error;
            }));
        }
        return map.get(key);
    }

    function loadToc(rite = DEFAULT_RITE) {
        return cached(tocs, rite, () => fetchJson(`/api/siddur/v2/toc/${encodeURIComponent(rite)}`));
    }

    function loadService(toc, slug) {
        return cached(services, `${toc.rite.slug}/${slug}@${toc.version}`,
            () => fetchJson(`/api/siddur/v2/service/${toc.rite.slug}/${slug}?v=${toc.version}`));
    }

    function israel() {
        const saved = day.readIsrael(storage);
        return saved ?? day.guessIsrael(deps.getLocation?.());
    }

    function whichDay() {
        return day.hebrewDayFor(new Date(), deps.getSunset?.() || null, eveningOverride);
    }

    function badgeText() {
        return t("Today", "היום");
    }

    // Marks the rubrics that apply today; used for a late day answer and
    // when the user flips Israel / after-sunset.
    function markToday(root, tags) {
        root.querySelectorAll("[data-when]").forEach((line) => {
            const on = line.dataset.when.split(" ").some((tag) => tags.has(tag));
            const badge = line.querySelector(":scope > .siddur-today-badge");
            line.toggleAttribute("data-today", on);
            if (on && !badge) line.insertAdjacentHTML("afterbegin", `<span class="siddur-today-badge">${deps.escapeHtml(badgeText())}</span>`);
            if (!on && badge) badge.remove();
        });
    }

    function renderToday(root, dayData, { kind, il, afterSunset }) {
        const slot = root.querySelector("[data-siddur-today]");
        if (!slot) return;
        slot.innerHTML = dayData
            ? day.todayCardMarkup(dayData, { kind, il, afterSunset, t, escapeHtml: deps.escapeHtml, isHebrew: isHebrew() })
            : "";
    }

    async function refreshDay(root, kind) {
        const { date, afterSunset } = whichDay();
        const il = israel();
        let dayData = null;
        try {
            dayData = await dayClient.get(date, il);
        } catch (_) {
            dayData = null;
        }
        if (shown?.root !== root) return null;
        renderToday(root, dayData, { kind, il, afterSunset });
        markToday(root, dayData ? day.activeTags(dayData) : new Set());
        return dayData;
    }

    function wire(root, { toc, where, kind }) {
        root.addEventListener("click", (event) => {
            const action = event.target.closest("[data-siddur-action]");
            if (action) {
                const name = action.dataset.siddurAction;
                if (name === "toggle-israel") {
                    day.writeIsrael(!israel(), storage);
                    void refreshDay(root, kind);
                } else if (name === "toggle-evening") {
                    eveningOverride = !whichDay().afterSunset;
                    void refreshDay(root, kind);
                } else if (name === "wake") {
                    const turnOn = !wake.active;
                    void (turnOn ? wake.enable() : wake.disable()).then(() => {
                        action.setAttribute("aria-pressed", wake.active ? "true" : "false");
                    });
                }
                return;
            }
            const chip = event.target.closest("[data-siddur-section]");
            if (chip && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
                event.preventDefault();
                reader.scrollToSection(root, chip.dataset.siddurSection, { reduceMotion: deps.reduceMotion?.() });
            }
        });

        const wakeBtn = root.querySelector('[data-siddur-action="wake"]');
        if (wakeBtn && wake.supported) {
            wakeBtn.hidden = false;
            wakeBtn.setAttribute("aria-pressed", wake.active ? "true" : "false");
        }

        if (!where.service) return;
        const key = reader.routeValue(toc.rite.slug, where.service.slug);
        stopTracking();
        stopTracking = reader.trackSections(root, {
            win,
            onSection(slug) {
                // Hidden (another view is up) or gone: not a reading position.
                if (!root.isConnected || root.offsetParent === null) return;
                reader.writePosition(key, slug, storage);
                // Back at the top section, the URL is the service's own.
                const first = where.service.sections[0].slug;
                deps.replaceRoute?.(reader.routeValue(toc.rite.slug, where.service.slug, slug === first ? null : slug));
            },
        });
    }

    function showContinue(root, toc, where) {
        const link = root.querySelector("[data-siddur-continue]");
        if (!link || where.service.sections.length < 2) return;
        const saved = reader.readPosition(reader.routeValue(toc.rite.slug, where.service.slug), storage);
        const section = where.service.sections.find((s) => s.slug === saved);
        if (!section || section === where.service.sections[0]) return;
        link.hidden = false;
        link.href = reader.sitePath(toc.rite.slug, where.service.slug, section.slug);
        link.dataset.siddurSection = section.slug;
        link.textContent = t(`Continue: ${section.title.en}`, `להמשיך: ${section.title.he}`);
    }

    // Loads what the page `value` names ("<rite>[/<service>[/<section>]]")
    // needs. Rejects with SiddurNotFound for a page the siddur doesn't have.
    // Nothing is drawn yet, so a caller that has moved on can drop it.
    async function prepare(value) {
        const parsed = reader.parseValue(value);
        if (!parsed) throw new SiddurNotFound(String(value));
        const toc = await loadToc(parsed.rite);
        const where = reader.locate(toc, parsed);
        if (!where) throw new SiddurNotFound(String(value));
        const { date } = whichDay();
        const dayRequest = dayClient.get(date, israel()).catch(() => null);
        const payload = where.service ? await loadService(toc, where.service.slug) : null;
        // A short wait for the day's answer, so today's rubrics paint marked
        // instead of shifting in; a slow answer lands afterwards.
        const early = await Promise.race([dayRequest, new Promise((resolve) => setTimeout(() => resolve(undefined), DAY_WAIT_MS))]);
        return { value, parsed, toc, where, payload, early: early || null };
    }

    // Draws a prepared page into `container`, synchronously. Returns
    // { label } for the reader chrome.
    function mount(container, prepared, { keepSection = null } = {}) {
        const { parsed, toc, where, payload, early } = prepared;
        const kind = where.service ? day.serviceKind(where.service.slug) : null;
        stopTracking();
        const escapeHtml = deps.escapeHtml;
        const prefs = deps.getPrefs?.() || {};
        container.innerHTML = where.service
            ? reader.serviceMarkup(toc, where, payload, {
                layout: prefs.readerLayout || "hebrew",
                applyHebrew: (html) => deps.applyHebrewDisplaySettings?.(html) ?? html,
                today: early ? day.activeTags(early) : new Set(),
                t, escapeHtml, isHebrew: isHebrew(),
            })
            : reader.landingMarkup(toc, { t, escapeHtml, isHebrew: isHebrew() });
        const root = container.firstElementChild;
        shown = { root, prepared, container };
        wire(root, { toc, where, kind });
        const section = keepSection || parsed.section;
        const jump = () => {
            if (where.service && section) reader.scrollToSection(root, section, { reduceMotion: true });
        };
        // The Today card sits above the text: drawn in this same pass when
        // the day's answer is already here, so the jump to a section below
        // it lands where it stays. A late answer re-applies the jump once
        // the card is in.
        if (early) {
            renderToday(root, early, { kind, il: israel(), afterSunset: whichDay().afterSunset });
        } else {
            void refreshDay(root, kind).then(jump);
        }
        jump();
        if (where.service && !section) showContinue(root, toc, where);
        return { label: reader.viewLabel(toc, where, isHebrew()), section };
    }

    async function open(value, { container }) {
        return mount(container, await prepare(value));
    }

    // Redraws the page on screen after a display change (layout, vowels,
    // cantillation, language), staying on the section being read.
    function rerender() {
        if (!shown?.root?.isConnected) return null;
        const current = shown.root.querySelector('[data-siddur-section][aria-current="location"]')?.dataset.siddurSection || null;
        return mount(shown.container, shown.prepared, { keepSection: current });
    }

    // Back/Forward to another section of the service on screen.
    function scrollTo(value) {
        const parsed = reader.parseValue(value);
        if (!shown || !parsed?.section) return false;
        return reader.scrollToSection(shown.root, parsed.section, { reduceMotion: deps.reduceMotion?.() });
    }

    function isShowing(value) {
        const parsed = reader.parseValue(value);
        if (!parsed || !shown?.root?.isConnected) return false;
        const { toc, where } = shown.prepared;
        return toc.rite.slug === parsed.rite && (where.service?.slug || null) === (parsed.service || null);
    }

    function close() {
        stopTracking();
        stopTracking = () => {};
        shown = null;
        void wake.disable();
    }

    // The table of contents for a menu mount point (Texts menu, sidebar).
    async function renderToc(mount, { compact = false, current = null } = {}) {
        if (!mount) return;
        mount.setAttribute("aria-busy", "true");
        try {
            const toc = await loadToc();
            mount.innerHTML = reader.tocMarkup(toc, { escapeHtml: deps.escapeHtml, isHebrew: isHebrew(), compact, current });
        } finally {
            mount.setAttribute("aria-busy", "false");
        }
    }

    return { prepare, mount, open, rerender, scrollTo, isShowing, close, renderToc, loadToc, wake };
}
