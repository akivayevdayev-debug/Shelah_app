/**
 * static/js/conversation-voice.js -- the composer's dictation button: the
 * browser's own SpeechRecognition typing into the question box.
 *
 * The recognizer, the textarea and the button are fakes that keep just enough
 * of their real contracts: the recognizer streams `results` the way Chrome's
 * does (the whole list so far, `isFinal` set as words settle), the textarea
 * is an event target, and the button records aria-pressed.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/conversation-voice.js', {});
    return mod.namespace;
}

class FakeEvent {
    constructor(type, init = {}) {
        this.type = type;
        this.isTrusted = Boolean(init.trusted);
    }
}

function fakeTarget() {
    const listeners = {};
    return {
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
        dispatchEvent(event) { (listeners[event.type] || []).forEach((fn) => fn(event)); return true; },
        listenerCount(type) { return (listeners[type] || []).length; },
    };
}

function fakeInput(value = '') {
    return Object.assign(fakeTarget(), { value });
}

function fakeButton() {
    const button = fakeTarget();
    const attrs = {};
    button.setAttribute = (name, value) => { attrs[name] = value; };
    button.getAttribute = (name) => attrs[name];
    return button;
}

// A recognizer the test drives by hand.
function fakeRecognizer() {
    const instances = [];
    class Recognizer {
        constructor() {
            this.started = 0;
            this.stopped = 0;
            this.aborted = 0;
            instances.push(this);
        }
        start() { this.started += 1; }
        stop() { this.stopped += 1; }
        abort() { this.aborted += 1; }
        // `parts` is [[transcript, isFinal], ...] -- the full list so far.
        say(parts) {
            const results = parts.map(([transcript, isFinal]) => Object.assign([{ transcript }], { isFinal }));
            this.onresult?.({ results });
        }
    }
    return { Recognizer, instances };
}

async function setup({ value = '', lang = 'en', ctor = true } = {}) {
    const mod = await load();
    const { Recognizer, instances } = fakeRecognizer();
    const input = fakeInput(value);
    const button = fakeButton();
    const states = [];
    const errors = [];
    const win = { Event: FakeEvent, ...(ctor ? { webkitSpeechRecognition: Recognizer } : {}) };
    const voice = mod.createVoiceInput({
        input, button, win,
        getLang: () => lang,
        onState: (on) => states.push(on),
        onError: (kind) => errors.push(kind),
    });
    return { mod, voice, input, button, instances, states, errors };
}

test('the interface language picks the recognition language', async () => {
    const { voiceLang } = await load();
    assert.equal(voiceLang('he'), 'he-IL');
    assert.equal(voiceLang('en'), 'en-US');
    assert.equal(voiceLang(undefined), 'en-US');
});

test('speechRecognitionCtor prefers the standard name, falls back to webkit, else null', async () => {
    const { speechRecognitionCtor } = await load();
    const std = function Standard() {};
    const webkit = function Webkit() {};
    assert.equal(speechRecognitionCtor({ SpeechRecognition: std, webkitSpeechRecognition: webkit }), std);
    assert.equal(speechRecognitionCtor({ webkitSpeechRecognition: webkit }), webkit);
    assert.equal(speechRecognitionCtor({}), null);
});

test('a browser without speech recognition gets no controller (the button stays hidden)', async () => {
    const { voice } = await setup({ ctor: false });
    assert.equal(voice, null);
});

test('joinSpoken puts one space between typed and spoken text, none at an empty or spaced edge', async () => {
    const { joinSpoken } = await load();
    assert.equal(joinSpoken('', 'hello'), 'hello');
    assert.equal(joinSpoken('When is', 'candle lighting'), 'When is candle lighting');
    assert.equal(joinSpoken('When is ', 'candle lighting'), 'When is candle lighting');
    assert.equal(joinSpoken('typed', ''), 'typed');
});

test('errorKind folds the browser codes to the few the page words differently', async () => {
    const { errorKind } = await load();
    assert.equal(errorKind('not-allowed'), 'denied');
    assert.equal(errorKind('service-not-allowed'), 'denied');
    assert.equal(errorKind('audio-capture'), 'no-mic');
    assert.equal(errorKind('network'), 'network');
    assert.equal(errorKind('language-not-supported'), 'language');
    // Silence and a deliberate abort are not errors worth a message.
    assert.equal(errorKind('no-speech'), null);
    assert.equal(errorKind('aborted'), null);
});

test('start listens in the interface language, interim and continuous, and marks the button pressed', async () => {
    const { voice, button, instances, states } = await setup({ lang: 'he' });
    voice.toggle();
    assert.equal(instances.length, 1);
    const rec = instances[0];
    assert.equal(rec.lang, 'he-IL');
    assert.equal(rec.continuous, true);
    assert.equal(rec.interimResults, true);
    assert.equal(rec.started, 1);
    assert.equal(voice.isListening(), true);
    assert.equal(button.getAttribute('aria-pressed'), 'true');
    assert.deepEqual(states, [true]);
});

test('words appear as they are heard, after what was already typed, replacing the unsettled tail', async () => {
    const { voice, input, instances } = await setup({ value: 'When is' });
    const inputEvents = [];
    input.addEventListener('input', (e) => inputEvents.push(e.isTrusted));
    voice.start();
    const rec = instances[0];
    rec.say([['candle', false]]);
    assert.equal(input.value, 'When is candle');
    rec.say([['candle lighting', false]]);
    assert.equal(input.value, 'When is candle lighting');
    rec.say([['candle lighting', true], [' on Friday', false]]);
    assert.equal(input.value, 'When is candle lighting on Friday');
    // Every update tells the composer (resize, enable Send) -- as a script event.
    assert.equal(inputEvents.length, 3);
    assert.ok(inputEvents.every((trusted) => trusted === false));
});

test('toggling again stops the session and releases the button', async () => {
    const { voice, button, instances, states } = await setup();
    voice.toggle();
    voice.toggle();
    assert.equal(instances[0].stopped, 1);
    assert.equal(voice.isListening(), false);
    assert.equal(button.getAttribute('aria-pressed'), 'false');
    assert.deepEqual(states, [true, false]);
});

test('the engine ending on its own (silence, a time limit) releases the button', async () => {
    const { voice, button, instances } = await setup();
    voice.start();
    instances[0].onend();
    assert.equal(voice.isListening(), false);
    assert.equal(button.getAttribute('aria-pressed'), 'false');
    // A fresh press starts a fresh session.
    voice.start();
    assert.equal(instances.length, 2);
});

test('the visitor typing takes the box back: a real keystroke stops dictation, ours does not', async () => {
    const { voice, input, instances } = await setup();
    voice.start();
    instances[0].say([['hello', false]]); // our own synthetic input event
    assert.equal(voice.isListening(), true);
    input.dispatchEvent(new FakeEvent('input', { trusted: true }));
    assert.equal(voice.isListening(), false);
    assert.equal(instances[0].stopped, 1);
});

test('abort drops late results so a sent question cannot be retyped into the emptied box', async () => {
    const { voice, input, instances } = await setup();
    voice.start();
    const rec = instances[0];
    rec.say([['send me', true]]);
    assert.equal(input.value, 'send me');
    voice.abort();
    input.value = ''; // the composer clears the box on send
    assert.equal(rec.aborted, 1);
    assert.equal(rec.onresult, null);
    assert.equal(voice.isListening(), false);
    // And a stray result after the abort is not wired to anything.
    assert.equal(input.value, '');
});

test('errors are reported by kind; silence is not an error', async () => {
    const { voice, instances, errors } = await setup();
    voice.start();
    instances[0].onerror({ error: 'not-allowed' });
    instances[0].onerror({ error: 'no-speech' });
    instances[0].onerror({ error: 'network' });
    assert.deepEqual(errors, ['denied', 'network']);
});

test('a start() that throws (a session already running) leaves it idle', async () => {
    const mod = await load();
    class Throwing {
        start() { throw new Error('InvalidStateError'); }
        stop() {}
        abort() {}
    }
    const input = fakeInput();
    const button = fakeButton();
    const voice = mod.createVoiceInput({ input, button, win: { Event: FakeEvent, SpeechRecognition: Throwing } });
    voice.start();
    assert.equal(voice.isListening(), false);
    // Never marked pressed (the markup starts it at "false").
    assert.notEqual(button.getAttribute('aria-pressed'), 'true');
});

test('destroy ends the session and unhooks the button and the box', async () => {
    const { voice, input, button, instances } = await setup();
    voice.start();
    voice.destroy();
    assert.equal(instances[0].aborted, 1);
    assert.equal(button.listenerCount('click'), 0);
    assert.equal(input.listenerCount('input'), 0);
});
