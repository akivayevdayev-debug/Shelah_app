// The card a numbered source chip opens (static/js/citation-markers.js makes
// the chips): which source it is, the model's one-line note, the first lines
// of the text, and two ways on -- open it in the reader, or show its row in
// the sources list under the answer. Hovering a chip with a mouse opens it
// after a beat; clicking, tapping or pressing Enter opens it and keeps it.
//
// The card is built from the source's row in the sources list (its reference,
// note and whether the reader can open it), so the chip and the list can never
// disagree. Dependencies come in as options so this module owns only the
// card's behaviour.

import { escapeAttr } from "./citation-markers.js";

const HOVER_OPEN_MS = 140;
const HOVER_CLOSE_MS = 220;
const GAP = 8;
const EDGE = 8;

// options:
//   tr(en, he)           -> the string for the interface language
//   lang()               -> "en" | "he"
//   fetchPayload(ref)    -> Promise<payload | null>: the passage (shared cache)
//   previewHtml(lines, { lang, max }) -> the text excerpt's markup
//   openRef(ref)         -> open the source in the reader
//   reducedMotion()      -> true when motion should be skipped
//   bounds()             -> { top, bottom }: the part of the page the chips
//                           scroll within (sync closes the card once its chip
//                           leaves it)
export function createCitationPopover({
    doc = document,
    win = window,
    tr,
    lang,
    fetchPayload,
    previewHtml,
    openRef,
    reducedMotion = () => false,
    bounds = () => ({ top: 0, bottom: win.innerHeight }),
}) {
    let el = null;
    let anchor = null;
    let row = null;
    let hoverOpened = false;
    let hoverTimer = null;
    let leaveTimer = null;
    let token = 0;

    function ensure() {
        if (el) return el;
        el = doc.createElement("div");
        el.className = "conv-citepop hidden";
        el.setAttribute("role", "dialog");
        el.tabIndex = -1;
        el.addEventListener("click", onPopClick);
        el.addEventListener("pointerenter", () => clearTimeout(leaveTimer));
        el.addEventListener("pointerleave", (event) => {
            if (event.pointerType === "mouse" && hoverOpened) scheduleClose();
        });
        el.addEventListener("focusout", (event) => {
            if (el.classList.contains("hidden")) return;
            const next = event.relatedTarget;
            if (next && (el.contains(next) || next === anchor)) return;
            // Focus left without going anywhere in particular (a click on the
            // page): the outside-click handler closes it; a Tab out does here.
            if (next) close();
        });
        doc.body.appendChild(el);
        return el;
    }

    const isOpen = () => Boolean(el) && !el.classList.contains("hidden");

    function textState(row) {
        // A source the reader cannot open has no text to show.
        return row?.dataset.readable === "false" ? "none" : "loading";
    }

    function contentHtml(chip) {
        const number = chip.dataset.mark;
        const ref = row.dataset.ref || "";
        const refText = row.querySelector(".conv-cite__ref")?.textContent?.trim() || ref;
        const note = row.querySelector(".conv-cite__excerpt")?.textContent?.trim() || "";
        const readable = row.dataset.readable !== "false";
        const open = readable
            ? `<button type="button" class="conv-citepop__action" data-pop-action="open">${escapeAttr(tr("Open in reader", "פתח בקורא"))}</button>`
            : "";
        const jump = `<button type="button" class="conv-citepop__action conv-citepop__action--quiet" data-pop-action="jump">${escapeAttr(tr("Show in sources", "הצג במקורות"))}</button>`;
        return `<div class="conv-citepop__head"><span class="conv-citepop__num" aria-hidden="true">${escapeAttr(number)}</span>`
            + `<span class="conv-citepop__ref" dir="auto">${escapeAttr(refText)}</span></div>`
            + (note ? `<p class="conv-citepop__note" dir="auto">${escapeAttr(note)}</p>` : "")
            + `<div class="conv-citepop__text" data-state="${textState(row)}" aria-live="polite"></div>`
            + `<div class="conv-citepop__actions">${open}${jump}</div>`;
    }

    function place() {
        if (!isOpen() || !anchor?.isConnected) return;
        const rect = anchor.getBoundingClientRect();
        const width = el.offsetWidth;
        const height = el.offsetHeight;
        const viewportW = win.innerWidth;
        const viewportH = win.innerHeight;
        let top = rect.bottom + GAP;
        const fitsBelow = top + height <= viewportH - EDGE;
        if (!fitsBelow && rect.top - GAP - height >= EDGE) top = rect.top - GAP - height;
        top = Math.max(EDGE, Math.min(top, viewportH - height - EDGE));
        let left = rect.left + rect.width / 2 - width / 2;
        left = Math.max(EDGE, Math.min(left, viewportW - width - EDGE));
        el.style.top = `${Math.round(top)}px`;
        el.style.left = `${Math.round(left)}px`;
        el.style.setProperty("--citepop-origin", `${Math.round(rect.left + rect.width / 2 - left)}px ${top < rect.top ? "100%" : "0"}`);
    }

    // The text moved (a scroll, or a row above changing height): the card
    // follows its chip, and closes only when the chip has scrolled out of view.
    function sync() {
        if (!isOpen()) return;
        if (!anchor?.isConnected) {
            close();
            return;
        }
        const rect = anchor.getBoundingClientRect();
        const area = bounds();
        if (rect.bottom < area.top || rect.top > area.bottom) close();
        else place();
    }

    async function fillText(forToken) {
        const body = el.querySelector(".conv-citepop__text");
        const ref = row?.dataset.ref;
        if (!body || body.dataset.state !== "loading" || !ref) return;
        body.innerHTML = '<div class="ai-src-skeleton-line ai-src-skeleton-line--wide"></div><div class="ai-src-skeleton-line ai-src-skeleton-line--medium"></div>';
        const payload = await fetchPayload(ref);
        if (forToken !== token || !isOpen()) return;
        const html = payload ? previewHtml(payload.lines, { lang: lang(), max: 2 }) : "";
        body.innerHTML = html;
        body.dataset.state = html ? "done" : "none";
        place();
    }

    function show(chip, { focus }) {
        const target = doc.getElementById(chip.dataset.markTarget || "");
        if (!target) return;
        close();
        ensure();
        anchor = chip;
        row = target;
        token += 1;
        el.setAttribute("aria-label", tr(`Source ${chip.dataset.mark}`, `מקור ${chip.dataset.mark}`));
        el.innerHTML = contentHtml(chip);
        el.classList.remove("hidden");
        place();
        chip.setAttribute("aria-expanded", "true");
        row.classList.add("conv-cite--active");
        if (!reducedMotion()) {
            el.classList.remove("conv-citepop--in");
            void el.offsetWidth; // restart the entrance if it is reopened at once
            el.classList.add("conv-citepop--in");
        }
        if (focus) el.focus({ preventScroll: true });
        void fillText(token);
    }

    function close({ restoreFocus = false } = {}) {
        clearTimeout(hoverTimer);
        clearTimeout(leaveTimer);
        hoverOpened = false;
        token += 1;
        if (el && !el.classList.contains("hidden")) {
            el.classList.add("hidden");
            el.classList.remove("conv-citepop--in");
        }
        const chip = anchor;
        if (chip) chip.setAttribute("aria-expanded", "false");
        row?.classList.remove("conv-cite--active");
        anchor = null;
        row = null;
        if (restoreFocus && chip?.isConnected) chip.focus({ preventScroll: true });
    }

    function scheduleClose() {
        clearTimeout(leaveTimer);
        leaveTimer = setTimeout(() => {
            if (hoverOpened && !el?.matches(":hover")) close();
        }, HOVER_CLOSE_MS);
    }

    // Click, tap or Enter: open and keep it open (and take focus, so Escape
    // and Tab work); the same chip again closes it.
    function toggle(chip) {
        if (isOpen() && anchor === chip) {
            close({ restoreFocus: true });
            return;
        }
        show(chip, { focus: true });
    }

    // A mouse resting on a chip: preview it. Never replaces a card the reader
    // opened on purpose.
    function hoverIn(chip) {
        clearTimeout(leaveTimer);
        if (isOpen() && anchor === chip) return;
        if (isOpen() && !hoverOpened) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => {
            if (isOpen() && !hoverOpened) return;
            show(chip, { focus: false });
            hoverOpened = true;
        }, HOVER_OPEN_MS);
    }

    function hoverOut() {
        clearTimeout(hoverTimer);
        if (isOpen() && hoverOpened) scheduleClose();
    }

    function jumpToRow(target) {
        target.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
        target.focus({ preventScroll: true });
        if (reducedMotion()) return;
        target.classList.remove("conv-cite--flash");
        void target.offsetWidth;
        target.classList.add("conv-cite--flash");
        win.setTimeout(() => target.classList.remove("conv-cite--flash"), 1400);
    }

    function onPopClick(event) {
        const button = event.target.closest?.("[data-pop-action]");
        if (!button || !row) return;
        const ref = row.dataset.ref;
        const target = row;
        close();
        if (button.dataset.popAction === "open") openRef(ref);
        else jumpToRow(target);
    }

    return {
        toggle,
        hoverIn,
        hoverOut,
        close,
        place,
        sync,
        isOpen,
        anchor: () => anchor,
        contains: (node) => Boolean(el) && el.contains(node),
    };
}
