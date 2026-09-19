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

// motion@12.43.0's spring() is a low-level generator that requires its own
// `keyframes` and is meant for advanced use (e.g. a spring visualiser) --
// animate()'s documented way to drive a spring transition is `type: spring`
// alongside the physics params, not `easing: spring(config)` (which throws,
// since that config lacks the keyframes spring() itself expects).
function _springTransition(config, extra = {}) {
    const spring = window.Motion?.spring ?? null;
    if (!spring) {
        return { duration: config.duration ?? 0.22, ...extra };
    }
    return { type: spring, ...config, ...extra };
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
        _springTransition(SPRING_ENTER, { delay }),
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
        _springTransition(SPRING_EXIT, { delay }),
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
        _springTransition(SPRING_ENTER, { delay: stagger(staggerDelay) }),
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
        _springTransition(SPRING_MOVE, { delay }),
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
        _springTransition(SPRING_ENTER),
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
        _springTransition(SPRING_EXIT),
    );
}

// ── Presence: animated show / hide for menus, sheets and modals ─────────────
//
// present() / dismiss() are the vanilla stand-in for <AnimatePresence>: they
// own the `hidden` class, so an element is only display:none once its exit has
// finished, and they are safe to call in any order (a show during an exit
// cancels it and vice versa -- `token` discards the stale completion).
//
// Each preset is the offset an element rises from on enter / sinks to on exit;
// the transform is always written with the SAME function list (translateY +
// scale) at both ends, which is what lets motion interpolate the strings.
const _PRESENCE = {
    // Anchored dropdowns: rise a few px into place.
    popover: { y: 12, scale: 0.98, exitY: 8, exitScale: 0.98, spring: SPRING_ENTER },
    // Phone bottom sheets / modal cards: slide up from below.
    sheet:   { y: 56, scale: 1,    exitY: 40, exitScale: 1,   spring: { stiffness: 380, damping: 34, mass: 0.8 } },
    // Centred modal cards on larger screens.
    modal:   { y: 18, scale: 0.97, exitY: 10, exitScale: 0.98, spring: SPRING_ENTER },
    // Opacity only (scrims, full-screen dialog shells).
    fade:    { y: 0,  scale: 1,    exitY: 0,  exitScale: 1,   spring: SPRING_ENTER },
};
// Exit springs are critically damped so the element settles without a rebound
// while it is already leaving.
const SPRING_LEAVE = { stiffness: 420, damping: 40, mass: 0.8 };
const _presence = new WeakMap();

const _xf = (y, scale) => `translateY(${y}px) scale(${scale})`;

function _presenceState(el) {
    let state = _presence.get(el);
    if (!state) {
        state = { token: 0, controls: [] };
        _presence.set(el, state);
    }
    state.controls.forEach((c) => c.stop?.());
    state.controls = [];
    return state;
}

// motion writes an animation's final value back to the element's inline style
// as it finishes, which can land just after the promise it resolves; wait a
// macrotask before clearing so that write cannot overwrite the cleanup.
const _settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function _clearMotionStyles(...els) {
    els.forEach((el) => {
        if (!el) return;
        el.style.removeProperty('opacity');
        el.style.removeProperty('transform');
    });
}

/**
 * Show `el` with an entrance. `card` (optional) is a child that carries the
 * movement while `el` itself only fades -- the shape of a modal, where `el` is
 * the scrim/dialog shell and `card` the panel.  Resolves once settled.
 *
 * `replay: true` is for a caller that had to un-hide the element itself first
 * (a <dialog> must be rendered when showModal() runs, or focus cannot move
 * into it); without it an element that is already showing is left alone.
 */
export async function present(el, { preset = 'popover', card = null, replay = false } = {}) {
    if (!el) return;
    const wasHidden = el.classList.contains('hidden');
    const leaving = el.classList.contains('is-hiding');
    // Already showing (or still entering): nothing to replay.
    if (!wasHidden && !leaving && !replay) return;
    // Cancelling an exit half-way: fade back in from where it had got to.
    const startOpacity = wasHidden || (replay && !leaving) ? 0 : Number.parseFloat(getComputedStyle(el).opacity) || 0;
    const state = _presenceState(el);
    const token = ++state.token;
    el.classList.remove('hidden', 'is-hiding');

    const animate = _motionAnimate();
    const p = _PRESENCE[preset] ?? _PRESENCE.popover;
    if (!animate || isMotionReduced()) {
        _clearMotionStyles(el, card);
        return;
    }

    const mover = card ?? (p.y || p.scale !== 1 ? el : null);
    el.style.opacity = String(startOpacity);
    if (mover) mover.style.transform = _xf(p.y, p.scale);

    const controls = [animate(el, { opacity: [startOpacity, 1] }, { duration: 0.18, ease: [0.25, 0, 0.3, 1] })];
    if (mover) {
        controls.push(animate(mover, { transform: [_xf(p.y, p.scale), _xf(0, 1)] }, _springTransition(p.spring)));
    }
    state.controls = controls;
    await Promise.all(controls);
    await _settle();
    if (state.token !== token) return; // superseded by a dismiss()
    _clearMotionStyles(el, card);
}

