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

// ── Apple spring parameterisation ────────────────────────────────────────────
//
// Apple describes a spring by how long it takes to settle and how much it
// overshoots (WWDC23 "Animate with springs"): `duration` (s) and `bounce`
// (0 = critically damped, ~0.15 = a hair of overshoot, ~0.3 = playful).
// With unit mass those map to the physical model as
//     stiffness = (2π / duration)²      damping = 4π · (1 − bounce) / duration
// so the same two numbers can drive motion.dev's stiffness/damping/mass.
export function appleSpring(duration, bounce = 0) {
    const w = (2 * Math.PI) / duration;
    return { stiffness: w * w, damping: 2 * w * (1 - bounce), mass: 1 };
}

// Named presets, matching SwiftUI's .smooth / .snappy / .bouncy (0.5 s each).
export const APPLE_SPRING = {
    smooth: appleSpring(0.5, 0),
    snappy: appleSpring(0.5, 0.15),
    bouncy: appleSpring(0.5, 0.3),
};

/**
 * Spring `keyframes` onto `el`, carrying `velocity` (px/s, or the unit of the
 * animated property) into the spring so a release from a drag continues at the
 * finger's speed instead of restarting from rest.  Returns motion's controls,
 * or null when motion is unavailable/reduced (the caller then sets the end
 * state itself).
 */
export function springAnimate(el, keyframes, spring, { velocity } = {}) {
    const animate = _motionAnimate();
    if (!el || !animate || isMotionReduced()) return null;
    const extra = Number.isFinite(velocity) ? { velocity } : {};
    return animate(el, keyframes, _springTransition(spring, extra));
}

/**
 * Spring a plain number from `from` to `to`, calling `onUpdate(value)` each
 * frame.  For gesture-driven motion (a sheet released mid-drag) the caller owns
 * the value, so an interrupting touch can read where it is right now and start
 * the next spring from there, carrying `velocity` (units/s) so there is no
 * seam between the finger and the animation.  With motion unavailable or
 * reduced it jumps to `to` and returns null.
 */
export function springValue(from, to, spring, { velocity = 0, onUpdate, onComplete } = {}) {
    const animate = _motionAnimate();
    if (!animate || isMotionReduced()) {
        onUpdate?.(to);
        onComplete?.();
        return null;
    }
    return animate(from, to, { ..._springTransition(spring, { velocity }), onUpdate, onComplete });
}

// ── Grow-from / collapse-into the trigger ────────────────────────────────────
//
// Menus and modals open out of the control that summoned them and fold back
// into it on close (spatial consistency: what leaves the way it came).  The
// mover scales about a transform-origin placed on the trigger, so no
// translation is needed and the two ends share one transform function list.
const _ORIGIN_SCALE = { popover: 0.5, modal: 0.55, emerge: 0.2 };

