"""
Anthropic/Claude prompt and response helper for Sh'elah.

Responsibilities:
- Format source/custom/wiki payloads into prompt-ready text blocks.
- Build the structured prompt used for halachic responses.
- Call Anthropic when configured, with safe fallback behavior when SDK/key is absent.

This module is intentionally stateless: app.py and data_service.py prepare context,
then this file focuses on LLM formatting and call execution.
"""

import os
from typing import List, Dict

try:
    import anthropic
except Exception:  # pragma: no cover - graceful fallback when SDK is unavailable
    anthropic = None

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(
    api_key=API_KEY) if anthropic and API_KEY else None


SYSTEM_PROMPT = """
You are a halakhic source synthesizer.

Output style:
- Direct and fact-focused.
- No greetings, no conversational filler, no motivational language.
- Keep claims tied to explicit provided evidence.

Source hierarchy (strict):
1) Primary: Sefaria API and whitelisted external sources only: HebrewBooks, Halachipedia, Yeshivat Har Bracha (YHB).
2) Secondary: Local customs JSON data from the customs directory.

Conflict handling:
- If a local custom contradicts a primary Sefaria source, explicitly add a "Conflict Flag" section naming both positions.

No-hallucination rule:
- If the prompt data does not contain a verified source in the allowed domains or local customs, return exactly: "No verified source found".
- Do not invent citations or books.

Math and measurements:
- Use LaTeX for shiurim, quantities, and mathematical logic (example format: $k = 27$).
""".strip()


def format_sefaria_sources(sources):
    """Format Sefaria sources into readable text"""
    output = ""
    for s in sources:
        text = s.get("text", "")[:500]
        ref = s.get("ref", "")
        output += f"\n--- {ref} ---\n{text}\n"
    return output


def format_customs(customs):
    """Format customs into readable text"""
    output = ""
    for c in customs:
        community = c.get("community", "")
        ruling = c.get("ruling", "")
        output += f"\n[{community}] {ruling}\n"
    return output


def format_wiki(wiki):
    """Format Wikipedia results"""
    if not wiki:
        return ""
    output = ""
    for w in wiki:
        title = w.get("title", "")
        summary = w.get("summary", "")
        output += f"\n[{title}] {summary}\n"
    return output


def build_prompt(question, sefaria_sources, customs, wiki, halachipedia=None, mode="balanced", community_lens="All"):
    """Build structured prompt for Claude"""

    sefaria_text = format_sefaria_sources(sefaria_sources)
    customs_text = format_customs(customs)
    halachic_text = format_wiki(halachipedia) if halachipedia else ""

    prompt = f"""
QUESTION:
{question}

PRIMARY SOURCES (SEFARIA + WHITELISTED EXTERNAL CONTEXT):
{sefaria_text}
{halachic_text}

SECONDARY SOURCES (LOCAL CUSTOMS JSON):
{customs_text}

INSTRUCTIONS:
1. Response mode requested: {mode}
2. Community lens requested: {community_lens}
3. If mode is strict, do not include unsupported claims.
4. Keep source ordering aligned with the hierarchy above.
"""

    return prompt


def limit_words(text, max_words=500):
    """Limit response to a maximum number of words"""
    words = text.split()
    if len(words) > max_words:
        truncated = ' '.join(words[:max_words])
        # Add ellipsis and note about truncation
        return truncated + '\n\n*[Response truncated to preserve API tokens. For the full analysis, consult a local Rabbi.]*'
    return text


def ask_claude(prompt):
    """Send prompt to Claude API with word limit"""
    if client is None:
        return {"answer": "AI provider is currently unavailable.", "confidence": 0, "error": "unavailable", "is_fallback": True}

    try:
        message = client.messages.create(
            model="claude-haiku-4-5",
            system=SYSTEM_PROMPT,
            max_tokens=800,  # Reduced from 2000 to ~500-600 words
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        response_text = message.content[0].text
        # Apply additional word limit as safety measure
        response_text = limit_words(response_text, max_words=500)
        return {"answer": response_text, "confidence": 0.78, "is_fallback": False}
    except Exception as e:
        return {"answer": "AI provider is currently unavailable.", "confidence": 0, "error": str(e), "is_fallback": True}


def build_fallback_answer(question: str, sefaria_sources: List[Dict], customs: List[Dict], wiki: List[Dict], halachipedia: List[Dict], mode: str = "balanced", community_lens: str = "All"):
    """Build a useful deterministic answer when Claude is unavailable."""
    source_refs = [s.get("ref", "")
                   for s in sefaria_sources if s.get("ref")][:3]
    customs_snippets = [c.get("ruling", "")
                        for c in customs if c.get("ruling")][:2]
    context_snippets = [w.get("summary", "") for w in (
        halachipedia or []) + (wiki or []) if w and w.get("summary")][:2]

    answer_lines = [
        "**AI synthesis is temporarily unavailable**, so here is a source-based fallback summary.",
        "",
        f"Question: {question}",
        f"Mode: {mode}",
        f"Community lens: {community_lens}",
        "",
    ]

    if mode in ("sources", "strict"):
        answer_lines.append("### Primary Sources")
    elif mode == "practical":
        answer_lines.append("### Practical Notes")
    else:
        answer_lines.append("### Key Points")

    if mode in ("sources", "balanced", "strict"):
        if source_refs:
            for ref in source_refs:
                answer_lines.append(f"- {ref}")
        else:
            answer_lines.append("- No primary source references were matched.")

    if mode == "strict" and not source_refs:
        answer_lines.append(
            "- Strict sources mode prevented an inferred ruling without explicit mekorot.")

    if mode == "practical":
        answer_lines.append(
            "- Start with the practical details below and verify with your local rabbi.")

    answer_lines.append("")
    answer_lines.append("### Practical Notes")
    if customs_snippets:
        for note in customs_snippets:
            answer_lines.append(f"- {note}")
    else:
        answer_lines.append(
            "- No community-specific custom was found for this query.")

    if context_snippets:
        answer_lines.append("")
        answer_lines.append("### Background")
        for snippet in context_snippets:
            answer_lines.append(f"- {snippet[:220]}...")

    answer_lines.extend([
        "",
        "Please consult a qualified Orthodox Rabbi for a practical psak."
    ])

    return "\n".join(answer_lines)


def get_halachic_answer(question, sefaria_sources, customs, wiki=None, halachipedia=None, mode="balanced", community_lens="All"):
    """Main function to get answer"""
    if mode == "strict" and not sefaria_sources:
        return {
            "answer": (
                "Strict Sources Mode could not generate a ruling because no direct primary sources were found. "
                "Please provide a specific textual reference."
            ),
            "confidence": 0.2,
            "is_fallback": True,
        }

    prompt = build_prompt(question, sefaria_sources,
                          customs, wiki or [], halachipedia or [], mode=mode, community_lens=community_lens)
    result = ask_claude(prompt)
    if result.get("error"):
        fallback = build_fallback_answer(
            question,
            sefaria_sources,
            customs,
            wiki or [],
            halachipedia or [],
            mode=mode,
            community_lens=community_lens,
        )
        return {"answer": fallback, "confidence": 0.35, "is_fallback": True}
    return result
