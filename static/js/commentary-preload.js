// Preloads the commentary sidebar for the verse the reader is on.
//
// The sidebar used to start fetching only after a text selection: the
// commentary list (a ~700 KB payload for Genesis 1:1), then the chosen
// commentator's text, then its translation, one after the other. This module
// finds the verse in view -- the one at a reading line a little below the top
// of the scroll area, or the one the reader just clicked -- once scrolling
// settles, and asks the page's loaders for that verse's commentary links and
// the text of its main commentators. The loaders fill the sidebar's caches, so
// selecting a passage or opening the sidebar paints from memory. The next two
// verses are requested after the reader lingers, so reading on stays ahead too
// (steady state costs two requests per verse advanced, whatever the look-ahead:
// every verse but the newest is already cached).
//
// Deliberately not part of the page script: the "which verse is this" rule and
// the request budget are the parts worth testing (tests_js/commentary_preload
// .test.js), and the page only supplies how to name a verse and how to load it.

export const READING_LINE_RATIO = 0.3;
export const SETTLE_MS = 220;
export const NEIGHBOR_MS = 1500;
export const BACKOFF_MS = 30000;
export const LOOKAHEAD = 2;

// Index of the row the reader is on, given `count` rows stacked top to bottom
// and `rectAt(i)` returning row i's bounding rect: the row holding `lineY`,
// else the last row above it (the line is in the gap between two rows or past
// the end), else the first row (the line is above all of them). Binary search,
// so a chapter of hundreds of verses costs a handful of layout reads.
export function pickReadingIndex(count, rectAt, lineY) {
    if (!count) return -1;
    let low = 0;
    let high = count - 1;
    let found = -1;
    while (low <= high) {
        const mid = (low + high) >> 1;
        if (rectAt(mid).top <= lineY) {
            found = mid;
            low = mid + 1;
        } else {
            high = mid - 1;
        }
    }
    return found === -1 ? 0 : found;
}

// Where the reading line sits, in viewport coordinates.
export function readingLineY(scrollEl, doc, win, ratio = READING_LINE_RATIO) {
    const isPage = !scrollEl
        || scrollEl === doc.documentElement
        || scrollEl === doc.body
        || scrollEl === doc.scrollingElement;
    const top = isPage ? 0 : scrollEl.getBoundingClientRect().top;
    const height = isPage ? win.innerHeight : scrollEl.clientHeight;
    return top + height * ratio;
}