function _originMorph(mover, origin, preset) {
    const scale = _ORIGIN_SCALE[preset];
    if (!mover || scale === undefined || !origin || !origin.isConnected) return null;
    // Measure the settled box: an interrupted exit can leave a transform behind.
    mover.style.transform = 'none';
    const o = origin.getBoundingClientRect();
    if (!o.width && !o.height) return null; // trigger is hidden (display:none)
    const r = mover.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const x = o.left + o.width / 2 - r.left;
    const y = o.top + o.height / 2 - r.top;
    return { scale, origin: `${Math.round(x)}px ${Math.round(y)}px` };
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
//
// Anchored menus (settings, profile, reader settings, city search) are opened and
// dismissed constantly and carry no gesture momentum, so they get the quickest
// motion in the app: critically damped springs (no overshoot tail to wait out)
// that read as settled in ~0.2 s going in and ~0.13 s going out, with the fade
// finishing first.  Every other preset keeps the `_FADE` defaults and its own spring.
const SPRING_MENU_ENTER = appleSpring(0.22, 0);
const SPRING_MENU_LEAVE = appleSpring(0.14, 0);
// A modal opened by tap (not a drag/flick) carries no gesture momentum, so --
// same reasoning as the menu springs above -- it gets no bounce either. It is
// a bigger, weightier surface than a dropdown, so its response is a touch
// longer (0.32s vs. the menu's 0.22s), but critically damped, not the ~0.5s
// bouncy APPLE_SPRING.snappy that present() falls back to for a morph preset
// with no explicit morphSpring. That silent fallback was what made the
// calendar modal (the only modal opened with a trigger `origin`, so the only
// one that ever takes the morph path) feel slow next to the retuned menus.
const SPRING_MODAL_ENTER = appleSpring(0.32, 0);
const _FADE = { in: 0.18, out: 0.16, outMorph: 0.22, outEase: [0.4, 0, 1, 1] };
const _PRESENCE = {
    // Anchored dropdowns: rise a few px into place (or grow out of their trigger).
    popover: {
        y: 12, scale: 0.98, exitY: 8, exitScale: 0.98,
        spring: SPRING_MENU_ENTER, morphSpring: SPRING_MENU_ENTER, leave: SPRING_MENU_LEAVE,
        // Ease-out on the way out too: the fade starts moving at once instead of lingering.
        fade: { in: 0.12, out: 0.1, outMorph: 0.12, outEase: [0.25, 0, 0.3, 1] },
    },
    // Phone bottom sheets / modal cards: slide up from below.
    sheet:   { y: 56, scale: 1,    exitY: 40, exitScale: 1,   spring: { stiffness: 380, damping: 34, mass: 0.8 } },
    // Centred modal cards on larger screens.
    modal:   { y: 18, scale: 0.97, exitY: 10, exitScale: 0.98, spring: SPRING_ENTER, morphSpring: SPRING_MODAL_ENTER },
    // Opacity only (scrims, full-screen dialog shells).
    fade:    { y: 0,  scale: 1,    exitY: 0,  exitScale: 1,   spring: SPRING_ENTER },
    // Dialog windows (chapter grid, library category, privacy, calendar): a
    // plain cross-fade with a barely-there settle in scale -- no travel and no
    // morph out of the trigger (no _ORIGIN_SCALE entry), so every window
    // simply appears where it lives, on phones and desktop alike.
    window:  {
        y: 0, scale: 0.985, exitY: 0, exitScale: 0.985,
        spring: appleSpring(0.3, 0), leave: SPRING_MENU_LEAVE,
        fade: { in: 0.22, out: 0.16 },
    },
    // Grows out of the control that summoned it from a small seed on a soft
    // spring with a whisper of overshoot, and folds back into it on close;
    // without a trigger it rises gently instead. (The desktop AI widget used
    // this until it moved to the plain "window" cross-fade; nothing uses it
    // now.)
    emerge:  {
        y: 24, scale: 0.96, exitY: 12, exitScale: 0.97,
        spring: appleSpring(0.45, 0.12), morphSpring: appleSpring(0.5, 0.14),
        leave: appleSpring(0.26, 0),
        fade: { in: 0.16, out: 0.16, outMorph: 0.22 },
    },
    // Surfaces docked to the bottom edge on phones (the conversation sheet,
    // its minimised bar, the search tray): travel the element's full height
    // from / back off the bottom of the screen, fully opaque most of the way
    // so it reads as sliding rather than fading. `awaitExit` keeps it on
    // screen until the slide has finished instead of cutting it at the fade.
    drawer:  {
        y: 'full', scale: 1, exitY: 'full', exitScale: 1,
        spring: appleSpring(0.42, 0.08), leave: appleSpring(0.3, 0),
        fade: { in: 0.1, out: 0.24, outEase: [0.7, 0, 1, 1] },
        awaitExit: true,
    },
};
// Exit springs are critically damped so the element settles without a rebound
// while it is already leaving.
const SPRING_LEAVE = { stiffness: 420, damping: 40, mass: 0.8 };
const _presence = new WeakMap();

const _xf = (y, scale) => `translateY(${y}px) scale(${scale})`;

// 'full' offsets resolve to the element's own height plus a little clearance
// (a shadow or safe-area inset) so it starts completely off-screen.
function _offset(value, mover) {
    if (value !== 'full') return value;
    return Math.ceil((mover?.getBoundingClientRect().height || 0) + 24);
}

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
        el.style.removeProperty('transform-origin');
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
export async function present(el, { preset = 'popover', card = null, replay = false, origin = null } = {}) {
    if (!el) return;
    const wasHidden = el.classList.contains('hidden');
    const leaving = el.classList.contains('is-hiding');
    // Already showing (or still entering): nothing to replay.
    if (!wasHidden && !leaving && !replay) return;
    // Cancelling an exit half-way: fade back in from where it had got to.
    const startOpacity = wasHidden || (replay && !leaving) ? 0 : Number.parseFloat(getComputedStyle(el).opacity) || 0;
    const state = _presenceState(el);
    const token = ++state.token;
    state.origin = origin; // dismiss() folds back into it; never carry a stale trigger over
    el.classList.remove('hidden', 'is-hiding');

    const animate = _motionAnimate();
    const p = _PRESENCE[preset] ?? _PRESENCE.popover;
    if (!animate || isMotionReduced()) {
        _clearMotionStyles(el, card);
        return;
    }

    const mover = card ?? (p.y || p.scale !== 1 || state.origin ? el : null);
    const morph = _originMorph(mover, state.origin, preset);
    const from = morph ? _xf(0, morph.scale) : _xf(_offset(p.y, mover), p.scale);
    el.style.opacity = String(startOpacity);
    if (mover) {
        if (morph) mover.style.transformOrigin = morph.origin;
        mover.style.transform = from;
    }

    const fade = { ..._FADE, ...p.fade };
    const controls = [animate(el, { opacity: [startOpacity, 1] }, { duration: fade.in, ease: [0.25, 0, 0.3, 1] })];
    if (mover) {
        controls.push(animate(mover, { transform: [from, _xf(0, 1)] }, _springTransition(morph ? (p.morphSpring ?? APPLE_SPRING.snappy) : p.spring)));
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
    const mover = card ?? (p.exitY || p.exitScale !== 1 || state.origin ? el : null);
    const morph = _originMorph(mover, state.origin, preset);
    if (morph) mover.style.transformOrigin = morph.origin;
    // Folding into a trigger reads better with a touch more time on screen.
    const timing = { ..._FADE, ...p.fade };
    const fade = animate(el, { opacity: 0 }, { duration: morph ? timing.outMorph : timing.out, ease: timing.outEase });
    const controls = [fade];
    if (mover) {
        const to = morph ? _xf(0, morph.scale) : _xf(_offset(p.exitY, mover), p.exitScale);
        controls.push(animate(mover, { transform: [_xf(0, 1), to] }, _springTransition(p.leave ?? SPRING_LEAVE)));
    }
    state.controls = controls;
    // Hide as soon as it has faded; the (longer) spring is cut off while invisible.
    await (p.awaitExit ? Promise.all(controls) : fade);
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
    appleSpring,
    APPLE_SPRING,
    springAnimate,
    springValue,
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
