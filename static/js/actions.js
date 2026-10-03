/**
 * Delegated click/input dispatcher -- the replacement for inline
 * onclick="..." / oninput="..." attributes.
 *
 * The Content-Security-Policy no longer carries 'unsafe-inline' for scripts
 * (backend/csp.py), and an inline event-handler attribute is script the
 * browser would refuse to run. Markup says what it wants with data
 * attributes instead:
 *
 *   <button data-onclick="readText" data-onclick-arg="Genesis 1">
 *   <button data-onclick="goHome">
 *   <input  data-oninput="handlePrivacyDeleteConfirmInput">
 *
 * and one capture-phase listener on `document` resolves the name against an
 * explicit allowlist, then calls the global function of that name with the
 * (optional) string argument. `data-onclick-encoding="uri"` marks an argument
 * that was written with encodeURIComponent() so it can carry any characters;
 * it is decoded here, which is what the old
 * onclick="fn(decodeURIComponent('...'))" form did inline.
 *
 * Three properties matter:
 *
 * - The allowlist is closed. A `data-onclick` naming anything else is
 *   ignored, so this is not a way to call arbitrary globals (eval, fetch, ...).
 * - Names are looked up on `window` when the click happens, not when the page
 *   loads, exactly as an inline handler resolved them -- so a function that is
 *   defined later in the page, or replaced by another script, behaves the same.
 * - The listener runs in the capture phase so a descendant or ancestor that
 *   calls stopPropagation() cannot swallow a click the markup asked for.
 *
 * ATTRIBUTES is exported for DOMPurify's FORBID_ATTR: DOMPurify keeps data-*
 * attributes by default, and HTML the model wrote must never be able to carry
 * one of these. (An inline onclick would have been stripped; a data attribute
 * is not.)
 *
 * Loaded as a plain classic <script> so it is live before the page's own
 * (large) inline script has finished parsing. Also loadable from Node for
 * tests_js/actions.test.js.
 */
(function (root) {
    'use strict';

    // Every function an inline handler used to name. Keep sorted; a
    // data-onclick/-oninput value in a template that is not here is a bug
    // (tests/test_inline_handlers.py fails the build on it).
    const ACTIONS = Object.freeze([
        'closeCalendarModal',
        'closePrivacyDataModal',
        'closeTextsMenu',
        'displayCommunity',
        'displayPrayer',
        'goHome',
        'handlePrivacyDataExport',
        'handlePrivacyDeleteAccount',
        'handlePrivacyDeleteConfirmInput',
        'openCalendarModal',
        'openCitySearch',
        'openCommentaryInline',
        'openLibraryCategory',
        'openLibrarySidebar',
        'openRashiCommentaryFromLibrary',
        'openTodayInCalendar',
        'readText',
        'renderSelectionInsightsChooser',
        'searchByCity',
        'toggleLanguage',
    ]);

    const ATTRIBUTES = Object.freeze([
        'data-onclick',
        'data-onclick-arg',
        'data-onclick-encoding',
        'data-oninput',
    ]);

    const ALLOWED = new Set(ACTIONS);

    // Run the action named by the nearest [data-on<type>] ancestor of the
    // event target. Returns true when an action was called.
    function dispatch(type, event, scope) {
        const target = event && event.target;
        if (!target || typeof target.closest !== 'function') return false;

        const attr = 'data-on' + type;
        const el = target.closest('[' + attr + ']');
        // A disabled control gets no click from the browser; a child of one
        // (an icon inside a disabled button) must not sneak one through here.
        if (!el || el.disabled) return false;

        const name = el.getAttribute(attr);
        if (!ALLOWED.has(name)) return false;

        const fn = scope[name];
        if (typeof fn !== 'function') return false;

        const argAttr = attr + '-arg';
        if (!el.hasAttribute(argAttr)) {
            fn.call(scope);
            return true;
        }
        let arg = el.getAttribute(argAttr);
        if (el.getAttribute(attr + '-encoding') === 'uri') {
            arg = decodeURIComponent(arg);
        }
        fn.call(scope, arg);
        return true;
    }

    function install(doc, scope) {
        doc.addEventListener('click', (event) => dispatch('click', event, scope), true);
        doc.addEventListener('input', (event) => dispatch('input', event, scope), true);
    }

    const api = { ACTIONS, ATTRIBUTES, dispatch, install };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    } else {
        root.ShelahActions = api;
        install(root.document, root);
    }
})(typeof window !== 'undefined' ? window : globalThis);
