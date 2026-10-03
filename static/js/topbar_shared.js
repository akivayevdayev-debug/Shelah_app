/**
 * Small helpers shared between templates/index.html's topbar and
 * templates/components/legal_scripts.html (used by the legal/about/help/
 * glossary pages). Each page's toggleLanguage() / positionFloatingMenu()
 * still owns everything page-specific -- index.html's versions touch dozens
 * of app-only elements and use a CSS class (not an inline style) to cancel
 * the floating menus' centring transform so their open/close animation can
 * still drive `transform` -- these two functions only cover the parts that
 * were verbatim duplicates (Sonar web:S4144) with no page-specific behavior
 * at all.
 *
 * Loaded as a plain classic <script> (not type="module") so its exports
 * become real globals, callable by name the same way
 * the functions themselves were before this was split out.
 */
(function (root) {
    'use strict';

    // The shared prefix of both toggleLanguage() implementations: normalize
    // the language, persist it, and swap every [data-en]/[data-he] element's
    // visible text. Returns the normalized language so the caller can use it
    // for whatever page-specific work (RTL attributes, lang-btn styling, ...)
    // comes next. `persist: false` shows the language without saving it (a
    // link's ?lang= is that page load's language, not the visitor's choice).
    function applyLanguagePreference(lang, options, languageManualKey) {
        const normalized = (lang === 'en' || lang === 'he') ? lang : 'en';
        const autoChange = Boolean(options && options.auto);
        const persist = !(options && options.persist === false);

        if (persist) {
            localStorage.setItem('preferredLanguage', normalized);
            if (!autoChange) {
                localStorage.setItem(languageManualKey, '1');
            }
        }

        swapLanguageText(normalized, document);

        return normalized;
    }

    // Swap every [data-en]/[data-he] element under `scope` to `lang`'s text,
    // leaving any element with no text for that language alone.
    function swapLanguageText(lang, scope) {
        scope.querySelectorAll('[data-en], [data-he]').forEach((el) => {
            if (el.dataset[lang]) {
                el.textContent = el.dataset[lang];
            }
        });
    }

    // index.html's head script marks a Hebrew load data-lang-pending, which
    // keeps [data-he] text unseen until toggleLanguage() swaps it in. A region
    // that calls this from an inline script right after its markup gets its
    // Hebrew text laid out before first paint instead, so the swap doesn't
    // shift the page, and data-lang-ready shows it at once (style.css).
    function swapPendingLanguage(scope) {
        const html = document.documentElement;
        if (!scope || !html.hasAttribute('data-lang-pending')) return;
        const lang = html.getAttribute('lang') === 'he' ? 'he' : 'en';
        swapLanguageText(lang, scope);
        scope.querySelectorAll(`[data-${lang}-placeholder]`).forEach((el) => {
            el.placeholder = el.dataset[`${lang}Placeholder`];
        });
        scope.setAttribute('data-lang-ready', '');
    }

    // The shared geometry of both positionFloatingMenu() implementations:
    // where a dropdown should sit given its trigger button and the shared
    // topbar's bottom edge. Returns null when there's nothing to position.
    // Callers apply the position (and anything page-specific, like the
    // transform-cancelling technique) themselves.
    function computeFloatingMenuPosition(menuEl, triggerEl, options) {
        const opts = options || {};
        if (!menuEl || !triggerEl) return null;

        const viewportWidth = Math.max(Math.round(window.innerWidth || 0), 320);
        const viewportHeight = Math.max(Math.round(window.innerHeight || 0), 320);
        const padding = Number.isFinite(opts.padding) ? opts.padding : 8;
        const offset = Number.isFinite(opts.offset) ? opts.offset : 0;
        const minWidth = Number.isFinite(opts.minWidth) ? opts.minWidth : 160;
        const maxWidth = Number.isFinite(opts.maxWidth) ? opts.maxWidth : 352;
        const triggerRect = triggerEl.getBoundingClientRect();
        const panelWidth = Math.min(maxWidth, Math.max(minWidth, viewportWidth - (padding * 2)));

        const preferredLeft = Math.round(triggerRect.left + triggerRect.width / 2 - panelWidth / 2);
        const left = Math.max(padding, Math.min(preferredLeft, viewportWidth - panelWidth - padding));

        const topbarEl = document.querySelector('header[role="banner"]');
        const anchorBottom = topbarEl ? topbarEl.getBoundingClientRect().bottom : triggerRect.bottom;
        const top = Math.min(viewportHeight - padding, Math.round(anchorBottom + offset));

        return {
            left,
            top,
            width: panelWidth,
            maxWidth: Math.max(0, viewportWidth - (padding * 2)),
        };
    }

    const api = { applyLanguagePreference, swapPendingLanguage, computeFloatingMenuPosition };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    } else {
        root.applyLanguagePreference = applyLanguagePreference;
        root.swapPendingLanguage = swapPendingLanguage;
        root.computeFloatingMenuPosition = computeFloatingMenuPosition;
    }
})(typeof window !== 'undefined' ? window : globalThis);
