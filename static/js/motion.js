/**
 * motion.js — Sh'elah Vanilla Motion Helper
 *
 * Thin wrapper around the motion.dev vanilla API (motion.dev/docs/animate).
 * Each call maps 1:1 to a Framer Motion equivalent for the future React
 * migration (animate() → <motion.div>, springs → type:"spring" variants).
 *
 * ENGINEERING_RULES.md rules enforced here:
 * - Animate transform / opacity ONLY — never layout properties.
 * - Respect prefers-reduced-motion globally: skip all animations when reduced.
 * - Spring physics for movement; tweens only for opacity/color.
 * - Exit animations run before DOM removal (vanilla AnimatePresence equivalent).
 *
 * Usage:
 *   import { animateIn, animateOut, staggerIn, springMove } from './motion.js';
 *
 * The module degrades gracefully when motion.dev is not loaded (CDN failure
 * or offline); every export falls back to an instant state-change.
 */

// ── Reduced-motion guard ────────────────────────────────────────────────────
const _mq = window.matchMedia('(prefers-reduced-motion: reduce)');
export function isMotionReduced() { return _mq.matches; }

// ── motion.dev lazy accessor ────────────────────────────────────────────────
// The library is loaded from CDN as window.Motion; we don't bundle it.
// If it isn't present, all helpers fall back to no-op / instant changes.
function _motionAnimate() {
    return window.Motion?.animate ?? null;
}

function _motionStagger() {
    return window.Motion?.stagger ?? null;
}

function _spring(config) {
    return window.Motion?.spring?.(config) ?? config.duration ?? 0.22;
}


// ── Default spring presets ──────────────────────────────────────────────────
const SPRING_ENTER  = { stiffness: 320, damping: 28, mass: 0.8 };
const SPRING_EXIT   = { stiffness: 280, damping: 26, mass: 0.8 };
const SPRING_MOVE   = { stiffness: 260, damping: 22, mass: 0.9 };
const TWEEN_OPACITY = { duration: 0.2, ease: [0.25, 0, 0.3, 1] };


// ── Core primitives ─────────────────────────────────────────────────────────

/**
 * Fade + slide element in.  Returns a Promise that resolves when done.
 */
export async function animateIn(el, { delay = 0, y = 8 } = {}) {
    if (!el) return;
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.opacity = '1';
        el.style.transform = 'translateY(0)';
        return;
    }
    return animate(
        el,
        { opacity: [0, 1], transform: [`translateY(${y}px)`, 'translateY(0)'] },
        { delay, easing: _spring(SPRING_ENTER) },
    );
}

/**
 * Fade + slide element out.  Resolves when animation completes (caller
 * should remove the element from the DOM in the then() callback).
 */
export async function animateOut(el, { delay = 0, y = -6 } = {}) {
    if (!el) return;
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.opacity = '0';
        return;
    }
    return animate(
        el,
        { opacity: [1, 0], transform: ['translateY(0)', `translateY(${y}px)`] },
        { delay, easing: _spring(SPRING_EXIT) },
    );
}

/**
 * Stagger-fade a NodeList / array of elements in.
 * Replaces nth-child stagger rules (§7.1.4): works for any element count,
 * immune to sibling insertions, honors reduced-motion.
 */
export async function staggerIn(elements, { staggerDelay = 0.06, y = 8 } = {}) {
    const els = Array.from(elements ?? []).filter(Boolean);
    if (!els.length) return;
    const animate = _motionAnimate();
    const stagger = _motionStagger();
    if (!animate || !stagger || isMotionReduced()) {
        els.forEach(el => { el.style.opacity = '1'; el.style.transform = 'none'; });
        return;
    }
    return animate(
        els,
        { opacity: [0, 1], transform: [`translateY(${y}px)`, 'translateY(0)'] },
        {
            delay: stagger(staggerDelay),
            easing: _spring(SPRING_ENTER),
        },
    );
}

/**
 * Spring-move element to a new transform position.
 */
export async function springMove(el, transform, { delay = 0 } = {}) {
    if (!el) return;
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.transform = transform;
        return;
    }
    return animate(
        el,
        { transform },
        { delay, easing: _spring(SPRING_MOVE) },
    );
}

/**
 * Fade-only animation (tween, not spring — correct per ENGINEERING_RULES.md).
 */
export async function fadeOpacity(el, to, { delay = 0, duration = 0.2 } = {}) {
    if (!el) return;
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.opacity = String(to);
        return;
    }
    return animate(el, { opacity: to }, { delay, duration, easing: [0.25, 0, 0.3, 1] });
}

/**
 * Cross-fade between two elements (vanilla AnimatePresence equivalent).
 * outEl animates out, then inEl animates in.
 */
export async function crossFade(outEl, inEl, { duration = 0.18 } = {}) {
    if (!outEl || !inEl) return;
    await fadeOpacity(outEl, 0, { duration });
    inEl.style.opacity = '0';
    inEl.classList.remove('hidden');
    await fadeOpacity(inEl, 1, { duration });
    outEl.classList.add('hidden');
    outEl.style.opacity = '';
}

/**
 * Animate element in, then schedule its removal from the DOM after animating out.
 * Vanilla equivalent of <AnimatePresence>:
 *   show()  ← mount + animate in
 *   hide()  ← animate out + remove from DOM
 */
export function createPresence(el) {
    return {
        async show() {
            el.classList.remove('hidden');
            el.style.opacity = '0';
            await animateIn(el);
        },
        async hide(remove = false) {
            await animateOut(el);
            if (remove) {
                el.remove();
            } else {
                el.classList.add('hidden');
                el.style.opacity = '';
            }
        },
    };
}

/**
 * Animate a sidebar/drawer panel sliding in from the side.
 */
export async function slideIn(el, { from = 'left', distance = '100%' } = {}) {
    if (!el) return;
    const axis = from === 'left' || from === 'right' ? 'X' : 'Y';
    const sign = from === 'right' || from === 'bottom' ? '' : '-';
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.transform = 'translate(0,0)';
        return;
    }
    return animate(
        el,
        { transform: [`translate${axis}(${sign}${distance})`, 'translate(0,0)'] },
        { easing: _spring(SPRING_ENTER) },
    );
}

export async function slideOut(el, { to = 'left', distance = '100%' } = {}) {
    if (!el) return;
    const axis = to === 'left' || to === 'right' ? 'X' : 'Y';
    const sign = to === 'right' || to === 'bottom' ? '' : '-';
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        el.style.transform = `translate${axis}(${sign}${distance})`;
        return;
    }
    return animate(
        el,
        { transform: ['translate(0,0)', `translate${axis}(${sign}${distance})`] },
        { easing: _spring(SPRING_EXIT) },
    );
}

// Expose on window so legacy inline-script code can call without an import
window.ShelahMotion = {
    animateIn,
    animateOut,
    staggerIn,
    springMove,
    fadeOpacity,
    crossFade,
    createPresence,
    slideIn,
    slideOut,
    isMotionReduced,
};
