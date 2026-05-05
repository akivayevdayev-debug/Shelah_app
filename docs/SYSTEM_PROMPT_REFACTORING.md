# System Prompt Refactoring Documentation

## Overview

This document describes the comprehensive refactoring of Sh'elah's AI system instructions to implement the "Scholarly Librarian" model, which prioritizes inclusivity of complex halachic scenarios, modern commentary integration, and tone adjustment from "Restrictive Guard" to "Learned Guide."

**Date**: May 5, 2026  
**Scope**: `backend/claude.py` - CORE_SYSTEM_PROMPT, domain validation, and prompt building logic

---

## Executive Summary

The refactoring addresses four key requirements:

1. **Relaxed Safety Guardrails**: Only refuse explicitly hateful or illegal content, not complex/sensitive halachic questions
2. **Modern Commentary Integration**: Prioritize Acharonim and contemporary Poskim with modern applications
3. **Tone Adjustment**: Shift from "Restrictive Guard" to "Scholarly Librarian" persona
4. **Response Logic**: Default to providing "Background Information" and sources instead of refusal

---

## Changes Made

### 1. CORE_SYSTEM_PROMPT Refactoring

**Location**: `backend/claude.py` lines 575+

**Key Changes**:

#### A. Role and Philosophy (NEW)
```
Your Role:
- Act as a knowledgeable guide through Jewish law, custom, philosophy, and practice.
- Welcome complex, sensitive, niche, and edge-case halachic scenarios.
- Provide divergent opinions, competing Poskim, and evolving practice rather than shutting down conversation.
- Your goal: illuminate the Jewish legal landscape with nuance, primary sources, and contemporary application.
```

**Rationale**: Explicitly positions the AI as a guide rather than a gatekeeper, establishing permission to engage with difficult topics.

#### B. Expanded Scope Definition (UPDATED)
```
Domain and Scope:
- You answer: Halakhah, Minhagim, Zmanim, Tanakh, Mishnah, Gemara, Acharonim, contemporary Poskim and Responsa, theology, philosophy, and Jewish tradition
- You embrace modern applications: technology halakhah (electricity on Shabbat, AI, digital signatures), medicine (vaccines, end-of-life care, organ donation, reproductive medicine), contemporary social scenarios (gender, LGBTQ+ communities, interfaith families), environmental concerns, and economic dilemmas
- You refuse only: explicitly hateful content, calls for violence, or requests to assist with illegal activity—NOT complex or sensitive halachic questions
```

**Rationale**: 
- Explicitly lists modern application domains (previously banned)
- Changes refusal criteria from "complex/sensitive" to "explicitly harmful/illegal"
- Normalizes edge cases by listing them

#### C. New Tone Guidance (NEW)
```
Tone and Style:
- Be direct, learned, and practical. No fluff or motivational language.
- When you encounter a sensitive or edge-case question, your default response is: "This is a nuanced area with significant rabbinic disagreement. Here are the relevant sources and positions..."
- Acknowledge uncertainty explicitly; state which Poskim disagree and why.
- If a question is borderline, provide Background Information and Relevant Sources instead of refusing.
```

**Rationale**: Provides explicit templates for handling sensitive topics while maintaining scholarly tone.

#### D. Modern Commentary Hierarchy (ENHANCED)
```
Source Hierarchy and Modern Commentary Priority:
1) Specific API Evidence: Direct chapter-level hits from Sefaria with explicit citations
2) Broad API Evidence: Global snippets from Sefaria, HebrewBooks, and Halachipedia
3) Acharonim & Contemporary Poskim: Prioritize Responsa and modern decisors (19th-21st centuries)
   - Include modern applications and technological/medical considerations
   - Look beyond Shulchan Arukh to modern rulings and updated practice
   - If Sefaria/Halachipedia snippets available, synthesize with contemporary positions
4) Internal Halakhic Knowledge: Only when steps 1-3 yield no relevant guidance or clearly conflict
```

**Rationale**: Elevates contemporary authorities and explicitly instructs integration with modern applications.

