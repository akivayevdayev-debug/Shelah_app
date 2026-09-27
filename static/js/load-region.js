/**
 * One way for every async region of the page to load, fail and recover
 * (audit L-8).
 *
 *  - Busy: aria-busy="true" on the region, and a separate role="status" line
 *    beside it reading "Loading…" / "Loaded" (aria-busy only mutes a region;
 *    it announces nothing itself).
 *  - Failed: a role="alert" message with a focusable Retry button, or none
 *    when retrying can't help (a text that doesn't exist), plus any actions
 *    of the caller's ("Back to prayers", "Search for …").
 *  - Recovery: automatic retries after 2 s and 6 s (the reader's long-standing
 *    delays), then manual only; one more when the connection comes back.
 *    Data that ages can revalidate when the tab becomes visible again.
 *  - One AbortController per region: a new load of the region, or
 *    abortRegion(), aborts the one before and clears every timer and
 *    listener it left (ENGINEERING_RULES loading rule 5).
 *
 * Loaded as a plain classic <script> before index.html's inline script, which
 * reads window.ShelahLoadRegion; node tests require() it.
 */
(function (root) {
    'use strict';

    const RETRY_DELAYS_MS = Object.freeze([2000, 6000]);

    // Per region: the active load's AbortController, and its pending
    // retry timers and listeners.
    const regions = new WeakMap();
    const statusLines = new WeakMap();

    function isHebrew(el) {
        return el?.ownerDocument?.documentElement?.getAttribute('lang') === 'he';
    }

    function tr(el, en, he) {
        return isHebrew(el) ? he : en;
    }

    function regionState(el) {
        let state = regions.get(el);
        if (!state) {
            state = { controller: null, timers: new Set(), listeners: [] };
            regions.set(el, state);
        }
        return state;
    }

    function clearWaits(state) {
        state.timers.forEach((timer) => clearTimeout(timer));
        state.timers.clear();
        state.listeners.forEach(([target, type, handler]) => target.removeEventListener(type, handler));
        state.listeners = [];
    }

    function listen(state, target, type, handler) {
        if (!target?.addEventListener) return;
        const once = (event) => {
            target.removeEventListener(type, once);
            state.listeners = state.listeners.filter((entry) => entry[2] !== once);
            handler(event);
        };
        target.addEventListener(type, once);
        state.listeners.push([target, type, once]);
    }

    // Stops whatever the region is doing: its fetch, its retry timers, its
    // online/visibility listeners.
    function abortRegion(el) {
        const state = regions.get(el);
        if (!state) return;
        state.controller?.abort();
        state.controller = null;
        clearWaits(state);
    }

    // A fresh AbortController for the region's next fetch; the one before is
    // aborted and its pending retries dropped.
    function beginRegionLoad(el) {
        abortRegion(el);
        const state = regionState(el);
        state.controller = new AbortController();
        return state.controller.signal;
    }

    function statusLine(el) {
        let status = statusLines.get(el);
        if (status && status.parentNode) return status;
        const doc = el.ownerDocument;
        if (!doc || !el.parentNode) return null;
        status = doc.createElement('p');
        status.className = 'sr-only';
        status.setAttribute('role', 'status');
        status.setAttribute('data-region-status', '');
        el.parentNode.insertBefore(status, el.nextSibling);
        statusLines.set(el, status);
        return status;
    }

    // `quiet`: the load was called off, so nothing is announced ("Loaded"
    // would be untrue) and the status line is emptied.
    function setRegionBusy(el, busy, { quiet = false } = {}) {
        if (!el) return;
        el.setAttribute('aria-busy', busy ? 'true' : 'false');
        const status = quiet ? statusLines.get(el) : statusLine(el);
        if (!status) return;
        if (quiet && !busy) status.textContent = '';
        else status.textContent = busy ? tr(el, 'Loading…', 'טוען…') : tr(el, 'Loaded', 'נטען');
    }

    function button(doc, className, label, onClick) {
        const el = doc.createElement('button');
        el.type = 'button';
        el.className = className;
        el.textContent = label;
        el.addEventListener('click', onClick);
        return el;
    }

    // Replaces the region's content with the error. `retry` null: no Retry
    // button (retrying can't help). `actions`: [{ label, onClick }].
    function renderRegionError(el, { message, retry = null, actions = [] } = {}) {
        const doc = el.ownerDocument;
        el.setAttribute('aria-busy', 'false');
        const status = statusLines.get(el);
        if (status) status.textContent = '';
        const box = doc.createElement('div');
        box.className = 'region-error';
        box.setAttribute('role', 'alert');
        const text = doc.createElement('p');
        text.className = 'region-error__message';
        text.textContent = String(message || tr(el, 'Something went wrong.', 'משהו השתבש.'));
        box.appendChild(text);
        const row = doc.createElement('div');
        row.className = 'region-error__actions';
        if (typeof retry === 'function') {
            row.appendChild(button(doc, 'region-error__retry', tr(el, 'Retry', 'נסה שוב'), () => retry()));
        }
        actions.forEach(({ label, onClick }) => {
            row.appendChild(button(doc, 'region-error__action', label, () => onClick()));
        });
        if (row.firstChild) box.appendChild(row);
        el.textContent = '';
        el.appendChild(box);
        return box;
    }

    // After a failure: `retry(attempt + 1)` after delays[attempt] (online
    // only), then manual; `retry(attempt)` once when the connection is back.
    // Any later load or abort of the region cancels both.
    function retryLater(el, retry, { attempt = 0, delays = RETRY_DELAYS_MS, onlineRetry = true } = {}) {
        const state = regionState(el);
        clearWaits(state);
        const win = root.window || root;
        if (attempt < delays.length) {
            const timer = setTimeout(() => {
                state.timers.delete(timer);
                if (win.navigator?.onLine === false) return;
                clearWaits(state);
                retry(attempt + 1);
            }, delays[attempt]);
            state.timers.add(timer);
        }
        if (onlineRetry) {
            listen(state, win, 'online', () => {
                clearWaits(state);
                retry(attempt);
            });
        }
    }

    function defaultMessage(el, error) {
        return error?.message || tr(el, "Couldn't load this. Check your connection.", 'לא ניתן היה לטעון. בדקו את החיבור.');
    }

    // The whole cycle for a region with a plain fetch -> render shape.
    //   skeleton(): HTML drawn at once (omitted: the content stays until the
    //     data replaces it). fetch(signal) -> data. render(data, {revalidated}).
    //   errorMessage(error), errorActions(error) -> [{label, onClick}],
    //   isRetryable(error): false for "doesn't exist" (default: error.retryable
    //     !== false).
    //   revalidateOnVisible: refetch quietly when the tab comes back into view;
    //     a failure then keeps what's on screen.
    // Returns { ready, reload, abort }; `ready` settles when this attempt has
    // rendered its data or its error.
    function loadRegion(el, {
        skeleton = null,
        fetch: load,
        render,
        errorMessage = (error) => defaultMessage(el, error),
        errorActions = () => [],
        isRetryable = (error) => error?.retryable !== false,
        retryDelays = RETRY_DELAYS_MS,
        onlineRetry = true,
        revalidateOnVisible = false,
    } = {}) {
        const doc = el.ownerDocument;

        const watchVisibility = () => {
            if (!revalidateOnVisible || !doc) return;
            listen(regionState(el), doc, 'visibilitychange', () => {
                if (doc.visibilityState === 'hidden') {
                    watchVisibility();
                    return;
                }
                void run(0, { quiet: true });
            });
        };

        async function run(attempt, { quiet = false } = {}) {
            const signal = beginRegionLoad(el);
            if (!quiet) {
                setRegionBusy(el, true);
                if (typeof skeleton === 'function') el.innerHTML = skeleton();
            }
            try {
                const data = await load(signal);
                if (signal.aborted) return;
                render(data, { revalidated: quiet });
                if (!quiet) setRegionBusy(el, false);
                watchVisibility();
            } catch (error) {
                if (signal.aborted) return;
                if (quiet) {
                    watchVisibility();
                    return;
                }
                const retryable = isRetryable(error);
                renderRegionError(el, {
                    message: errorMessage(error),
                    retry: retryable ? () => { void run(0); } : null,
                    actions: errorActions(error) || [],
                });
                if (retryable) {
                    retryLater(el, (next) => { void run(next); }, { attempt, delays: retryDelays, onlineRetry });
                }
            }
        }

        const ready = run(0);
        return {
            ready,
            reload: () => run(0),
            abort: () => abortRegion(el),
        };
    }

    const api = {
        RETRY_DELAYS_MS,
        loadRegion,
        setRegionBusy,
        renderRegionError,
        retryLater,
        beginRegionLoad,
        abortRegion,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    root.ShelahLoadRegion = api;
})(typeof window !== 'undefined' ? window : globalThis);
