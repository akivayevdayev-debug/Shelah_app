// "Share" for a whole conversation: the header button in the conversation
// panel (conversation-ui.js).
//
// It mints (or re-uses) the conversation's public link, `/a/<token>`
// (backend/routes_conversation_share.py), and copies it. The link shows the
// chat as it was when it was shared: pressing Share again after more turns
// keeps the same token and moves the snapshot forward, so a link already sent
// shows the newer turns once the owner chooses to. Stopping kills the link;
// sharing again mints a new one. The state store and the API client are
// answer-share.js's, with this kind's base path.

import { copyText } from "./answer-link.js";
import {
    CONVERSATION_SHARE_PATH,
    NothingToShareError,
    createAnswerShare,
    createShareApi,
} from "./answer-share.js";

// `copy` is injectable for tests. `publish` resolves
// { status: "copied" | "manual" | "empty" | "failed", url? }: "manual" means
// the link exists but the clipboard refused it, so the caller shows the URL.
export function createConversationShare({
    share = createAnswerShare({
        api: createShareApi({ basePath: CONVERSATION_SHARE_PATH }),
        privateFallback: false,
        refreshOnShare: true,
    }),
    copy = copyText,
} = {}) {
    async function publish(id) {
        // Started synchronously, inside the click, so copy() can begin the
        // clipboard write before the link exists (see copyText).
        const link = Promise.resolve().then(() => share.linkFor(id));
        link.catch(() => {});
        let copied = false;
        try {
            copied = await copy(link);
        } catch (_) {
            copied = false;
        }
        try {
            const url = await link;
            return { status: copied ? "copied" : "manual", url };
        } catch (err) {
            return { status: err instanceof NothingToShareError ? "empty" : "failed" };
        }
    }

    async function stop(id) {
        try {
            await share.revoke(id);
            return true;
        } catch (_) {
            return false;
        }
    }

    return {
        publish,
        stop,
        load: share.load,
        peek: share.peek,
        subscribe: share.subscribe,
    };
}