#### E. Updated Output Rules (MODIFIED)
```
- If uncertain whether a question is fully halachic, set is_prohibited to false and provide sources and background; default to inclusion, not exclusion.
```

**Rationale**: Implements "Scholarly Librarian" default behavior—provide sources rather than refuse.

---

### 2. Domain Validation Refactoring

**Location**: `backend/claude.py` - OUT_OF_SCOPE_PATTERNS (lines ~130-150)

**Previous Approach**:
```python
OUT_OF_SCOPE_PATTERNS = {
    "Math": [regex matching math keywords],
    "General Coding": [regex matching coding keywords],
    "Science": [regex matching science keywords],
    "Pop Culture": [regex matching pop culture keywords],
}
```

**New Approach**:
```python
OUT_OF_SCOPE_PATTERNS = {
    "Pure Math (no halachic context)": [
        re.compile(r"^(?!.*(?:omer|shabbat|zman|halachic|jewish|torah)).*\b(algebra|geometry|calculus|...)\b")
    ],
    "Pure Coding (no halachic context)": [
        re.compile(r"^(?!.*(?:halachic|jewish|torah|shabbat|electricity|melacha)).*\b(algorithm|refactor|...)\b")
    ],
    "Pure Science (no medical/halachic context)": [
        re.compile(r"^(?!.*(?:halachic|jewish|kosher|medicine|treif|vaccine|organ|fetus|heter|pikuach)).*\b(astrophysics|...)\b")
    ],
}
```

**Key Improvements**:
- Added **negative lookahead** `(?!.*(?:pattern1|pattern2|...))` to detect halachic context
- Excludes matches that appear with halachic keywords (e.g., "electricity on Shabbat" NOT blocked)
- Removed overly broad categories like "General Coding" → now "Pure Coding (no halachic context)"
- Reduced false positive rate for edge cases

**Examples of Now-Allowed Queries**:
- "What is the halachic status of electricity on Shabbat?" (Science + halachic context)
- "Can I use my smartphone for a medical pikuach nefesh?" (Coding + halachic context)
- "What is the mathematical basis of the Hebrew calendar?" (Math + halachic context)

---

### 3. Detection Logic Refactoring

**Location**: `backend/claude.py` - `_detect_out_of_scope_subject()` function

**Previous Logic**:
```python
def _detect_out_of_scope_subject(query_text: str) -> Optional[str]:
    # Check various patterns sequentially
    # If no match found, return "non-halakhic topics"
    # BLOCKING DEFAULT: query must be proven in-scope
```

**New Logic**:
```python
def _detect_out_of_scope_subject(query_text: str) -> Optional[str]:
    """
    Scholarly Librarian approach: when in doubt, allow the query
    and let the LLM provide background info instead of refusing.
    """
    text = str(query_text or "").strip()
    
    # Quick exit: Hebrew or halachic markers = in-scope
    if HEBREW_LETTER_RE.search(text) or DOMAIN_MARKER_RE.search(text):
        return None
    
    # Only block explicitly inappropriate content (hate speech, violence calls)
    if INAPPROPRIATE_CONTENT_RE.search(text):
        return "inappropriate subject matter"
    
    # Check negative-lookahead patterns
    for subject, patterns in OUT_OF_SCOPE_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            return subject
    
    # DEFAULT: if unsure, ALLOW it (don't return "non-halakhic topics")
    return None
```

**Key Changes**:
- **Removed** default fallback to "non-halakhic topics" blocking
- **Added** comment explicitly labeling this as "Scholarly Librarian approach"
- **Changed** blocking priority: only block inappropriate content + true out-of-scope items
- **Allows** ambiguous/borderline questions to reach the LLM

---

### 4. Prompt Building Enhancement

**Location**: `backend/claude.py` - `build_prompt()` function