// options:
//   resolveRef(row)  -> the passage ref for a verse row ('' if none)
//   loadLinks(ref)   -> Promise: fetch + cache that verse's commentary links
//   loadTexts(ref)   -> Promise: fetch + cache its main commentators' text
//   isEligible()     -> whether the open text has commentary at all
//   onReadingRef(ref, row) -> called when the verse in view changes
//   getScrollEl()    -> the element the reader scrolls
// A loader rejecting with `status === 429` pauses the speculative requests
// (the next verse, and text for this one) for BACKOFF_MS.
export function createCommentaryPreloader(options) {
    const {
        doc = document,
        win = window,
        rowSelector = '#readerSources .segment-row[data-segment]',
        getScrollEl = () => doc.scrollingElement,
        resolveRef,
        isEligible = () => true,
        loadLinks,
        loadTexts,
        onReadingRef = () => { },
        settleMs = SETTLE_MS,
        neighborMs = NEIGHBOR_MS,
        now = () => Date.now(),
        setTimer = (fn, ms) => setTimeout(fn, ms),
        clearTimer = (id) => clearTimeout(id),
        saveData = () => Boolean(win.navigator?.connection?.saveData),
    } = options;

    let settleTimer = null;
    let neighborTimer = null;
    let currentRef = '';
    let currentRow = null;
    let focusedRow = null;
    let backoffUntil = 0;
    let destroyed = false;

    const backedOff = () => now() < backoffUntil;

    async function guarded(load, ref) {
        try {
            await load(ref);
            return true;
        } catch (err) {
            if (err && err.status === 429) backoffUntil = now() + BACKOFF_MS;
            return false;
        }
    }

    const staleFor = (ref) => destroyed || currentRef !== ref || backedOff();

    // Links first -- they are what the sidebar lists and are small -- then the
    // text of the main commentators, then (after a pause) the next verses:
    // links and text for the first, links for the second. Every step first
    // checks the reader is still on `ref`.
    async function run(ref, nextRows) {
        clearTimer(neighborTimer);
        await guarded(loadLinks, ref);
        if (staleFor(ref) || saveData()) return;
        await guarded(loadTexts, ref);
        if (staleFor(ref) || saveData()) return;

        const nextRefs = nextRows
            .map((row) => resolveRef(row))
            .filter((nextRef, i, all) => nextRef && nextRef !== ref && all.indexOf(nextRef) === i);
        if (!nextRefs.length) return;
        neighborTimer = setTimer(async () => {
            for (const [i, nextRef] of nextRefs.entries()) {
                if (staleFor(ref)) return;
                await guarded(loadLinks, nextRef);
                if (i === 0 && !staleFor(ref)) await guarded(loadTexts, nextRef);
            }
        }, neighborMs);
    }

    function evaluate() {
        settleTimer = null;
        if (destroyed || doc.visibilityState === 'hidden' || !isEligible()) return;
        const rows = Array.from(doc.querySelectorAll(rowSelector));
        if (!rows.length) return;

        let index = focusedRow ? rows.indexOf(focusedRow) : -1;
        if (index === -1) {
            focusedRow = null;
            index = pickReadingIndex(
                rows.length,
                (i) => rows[i].getBoundingClientRect(),
                readingLineY(getScrollEl(), doc, win),
            );
        }
        const row = rows[index];
        const ref = resolveRef(row);
        if (!ref) return;

        currentRow = row;
        if (ref === currentRef) return;
        currentRef = ref;
        onReadingRef(ref, row);
        void run(ref, rows.slice(index + 1, index + 1 + LOOKAHEAD));
    }

    // Re-evaluate once scrolling (or whatever else moved the text) settles, so
    // a fast scroll through a chapter requests only where it stops.
    function refresh({ immediate = false } = {}) {
        if (destroyed) return;
        clearTimer(settleTimer);
        settleTimer = null;
        if (immediate) evaluate();
        else settleTimer = setTimer(evaluate, settleMs);
    }

    // The reader clicked or selected inside a verse: that is the verse they
    // are on, whatever the scroll position says, until the text scrolls.
    function focusRow(row) {
        if (destroyed || !row) return;
        focusedRow = row;
        refresh({ immediate: true });
    }

    function onScroll() {
        focusedRow = null;
        refresh();
    }

    function onVisibility() {
        if (doc.visibilityState === 'visible') refresh();
    }

    // Capture phase: `scroll` doesn't bubble, and which element scrolls (the
    // page or the main column) depends on the viewport.
    doc.addEventListener('scroll', onScroll, { capture: true, passive: true });
    win.addEventListener('resize', onScroll, { passive: true });
    doc.addEventListener('visibilitychange', onVisibility);

    return {
        refresh,
        focusRow,
        // A new text replaced the old one: forget the verse, so it is requested
        // afresh even if the new text happens to start at the same ref.
        reset() {
            clearTimer(settleTimer);
            clearTimer(neighborTimer);
            settleTimer = null;
            neighborTimer = null;
            currentRef = '';
            currentRow = null;
            focusedRow = null;
        },
        currentRef: () => currentRef,
        currentRow: () => currentRow,
        destroy() {
            destroyed = true;
            clearTimer(settleTimer);
            clearTimer(neighborTimer);
            doc.removeEventListener('scroll', onScroll, { capture: true });
            win.removeEventListener('resize', onScroll);
            doc.removeEventListener('visibilitychange', onVisibility);
        },
    };
}