/**
 * Hide `el` with an exit, then add `hidden`.  Runs `onHidden` afterwards (a
 * <dialog>'s close() belongs there: closing earlier would drop it out of the
 * top layer mid-animation).  Resolves once hidden.
 */
export async function dismiss(el, { preset = 'popover', card = null, onHidden } = {}) {
    if (!el) return;
    if (el.classList.contains('hidden')) {
        onHidden?.();
        return;
    }
    const state = _presenceState(el);
    const token = ++state.token;
    const finish = () => {
        el.classList.add('hidden');
        el.classList.remove('is-hiding');
        _clearMotionStyles(el, card);
        onHidden?.();
    };

    const animate = _motionAnimate();
    const p = _PRESENCE[preset] ?? _PRESENCE.popover;
    if (!animate || isMotionReduced()) {
        finish();
        return;
    }

    el.classList.add('is-hiding'); // pointer-events: none while it leaves
    const mover = card ?? (p.exitY || p.exitScale !== 1 ? el : null);
    const fade = animate(el, { opacity: 0 }, { duration: 0.16, ease: [0.4, 0, 1, 1] });
    const controls = [fade];
    if (mover) {
        controls.push(animate(mover, { transform: [_xf(0, 1), _xf(p.exitY, p.exitScale)] }, _springTransition(SPRING_LEAVE)));
    }
    state.controls = controls;
    // Hide as soon as it has faded; the (longer) spring is cut off while invisible.
    await fade;
    if (state.token !== token) return; // superseded by a present()
    controls.forEach((c) => c.stop?.());
    await _settle();
    if (state.token !== token) return;
    finish();
}

/**
 * Slide a highlight element to `x` (px, from the container's left edge).
 * Used by the bottom tab bar's active-tab pill: one element that travels
 * between buttons instead of each button repainting its own background.
 * The first placement, or any call with animate:false (resize, theme change),
 * places it without movement.
 *
 * Idempotent per target: a repeat call for the x it is already travelling to
 * (or resting at) does nothing.  Tapping Search runs the tab sync several times
 * in one gesture (click handler, expand handler, focus handler); each of those
 * used to stop the spring in flight and snap the pill to its destination.
 */
const _slides = new WeakMap(); // el -> { target, control }

export function slideTo(el, x, { animate: withMotion = true } = {}) {
    if (!el) return;
    const slide = _slides.get(el);
    if (slide && slide.target === x) return slide.control;

    const place = () => {
        slide?.control?.stop?.();
        el.style.transform = `translateX(${x}px)`;
        _slides.set(el, { target: x, control: null });
    };

    const animate = _motionAnimate();
    // No prior slide and no inline transform: first paint, nothing to travel from.
    const placedBefore = Boolean(slide) || Boolean(el.style.transform);
    if (!withMotion || !animate || isMotionReduced() || !placedBefore) {
        place();
        return;
    }

    // Continue from where an interrupted slide currently is (or from the
    // instantly-placed position if the module loaded after the first paint).
    const m = new DOMMatrixReadOnly(getComputedStyle(el).transform);
    const start = Number.isFinite(m.m41) ? m.m41 : x;
    if (Math.abs(start - x) < 0.5) {
        place();
        return;
    }
    slide?.control?.stop?.();
    const control = animate(
        el,
        { transform: [`translateX(${start}px)`, `translateX(${x}px)`] },
        _springTransition({ stiffness: 420, damping: 32, mass: 0.8 }),
    );
    const entry = { target: x, control };
    _slides.set(el, entry);
    // Once it lands the pill is simply "resting at x"; drop the handle.
    const done = () => { if (_slides.get(el) === entry) entry.control = null; };
    control.then?.(done, done);
    return control;
}

// Expose on window so legacy inline-script code can call without an import
window.ShelahMotion = {
    present,
    dismiss,
    slideTo,
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
