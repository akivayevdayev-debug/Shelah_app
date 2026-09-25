// Which "Ask" surface a question opens (decision A1). Owns no DOM: the UI
// hookup calls chooseAskSurface() and renders the result.
//   - signed in  -> "conversation": the multi-turn conversation UI
//     (conversation-store.js). /api/conversations/* needs a Clerk user, so
//     this is the only state in which that UI can save anything.
//   - signed out -> "legacy": the existing single-answer AI modal
//     (aiAssistantModal / handleAiSearch in templates/index.html), shown with
//     SIGN_IN_HINT so the user knows signing in keeps their conversations.

export const ASK_SURFACE = Object.freeze({
    CONVERSATION: "conversation",
    LEGACY: "legacy",
});

// Hint for the legacy modal when the user is signed out.
export const SIGN_IN_HINT = "Sign in to save your conversations.";

// Same test templates/index.html uses before its signed-in-only fetches
// (loadPrefsFromServerIfSignedIn, the ask-history loaders): Clerk configured
// for this deployment, plus a live session AND a user. Without a publishable
// key there is no auth at all (e.g. local dev), so that reads as signed out.
export function isSignedIn() {
    const clerkConfigured = Boolean(window.APP_CLERK && window.APP_CLERK.publishableKey);
    return clerkConfigured && Boolean(window.Clerk?.session) && Boolean(window.Clerk?.user);
}

// `signedIn` defaults to the live Clerk state; pass it explicitly when the
// caller already knows it (e.g. inside a Clerk listener).
export function chooseAskSurface({ signedIn = isSignedIn() } = {}) {
    return signedIn ? ASK_SURFACE.CONVERSATION : ASK_SURFACE.LEGACY;
}

window.ShelahConversationEntry = { isSignedIn, chooseAskSurface, ASK_SURFACE, SIGN_IN_HINT };
