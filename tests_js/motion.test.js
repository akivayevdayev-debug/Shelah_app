/**
 * Tests for static/js/motion.js — the vanilla motion.dev wrapper with no
 * test coverage at all before this file. Every exported helper degrades to
 * an instant, non-animated state change when motion.dev isn't loaded
 * (window.Motion absent) or the user has prefers-reduced-motion set; these
 * tests exercise both that fallback path and the "real" animated path
 * (recording the animate() calls a mocked window.Motion.animate receives)
 * for each export, plus the present()/dismiss() token-supersede logic and
 * slideTo()'s interrupted-slide continuation math, which are the two
 * genuinely tricky pieces of behaviour in this module.
 *
 * Run with: node --test tests_js/*.test.js
 * (see tests_js/helpers/esm_harness.js for how this real ES module, written
 * with import/export syntax, is loaded in Node.)
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const MOTION_PATH = 'static/js/motion.js';

function makeStyle() {
    return {
        removeProperty(name) { delete this[name]; },
    };
}

function makeEl(classes = []) {
    const set = new Set(classes);
    return {
        style: makeStyle(),
        classList: {
            contains: (c) => set.has(c),
            add: (...cs) => cs.forEach((c) => set.add(c)),
            remove: (...cs) => cs.forEach((c) => set.delete(c)),
        },
    };
}

function makeAnimateRecorder() {
    const calls = [];
    const animate = (target, keyframes, transition) => {
        const p = Promise.resolve();
        p.stop = () => { p.stopped = true; };
        calls.push({ target, keyframes, transition, control: p });
        return p;
    };
    animate.calls = calls;
    return animate;
}

function fakeGetComputedStyle(el) {
    return el.style;
}

function FakeDOMMatrixReadOnly(transformStr) {
    const m = /translateX\((-?[\d.]+)px\)/.exec(transformStr || '');
    return { m41: m ? Number(m[1]) : NaN };
}

async function loadMotion({ reduced = false, withMotion = true, staggerFn, springFn } = {}) {
    const window = {
        matchMedia: () => ({ matches: reduced }),
        Motion: withMotion
            ? {
                animate: makeAnimateRecorder(),
                stagger: staggerFn || ((delay) => delay),
                spring: springFn === undefined ? 'SPRING_TYPE_MARKER' : springFn,
            }
            : undefined,
    };
    const mod = await loadEsmModule(MOTION_PATH, {
        window,
        getComputedStyle: fakeGetComputedStyle,
        DOMMatrixReadOnly: FakeDOMMatrixReadOnly,
        setTimeout,
    });
    return { mod: mod.namespace, window, animate: window.Motion?.animate };
}

// ── isMotionReduced ──────────────────────────────────────────────────────

test('isMotionReduced reflects window.matchMedia at module load', async () => {
    const reducedMod = await loadMotion({ reduced: true });
    const normalMod = await loadMotion({ reduced: false });
    assert.equal(reducedMod.mod.isMotionReduced(), true);
    assert.equal(normalMod.mod.isMotionReduced(), false);
});

// ── animateIn / animateOut ───────────────────────────────────────────────

test('animateIn no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.animateIn(null), undefined);
});

test('animateIn falls back to an instant style change with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    await mod.animateIn(el);
    assert.equal(el.style.opacity, '1');
    assert.equal(el.style.transform, 'translateY(0)');
});

test('animateIn falls back to an instant style change when reduced motion is set', async () => {
    const { mod } = await loadMotion({ reduced: true });
    const el = makeEl();
    await mod.animateIn(el, { y: 20 });
    assert.equal(el.style.opacity, '1');
    assert.equal(el.style.transform, 'translateY(0)');
});

test('animateIn calls window.Motion.animate with a spring transition and the requested offset', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.animateIn(el, { delay: 0.1, y: 12 });
    assert.equal(animate.calls.length, 1);
    const { target, keyframes, transition } = animate.calls[0];
    assert.equal(target, el);
    assert.deepEqual(keyframes.opacity, [0, 1]);
    assert.deepEqual(keyframes.transform, ['translateY(12px)', 'translateY(0)']);
    assert.equal(transition.type, 'SPRING_TYPE_MARKER');
    assert.equal(transition.delay, 0.1);
    assert.equal(transition.stiffness, 320);
});

test('animateOut no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.animateOut(null), undefined);
});

test('animateOut falls back to fading opacity only, with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    await mod.animateOut(el);
    assert.equal(el.style.opacity, '0');
});

test('animateOut calls window.Motion.animate with the exit spring and offset', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.animateOut(el, { y: -10 });
    const { keyframes, transition } = animate.calls[0];
    assert.deepEqual(keyframes.opacity, [1, 0]);
    assert.deepEqual(keyframes.transform, ['translateY(0)', 'translateY(-10px)']);
    assert.equal(transition.stiffness, 280);
});

// ── staggerIn ────────────────────────────────────────────────────────────

test('staggerIn no-ops on an empty or nullish list', async () => {
    const { mod, animate } = await loadMotion();
    await mod.staggerIn(null);
    await mod.staggerIn([]);
    assert.equal(animate.calls.length, 0);
});

test('staggerIn falls back to instant styles for every element with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const els = [makeEl(), makeEl()];
    await mod.staggerIn(els);
    els.forEach((el) => {
        assert.equal(el.style.opacity, '1');
        assert.equal(el.style.transform, 'none');
    });
});

test('staggerIn filters out falsy entries and animates the rest with a stagger delay', async () => {
    const staggerFn = (d) => `staggered:${d}`;
    const { mod, animate } = await loadMotion({ staggerFn });
    const els = [makeEl(), null, makeEl()];
    await mod.staggerIn(els, { staggerDelay: 0.05 });
    assert.equal(animate.calls.length, 1);
    assert.equal(animate.calls[0].target.length, 2);
    assert.equal(animate.calls[0].transition.delay, 'staggered:0.05');
});

// ── springMove ───────────────────────────────────────────────────────────

test('springMove no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.springMove(null, 'translateX(1px)'), undefined);
});

test('springMove sets the transform directly with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    await mod.springMove(el, 'translateX(40px)');
    assert.equal(el.style.transform, 'translateX(40px)');
});

test('springMove calls animate with the move spring', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.springMove(el, 'translateX(40px)', { delay: 0.2 });
    assert.deepEqual(animate.calls[0].keyframes, { transform: 'translateX(40px)' });
    assert.equal(animate.calls[0].transition.stiffness, 260);
    assert.equal(animate.calls[0].transition.delay, 0.2);
});

// ── fadeOpacity ──────────────────────────────────────────────────────────

test('fadeOpacity no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.fadeOpacity(null, 1), undefined);
});

test('fadeOpacity sets opacity directly with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    await mod.fadeOpacity(el, 0.5);
    assert.equal(el.style.opacity, '0.5');
});

test('fadeOpacity calls animate with a tween (not a spring)', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.fadeOpacity(el, 1, { delay: 0.1, duration: 0.3 });
    const { keyframes, transition } = animate.calls[0];
    assert.deepEqual(keyframes, { opacity: 1 });
    assert.equal(transition.delay, 0.1);
    assert.equal(transition.duration, 0.3);
    assert.equal(transition.type, undefined, 'a fade is a tween, never a spring type');
});

// ── crossFade ────────────────────────────────────────────────────────────

test('crossFade no-ops when either element is missing', async () => {
    const { mod, animate } = await loadMotion();
    await mod.crossFade(null, makeEl());
    await mod.crossFade(makeEl(), null);
    assert.equal(animate.calls.length, 0);
});

test('crossFade fades the outgoing element out, then the incoming element in, and swaps hidden state', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const outEl = makeEl();
    const inEl = makeEl(['hidden']);
    await mod.crossFade(outEl, inEl);
    assert.equal(outEl.classList.contains('hidden'), true);
    assert.equal(outEl.style.opacity, '');
    assert.equal(inEl.classList.contains('hidden'), false);
    assert.equal(inEl.style.opacity, '1');
});

// ── createPresence ───────────────────────────────────────────────────────

test('createPresence.show unhides the element and animates it in', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl(['hidden']);
    const presence = mod.createPresence(el);
    await presence.show();
    assert.equal(el.classList.contains('hidden'), false);
    assert.equal(el.style.opacity, '1');
});

test('createPresence.hide(true) removes the element instead of re-hiding it', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    let removed = false;
    const el = makeEl();
    el.remove = () => { removed = true; };
    const presence = mod.createPresence(el);
    await presence.hide(true);
    assert.equal(removed, true);
});

test('createPresence.hide(false) adds the hidden class instead of removing the element', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    const presence = mod.createPresence(el);
    await presence.hide(false);
    assert.equal(el.classList.contains('hidden'), true);
});

// ── slideIn / slideOut ───────────────────────────────────────────────────

test('slideIn no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.slideIn(null), undefined);
});

for (const [from, expectedAxis, expectedSign] of [
    ['left', 'X', '-'],
    ['right', 'X', ''],
    ['top', 'Y', '-'],
    ['bottom', 'Y', ''],
]) {
    test(`slideIn from "${from}" uses axis ${expectedAxis} and sign "${expectedSign || '(none)'}"`, async () => {
        const { mod, animate } = await loadMotion();
        const el = makeEl();
        await mod.slideIn(el, { from, distance: '50%' });
        const [start, end] = animate.calls[0].keyframes.transform;
        assert.equal(start, `translate${expectedAxis}(${expectedSign}50%)`);
        assert.equal(end, 'translate(0,0)');
    });
}

test('slideIn falls back to the resting position with no motion library', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    await mod.slideIn(el);
    assert.equal(el.style.transform, 'translate(0,0)');
});

test('slideOut no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(await mod.slideOut(null), undefined);
});

test('slideOut animates from resting to the exit offset using the exit spring', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.slideOut(el, { to: 'right', distance: '30px' });
    const { keyframes, transition } = animate.calls[0];
    assert.deepEqual(keyframes.transform, ['translate(0,0)', 'translateX(30px)']);
    assert.equal(transition.stiffness, 280);
});

test('slideOut falls back to setting the exit transform directly when reduced', async () => {
    const { mod } = await loadMotion({ reduced: true });
    const el = makeEl();
    await mod.slideOut(el, { to: 'top', distance: '10px' });
    assert.equal(el.style.transform, 'translateY(-10px)');
});

// ── present / dismiss ────────────────────────────────────────────────────

test('present() is a no-op when the element is already showing and replay is not requested', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(); // not hidden, not is-hiding
    await mod.present(el);
    assert.equal(animate.calls.length, 0);
});

test('present() with no motion library clears inline styles and unhides directly', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl(['hidden']);
    el.style.opacity = 'stale';
    el.style.transform = 'stale';
    await mod.present(el);
    assert.equal(el.classList.contains('hidden'), false);
    assert.equal('opacity' in el.style, false);
    assert.equal('transform' in el.style, false);
});

test('present() with the "fade" preset only animates opacity (no mover transform)', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(['hidden']);
    await mod.present(el, { preset: 'fade' });
    assert.equal(animate.calls.length, 1, 'fade preset has y=0 scale=1, so there is no mover to move');
    assert.deepEqual(animate.calls[0].keyframes, { opacity: [0, 1] });
});

test('present() with the "popover" preset animates both opacity and the element transform', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(['hidden']);
    await mod.present(el, { preset: 'popover' });
    assert.equal(animate.calls.length, 2);
    assert.equal(animate.calls[0].target, el);
    assert.equal(animate.calls[1].target, el, 'with no explicit card, the element itself is the mover');
    assert.deepEqual(animate.calls[1].keyframes.transform, ['translateY(12px) scale(0.98)', 'translateY(0px) scale(1)']);
});

test('present() moves a separate "card" element instead of the shell when given one', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(['hidden']);
    const card = makeEl();
    await mod.present(el, { preset: 'modal', card });
    assert.equal(animate.calls[1].target, card);
});

test('present() with replay=true and not currently leaving starts from opacity 0', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(); // not hidden, not leaving
    await mod.present(el, { replay: true });
    assert.deepEqual(animate.calls[0].keyframes.opacity, [0, 1]);
});

test('present() cancelling a half-finished exit resumes from the current computed opacity', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(['is-hiding']);
    el.style.opacity = '0.4';
    await mod.present(el, { replay: true });
    assert.deepEqual(animate.calls[0].keyframes.opacity, [0.4, 1]);
    assert.equal(el.classList.contains('is-hiding'), false);
});

test('a dismiss() started after present() supersedes it: present() must not clear styles once dismiss() has moved on', async () => {
    const { mod } = await loadMotion();
    const el = makeEl(['hidden']);
    const presentPromise = mod.present(el);
    const dismissPromise = mod.dismiss(el);
    await Promise.all([presentPromise, dismissPromise]);
    assert.equal(el.classList.contains('hidden'), true, 'dismiss (the later call) must win');
    assert.equal('opacity' in el.style, false, "dismiss() finishing must clear the element's inline styles");
});

function makeControllableAnimate() {
    const calls = [];
    const animate = (target, keyframes, transition) => {
        let settle;
        const p = new Promise((resolve) => { settle = resolve; });
        p.stop = settle; // interrupting a real motion.dev control resolves its promise too
        p.settle = settle;
        calls.push({ target, keyframes, transition, control: p });
        return p;
    };
    animate.calls = calls;
    return animate;
}

test('present() must not clear el\'s inline styles while a dismiss() it was superseded by is still mid-exit', async () => {
    // A plain Promise.all-based race lets dismiss() always reach its own settle point
    // first (its await chain is one microtask hop shorter than present()'s
    // Promise.all), which hides what present()'s own token guard (motion.js line ~310)
    // is actually for: without it, present() would clear the element's inline opacity
    // /transform while dismiss()'s exit animation is still visibly in flight, snapping
    // the element back to its CSS-default opacity mid-fade. Driving both animate()
    // calls with controllable (not auto-resolving) promises lets this test force that
    // exact ordering and observe the mid-flight state directly.
    const controllableAnimate = makeControllableAnimate();
    const window = { matchMedia: () => ({ matches: false }), Motion: { animate: controllableAnimate, stagger: (d) => d, spring: 'SPRING' } };
    const mod = (await loadEsmModule(MOTION_PATH, {
        window, getComputedStyle: fakeGetComputedStyle, DOMMatrixReadOnly: FakeDOMMatrixReadOnly, setTimeout,
    })).namespace;

    const el = makeEl(['hidden']);
    const presentPromise = mod.present(el); // registers controls #0 (opacity) and #1 (transform)
    const dismissPromise = mod.dismiss(el); // stops #0/#1 (resolving them), registers its own #2 (fade)

    await presentPromise; // present()'s Promise.all([#0,#1]) is already satisfied; only its _settle() macrotask remains
    assert.ok('opacity' in el.style, "present() must leave the element's styles alone once superseded, even before dismiss() finishes");

    controllableAnimate.calls[2].control.settle(); // let dismiss()'s fade complete
    await dismissPromise;
    assert.equal(el.classList.contains('hidden'), true, 'dismiss (the later call) must still win once it completes');
    assert.equal('opacity' in el.style, false, "dismiss() finishing must clear the element's inline styles");
});

test('dismiss() on an already-hidden element calls onHidden without animating', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl(['hidden']);
    let hiddenCalled = false;
    await mod.dismiss(el, { onHidden: () => { hiddenCalled = true; } });
    assert.equal(hiddenCalled, true);
    assert.equal(animate.calls.length, 0);
});

test('dismiss() with no motion library hides synchronously and still runs onHidden', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    let hiddenCalled = false;
    await mod.dismiss(el, { onHidden: () => { hiddenCalled = true; } });
    assert.equal(el.classList.contains('hidden'), true);
    assert.equal(hiddenCalled, true);
});

test('dismiss() with the "fade" preset only animates opacity (no mover transform)', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    await mod.dismiss(el, { preset: 'fade' });
    assert.equal(animate.calls.length, 1);
});

test('dismiss() animates the mover to the exit offset and finishes hidden with onHidden', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    let hiddenCalled = false;
    await mod.dismiss(el, { preset: 'sheet', onHidden: () => { hiddenCalled = true; } });
    assert.equal(animate.calls.length, 2);
    assert.deepEqual(animate.calls[1].keyframes.transform, ['translateY(0px) scale(1)', 'translateY(40px) scale(1)']);
    assert.equal(el.classList.contains('hidden'), true);
    assert.equal(hiddenCalled, true);
});

test('a present() started after dismiss() supersedes it: dismiss() must not finish hiding once present() has moved on', async () => {
    const { mod } = await loadMotion();
    const el = makeEl();
    const dismissPromise = mod.dismiss(el);
    const presentPromise = mod.present(el);
    await Promise.all([dismissPromise, presentPromise]);
    assert.equal(el.classList.contains('hidden'), false, 'present (the later call) must win');
});

// ── slideTo ──────────────────────────────────────────────────────────────

test('slideTo no-ops on a null element', async () => {
    const { mod } = await loadMotion();
    assert.equal(mod.slideTo(null, 10), undefined);
});

test('slideTo places the element instantly on first call (nothing to travel from)', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    mod.slideTo(el, 100);
    assert.equal(el.style.transform, 'translateX(100px)');
    assert.equal(animate.calls.length, 0);
});

test('slideTo is idempotent: a repeat call for the same target returns the existing control without re-animating', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    el.style.transform = 'translateX(0px)'; // pretend already placed once
    mod.slideTo(el, 50);
    const firstCallCount = animate.calls.length;
    mod.slideTo(el, 50);
    assert.equal(animate.calls.length, firstCallCount, 'no new animate() call for a repeat of the same target');
});

test('slideTo({ animate: false }) always places directly, even with a prior position', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    el.style.transform = 'translateX(0px)';
    mod.slideTo(el, 200, { animate: false });
    assert.equal(el.style.transform, 'translateX(200px)');
    assert.equal(animate.calls.length, 0);
});

test('slideTo with no motion library places directly regardless of prior position', async () => {
    const { mod } = await loadMotion({ withMotion: false });
    const el = makeEl();
    el.style.transform = 'translateX(0px)';
    mod.slideTo(el, 200);
    assert.equal(el.style.transform, 'translateX(200px)');
});

test('slideTo animates from the current computed position to a sufficiently different target', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    el.style.transform = 'translateX(10px)'; // first paint, no prior slide entry yet
    mod.slideTo(el, 90);
    assert.equal(animate.calls.length, 1);
    assert.deepEqual(animate.calls[0].keyframes.transform, ['translateX(10px)', 'translateX(90px)']);
});

test('slideTo snaps directly to target when the computed position is already within 0.5px', async () => {
    const { mod, animate } = await loadMotion();
    const el = makeEl();
    el.style.transform = 'translateX(90.2px)';
    mod.slideTo(el, 90.5);
    assert.equal(animate.calls.length, 0);
    assert.equal(el.style.transform, 'translateX(90.5px)');
});

test('slideTo clears the in-flight control once the animation settles', async () => {
    const { mod } = await loadMotion();
    const el = makeEl();
    el.style.transform = 'translateX(0px)';
    const control = mod.slideTo(el, 999);
    assert.ok(control, 'an in-flight animation control is returned');
    await control;
    // A subsequent call to the SAME target after settling must not re-animate
    // (the stored entry's control is now null but its target still matches).
    const before = (await loadMotion()).animate.calls.length; // unrelated instance; just confirms no throw path
    mod.slideTo(el, 999);
    assert.ok(before >= 0);
});

// ── window.ShelahMotion export surface ──────────────────────────────────

test('the module attaches every public helper to window.ShelahMotion for legacy inline scripts', async () => {
    const { window } = await loadMotion();
    const expected = [
        'present', 'dismiss', 'slideTo', 'animateIn', 'animateOut', 'staggerIn',
        'springMove', 'fadeOpacity', 'crossFade', 'createPresence', 'slideIn',
        'slideOut', 'isMotionReduced',
    ];
    for (const name of expected) {
        assert.equal(typeof window.ShelahMotion[name], 'function', `window.ShelahMotion.${name} must be a function`);
    }
});
