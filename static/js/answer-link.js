// "Copy link" for a stored AI answer: in the #aiAssistantModal footer and at
// the bottom of the full-article view (#readerAnswerLink).
//
// The default link is the owner's private deep link, `/answer/<ask_history id>`
// (router.js `chat` key, hydrated by index.html's hydrateChatId): it opens the
// answer for its owner when signed in, and for nobody else. main.js plugs
// public links in through `getLinkUrl` (static/js/answer-share.js: POST
// .../share, then `/a/<share token>`), so the button, its "Copied!" flash
// and the manual-copy fallback stay as they are. `getLinkUrl` resolves to a
// URL string, or to `{ url, note }` when the copied link needs a visible
// caveat (e.g. a private fallback link). Only stored answers have an id --
// signed-out answers never do -- so the control stays hidden for them.
//
// The markup lives in templates/index.html so the icons can come from the
// phosphor() macro; this module only wires it. main.js installs one
// controller per placement (window.ShelahAnswerLink.modal / .article).

const COPIED_MS = 1600;

// ask_history ids are uuids; anything else is not ours to put in a URL.
const ANSWER_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function answerIdOf(data) {
    const id = String(data?.history_id || data?.id || "").trim();
    return ANSWER_ID_RE.test(id) ? id : null;
}

export function answerLinkUrl(id, origin) {
    return `${origin}/answer/${encodeURIComponent(id)}`;
}

// Clipboard API first; the hidden-textarea + execCommand path covers
// browsers/contexts without it (older Safari, non-secure dev origins).
// Resolves false instead of throwing, so the caller can fall back to
// showing the URL for a manual copy.
//
// `text` may be a Promise<string> (a share link still being created). Safari
// drops the click's user activation across an `await fetch`, so a plain
// writeText() afterwards is refused; handing clipboard.write() a
// ClipboardItem whose blob is still pending starts the write inside the
// click and lets the browser wait for the link.
export async function copyText(text, env = { navigator, document, ClipboardItem: globalThis.ClipboardItem, Blob: globalThis.Blob }) {
    const clipboard = env.navigator?.clipboard;
    if (typeof text !== "string" && clipboard?.write && env.ClipboardItem && env.Blob) {
        try {
            const blob = Promise.resolve(text).then((value) => {
                if (!value) throw new Error("no link");
                return new env.Blob([value], { type: "text/plain" });
            });
            await clipboard.write([new env.ClipboardItem({ "text/plain": blob })]);
            return true;
        } catch (_) {
            // Unsupported promise-valued items (older browsers) or no link:
            // retry below with the resolved string.
        }
    }
    try {
        text = await text;
    } catch (_) {
        return false;
    }
    if (!text) return false;
    try {
        if (clipboard?.writeText) {
            await clipboard.writeText(text);
            return true;
        }
    } catch (_) {
        // Permission denied or no user activation: try the legacy path.
    }
    const doc = env.document;
    if (!doc?.createElement || !doc.body) return false;
    const area = doc.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    doc.body.appendChild(area);
    try {
        area.select();
        return doc.execCommand?.("copy") === true;
    } catch (_) {
        return false;
    } finally {
        area.remove();
    }
}

function normalizeLink(value) {
    if (value && typeof value === "object") {
        return { url: String(value.url || ""), note: String(value.note || "") };
    }
    return { url: String(value || ""), note: "" };
}

// Wires one placement: `.answer-link-btn` (with an `--idle` and a `--copied`
// face stacked in the same grid cell, so the swap never changes its width),
// a `.answer-link-status` live region, and a read-only `.answer-link-fallback`
// input that shows the URL only when copying failed. `translate(en, he)`
// defaults to the classic script's t(), read per call so a language switch
// applies to the next message.
export function installAnswerLink(root, {
    getOrigin = () => window.location.origin,
    getLinkUrl = null,
    translate = (en, he) => (typeof window.t === "function" ? window.t(en, he) : en),
    copy = copyText,
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (handle) => clearTimeout(handle),
} = {}) {
    if (!root) return { show() {}, hide() {} };
    const button = root.querySelector(".answer-link-btn");
    const idleFace = root.querySelector(".answer-link-btn__face--idle");
    const copiedFace = root.querySelector(".answer-link-btn__face--copied");
    const status = root.querySelector(".answer-link-status");
    const fallback = root.querySelector(".answer-link-fallback");
    const linkFor = getLinkUrl || ((id) => answerLinkUrl(id, getOrigin()));
    let currentId = null;
    let copiedTimer = null;

    function setCopied(on) {
        if (copiedTimer !== null) clearTimer(copiedTimer);
        copiedTimer = null;
        button.dataset.state = on ? "copied" : "idle";
        idleFace?.setAttribute("aria-hidden", on ? "true" : "false");
        copiedFace?.setAttribute("aria-hidden", on ? "false" : "true");
        if (on) {
            copiedTimer = setTimer(() => {
                copiedTimer = null;
                setCopied(false);
            }, COPIED_MS);
        }
    }

    function reset() {
        setCopied(false);
        status.textContent = "";
        status.classList.remove("is-visible");
        fallback.value = "";
        fallback.classList.add("hidden");
        button.disabled = false;
    }

    button.addEventListener("click", async () => {
        const id = currentId;
        if (!id || button.disabled) return;
        button.disabled = true;
        // Started synchronously, inside the click, so copy() can begin the
        // clipboard write before the link exists (see copyText).
        const link = Promise.resolve().then(() => linkFor(id)).then(normalizeLink);
        const urlPromise = link.then((value) => value.url);
        urlPromise.catch(() => {});
        let copied = false;
        try {
            copied = await copy(urlPromise);
        } catch (_) {
            copied = false;
        }
        let url = "";
        let note = "";
        try {
            ({ url, note } = await link);
        } catch (_) {
            url = "";
        }
        copied = copied && Boolean(url);
        // The answer on screen may have changed while we waited.
        if (id !== currentId) return;
        button.disabled = false;
        if (copied) {
            fallback.classList.add("hidden");
            status.textContent = note || translate("Link copied", "הקישור הועתק");
            status.classList.toggle("is-visible", Boolean(note));
            setCopied(true);
            return;
        }
        if (!url) {
            status.textContent = translate("Couldn't create a link. Try again.", "לא ניתן היה ליצור קישור. נסה/י שוב.");
            status.classList.add("is-visible");
            return;
        }
        fallback.value = url;
        fallback.classList.remove("hidden");
        fallback.focus();
        fallback.select();
        status.textContent = translate("Couldn't copy automatically. Copy the link here.", "לא ניתן היה להעתיק אוטומטית. העתק/י את הקישור כאן.");
        status.classList.add("is-visible");
    });

    fallback.addEventListener("focus", () => fallback.select());

    return {
        show(data) {
            currentId = answerIdOf(data);
            reset();
            root.classList.toggle("hidden", !currentId);
        },
        hide() {
            currentId = null;
            reset();
            root.classList.add("hidden");
        },
    };
}
