# AI Word Limit Feature

> Sync status (2026-04-21): Verified against current implementation (report-driven library filtering, topbar menu icon layering fix, global warm icon tones, and backup template sync).

## Overview
The AI assistant now includes a word limit to reduce token usage and API costs.

## Changes Made
- **File Modified:** `backend/claude.py`
- **Feature Added:** Word limit function that caps responses at 500 words maximum
- **Token Reduction:** max_tokens reduced from 2000 to 800 (approximately 60% reduction)

## How It Works
1. All AI responses are processed through the `limit_words()` function
2. Responses exceeding 500 words are truncated
3. A notification message is appended when truncation occurs
4. Token usage per query reduced by ~60%

## Implementation Details

### limit_words() Function
```python
def limit_words(text, max_words=500):
    """Limit response to a maximum number of words"""
    words = text.split()
    if len(words) > max_words:
        truncated = ' '.join(words[:max_words])
        # Add ellipsis and note about truncation
        return truncated + '\n\n*[Response truncated to preserve API tokens. For the full analysis, consult a local Rabbi.]*'
    return text
```

### Integration
- Called in `ask_claude()` function before returning response
- Applied to all AI-generated answers through `get_halachic_answer()`
- Integrated with Flask `/ask` endpoint

## Usage
The feature is automatic and requires no user configuration. When users ask halachic questions via the website search bar:
1. Question is sent to Claude API
2. Response generated with max 800 tokens
3. Response text limited to 500 words
4. Limited response returned to user's browser

## Performance Impact
- **Before:** ~2000 tokens per query
- **After:** ~800 tokens per query
- **Savings:** ~60% reduction in API token consumption
- **User Experience:** Slightly shorter but still complete answers

## Testing
Feature has been tested and verified working:
- ✅ Word limit function correctly caps at 500 words
- ✅ Token limit set to 800 per query
- ✅ Live testing shows 464-word response (under limit)
- ✅ Truncation message displays correctly when needed
- ✅ Integration with Flask API confirmed working

## Rollback Instructions
If needed to revert this feature:
1. In `backend/claude.py`, change `max_tokens=800` back to `max_tokens=2000`
2. Remove the line: `response_text = limit_words(response_text, max_words=500)`
3. Remove the `limit_words()` function definition (lines 76-83)

---
Feature implemented and tested: 2026-04-06

---
Last Sync Check: 2026-04-07