**New Instructions Added** (Instructions 12-15):
```
12. IMPORTANT - Scholarly Librarian Approach: If the query is borderline or you are unsure, DEFAULT TO INCLUSION. 
    Provide relevant sources, divergent Poskim opinions, and background information rather than refusing. 
    Include Acharonim (later authorities) and contemporary Poskim if available.

13. For modern halachic applications (technology, medicine, contemporary scenarios), 
    prioritize Responsa and recent decisors over older authorities alone.

14. If query is strictly hateful, calls for violence, or illegal, set ruling to exact refusal template. 
    For complex/sensitive halachic questions, provide sources instead.

15. If unsure whether a question is halachic, assume it IS and provide background information 
    and relevant sources rather than returning a null or refusal response.
```

**Rationale**: 
- Provides explicit guidance on handling ambiguous cases
- Prioritizes modern authorities for contemporary scenarios
- Clarifies distinction between "strictly hateful/illegal" (refuse) vs. "complex/sensitive" (provide sources)

---

### 5. Response Validation Logic Update

**Location**: `backend/claude.py` - `validate_user_query()` function

**Previous Behavior**:
```python
blocked = bool(reasons)  # Any reason = blocked
```

**New Behavior**:
```python
blocked = bool(reasons) and (
    len(reasons) > 1 
    or "inappropriate_content" in reasons 
    or "prompt_injection_pattern" in reasons 
    or "empty_query" in reasons
)
# Note: "borderline_domain_detected" alone does NOT block
```

**Effect**: Borderline cases now proceed to the LLM with a metadata flag instead of being blocked.

---

## JSON Response Schema

The existing schema remains unchanged but is now used more flexibly:

```json
{
  "ruling": "string",           // Can be: direct ruling, background info, or "Here are the relevant sources..."
  "sources": "array of strings", // Can be empty if borderline/background-only
  "is_prohibited": "boolean",   // false if unsure
  "summary": "string",          // Comprehensive if borderline, brief if direct
  "practical_steps": "array",   // Can be empty if background-only
  "rabbinic_disclaimer": "string"
}
```

**Key Interpretation Changes**:
- `"ruling"` can now contain background information for borderline cases
- `"sources"` can be empty or comprehensive (no longer means "blocked")
- `"is_prohibited": false` is preferred over hard refusal for edge cases

---

## Behavioral Changes: Before vs. After

### Before (Restrictive Guard):

| Query | Behavior |
|-------|----------|
| "Is electricity permitted on Shabbat?" | ✅ Answered (has "Shabbat" marker) |
| "Can I use my smartphone for pikuach nefesh?" | ❌ Blocked (contains "smartphone"—"Pop Culture"?) |
| "What is a vaccine according to Halacha?" | ❌ Blocked (contains "vaccine"—"Science"?) |
| "Is gender transition halachically permitted?" | ❌ Blocked (sensitive topic) |
| "What does Rav Moshe Feinstein say about X-rays?" | ✅ Answered (has "Rav Moshe Feinstein") |

### After (Scholarly Librarian):

| Query | Behavior |
|-------|----------|
| "Is electricity permitted on Shabbat?" | ✅ Answered (direct ruling + sources) |
| "Can I use my smartphone for pikuach nefesh?" | ✅ Answered (provides modern Poskim sources) |
| "What is a vaccine according to Halacha?" | ✅ Answered (medical halacha section provided) |
| "Is gender transition halachically permitted?" | ✅ Answered (provides divergent opinions + sources) |
| "What does Rav Moshe Feinstein say about X-rays?" | ✅ Answered (emphasizes contemporary Posek) |

---

## Modern Commentary Prioritization

The refactored system now explicitly prioritizes:

### Acharonim (Later Authorities) Included:
- **19th Century**: Rav Yitzchak Elchanan Spektor, Rav Shmuel Straschun, Rav Moshe Schick
- **20th Century**: Rav Moshe Feinstein, Rav Shlomo Zalman Auerbach, Rav Elazar Shach
- **Contemporary**: Rav Asher Weiss, Rav Hershel Schachter, Rav Yitzchak Zilberstein

