// Dictation for the conversation composer: a microphone button that types what
// the visitor says into the question box, using the browser's own speech
// recognition (the Web Speech API: SpeechRecognition, or webkitSpeechRecognition
// in Chrome and Safari).
//
// The recognition itself is the browser's: Sh'elah never receives audio, and
// nothing here is sent anywhere. What the browser does with the audio is its
// vendor's business (Chrome, for one, streams it to Google to be transcribed),
// which is why the first use says so (conversation-ui.js). Firefox has no
// SpeechRecognition, so there the button never appears.
//
// The words land in the box as they are heard -- the still-uncertain tail is
// replaced as the engine settles on it -- after whatever was already typed, and
// the box fires `input` so the composer resizes and Send enables. The visitor
// ends it with the button, by typing, or by sending; the engine also ends it on
// its own after a stretch of silence.

// "he" -> Hebrew (Israel), anything else -> US English: the two languages the
// interface is offered in.
export function voiceLang(uiLang) {
    return uiLang === "he" ? "he-IL" : "en-US";
}

export function speechRecognitionCtor(win = globalThis) {
    return win?.SpeechRecognition || win?.webkitSpeechRecognition || null;
}

// The browser's error codes, folded to the few the interface tells apart.
const ERROR_KINDS = {
    "not-allowed": "denied",
    "service-not-allowed": "denied",
    "audio-capture": "no-mic",
    "language-not-supported": "language",
    network: "network",
};

export function errorKind(code) {
    return ERROR_KINDS[code] || null;
}

// Joins dictated text onto what was typed: one space between them, none when
// the box was empty or already ends in whitespace.
export function joinSpoken(base, spoken) {
    if (!spoken) return base;
    if (!base || /\s$/.test(base)) return base + spoken;
    return `${base} ${spoken}`;
}

// `input` is the textarea, `button` the mic. `getLang()` returns the interface
// language when listening starts. `onState(listening)` and `onError(kind)` let
// the page react; `win` is injectable for tests.
export function createVoiceInput({ input, button, getLang = () => "en", onState, onError, win = globalThis } = {}) {
    const Ctor = speechRecognitionCtor(win);
    if (!input || !button || !Ctor) return null;

    let recognition = null;
    let base = "";
    let listening = false;

    function setListening(next) {
        if (listening === next) return;
        listening = next;
        button.setAttribute("aria-pressed", next ? "true" : "false");
        onState?.(next);
    }

    function write(value) {
        input.value = value;
        // A synthetic event: the composer's own handlers (autosize, Send) run,
        // and onTyped() below can tell it from the visitor's keystroke.
        input.dispatchEvent(new win.Event("input", { bubbles: true }));
    }

    function onResult(event) {
        let spoken = "";
        for (let i = 0; i < event.results.length; i += 1) {
            spoken += event.results[i][0]?.transcript ?? "";
        }
        write(joinSpoken(base, spoken.trim() ? spoken.replace(/^\s+/, "") : ""));
    }

    function onTyped(event) {
        // The visitor typing (a trusted event) takes the box back.
        if (listening && event.isTrusted) stop();
    }

    function start() {
        if (listening) return;
        recognition = new Ctor();
        recognition.lang = voiceLang(getLang());
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;
        base = input.value;
        recognition.onresult = onResult;
        recognition.onerror = (event) => {
            const kind = errorKind(event?.error);
            if (kind) onError?.(kind);
        };
        recognition.onend = () => {
            recognition = null;
            setListening(false);
        };
        try {
            recognition.start();
            setListening(true);
        } catch (_err) {
            // start() throws if a session is already running.
            recognition = null;
            setListening(false);
        }
    }

    function stop() {
        const running = recognition;
        if (!running) {
            setListening(false);
            return;
        }
        try {
            running.stop();
        } catch (_err) {
            // Already ended.
        }
        setListening(false);
    }

    // Ends the session and drops anything still in flight: for when the box
    // has been sent or the panel closed, so a late result cannot type into it.
    function abort() {
        const running = recognition;
        recognition = null;
        if (running) {
            running.onresult = null;
            running.onerror = null;
            running.onend = null;
            try {
                running.abort();
            } catch (_err) {
                // Already ended.
            }
        }
        setListening(false);
    }

    function toggle() {
        if (listening) stop();
        else start();
    }

    button.addEventListener("click", toggle);
    input.addEventListener("input", onTyped);

    return {
        start,
        stop,
        abort,
        toggle,
        isListening: () => listening,
        destroy() {
            abort();
            button.removeEventListener("click", toggle);
            input.removeEventListener("input", onTyped);
        },
    };
}