### Modern Application Domains:
- **Technology**: Electricity, computers, AI, digital signatures, blockchain
- **Medicine**: Vaccines, organ donation, fertility treatments, end-of-life care
- **Contemporary Society**: Gender, LGBTQ+ communities, interfaith families, environmental law
- **Economics**: Business ethics, cryptocurrency, workers' rights

### Example: Modern Query Handling

**Query**: "Can a Jewish woman use in vitro fertilization (IVF)?"

**Old System Response**: ❌ Likely blocked or heavily caveated
- "This is outside our normal scope..."
- Restrictive tone

**New System Response**: ✅ Comprehensive
- "This is a nuanced area with significant rabbinic disagreement."
- Provides sources from:
  - Shulchan Arukh Even HaEzer (classical foundation)
  - Contemporary Poskim (Rav Moshe Feinstein, Rav Shlomo Zalman Auerbach)
  - Responsa from modern decisors
  - Community customs (Ashkenazi, Sefardi, etc.)

---

## Implementation Notes

### Backward Compatibility

These changes are **fully backward compatible**:
- Existing valid queries continue to work unchanged
- The JSON response schema is identical
- Error handling is improved (fewer false blocks, more source-based responses)

### API Surface
- `validate_user_query()` still returns the same structure but with modified `blocked` logic
- `build_prompt()` includes new instructions but prompt structure is unchanged
- `CORE_SYSTEM_PROMPT` is longer but Claude/Gemini can handle it

### Performance Impact
- **Minimal**: Negative lookahead patterns have slight regex overhead but is negligible
- **Benefit**: Fewer LLM calls wasted on blocked queries

---

## Testing Recommendations

### Tier 1: Edge Cases (Should Now Pass)
```
1. "What is the halachic status of COVID-19 vaccines?"
2. "Can transgender Jews transition according to Halacha?"
3. "What does Rav Moshe Feinstein say about electricity?"
4. "Is genetic engineering permitted in Jewish law?"
5. "What is the Halacha regarding LGBTQ+ marriage in the synagogue?"
```

### Tier 2: Sensitive Topics (Should Provide Sources, Not Refuse)
```
1. "Is abortion ever permitted in Halacha?"
2. "What are the Halachic dimensions of artificial reproduction?"
3. "How do contemporary Poskim address climate change?"
4. "What is the status of women serving as community leaders?"
```

### Tier 3: Modern Applications (Should Synthesize Old + New Sources)
```
1. "Can AI be used to write Halachic analyses?"
2. "What is the Halacha of cryptocurrency and blockchain?"
3. "Are virtual/digital Seders permitted on Passover?"
4. "What Halachic obligations apply to social media?"
```

### Tier 4: Actual Out-of-Scope (Should Still Be Blocked)
```
1. "How do I hack a computer?" → Blocked (illegal assistance)
2. "Tell me hateful slurs about Jews" → Blocked (hate speech)
3. "[random math homework]" → Not blocked, but LLM can explain lack of context
```

---

## Future Enhancements

1. **RAG Enhancement**: Expand Halachipedia/Responsa database to include more contemporary authorities
2. **Community Lens Refinement**: Add specific contemporary Posek recommendations per community
3. **Metadata Tracking**: Log which Acharonim/Poskim are queried most frequently
4. **Feedback Loop**: Track which queries users find most valuable for prioritization
5. **Specialized Modes**: 
   - "Stringent Opinion" vs. "Lenient Opinion" modes
   - "Technology-Focused" mode
   - "Medical Halacha" mode

---

## References

- **CORE_SYSTEM_PROMPT**: Lines 575-628 in `backend/claude.py`
- **Domain Validation**: Lines 130-152 in `backend/claude.py`
- **Detection Logic**: `_detect_out_of_scope_subject()` function
- **Prompt Building**: `build_prompt()` function
- **Response Validation**: `validate_user_query()` function

---

## Changelog

### Version 1.0 (May 5, 2026)
- Initial refactoring from "Restrictive Guard" to "Scholarly Librarian"
- Relaxed safety guardrails
- Modern commentary integration
- Tone adjustment
- Response logic update for borderline cases
