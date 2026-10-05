# Customs data notes

Community minhag data: thirteen community files, a JSON Schema, and one retired legacy aggregate.

## Files

| File | What it is |
|---|---|
| `ashkenaz.json`, `bukharian.json`, `ethiopian.json`, `georgian.json`, `greek-romaniote.json`, `iraqi.json`, `moroccan.json`, `mountain-jewish-kavkazi.json`, `persian.json`, `sefardic.json`, `syrian.json`, `turkish-ottoman-sefardic.json`, `yemenite.json` | One structured file per community, format `3.0` |
| `schema.json` | JSON Schema for the 3.0 shape. Required: `version`, `heritage_id`, `name`, `halacha_index` (and `runtime.lens_key`, see below) |
| `customs_db.json` | The legacy flat aggregate (`Kafkazi`, `Bukharian`, `Sephardic`, `Ashkenazic`). The loader skips it: it is retired from community and customs browsing and kept only as history |

## Format 3.0

Each community file carries these blocks (see `schema.json` for the exact shapes):

| Block | Purpose |
|---|---|
| `version`, `type`, `heritage_id`, `name`, `aliases`, `database_scope` | Identity of the file. `name` is also what the community page and API use, so renaming it renames the community. |
| `runtime` | Values the code reads instead of hard-coding them (next section). |
| `identity`, `languages`, `subgroups`, `historical_background`, `core_halachic_authorities` | Background shown on the community page. `identity` is `{primary_origin, secondary_centers}` and `languages` lists the languages the research names; both come from the research reports. `languages` is left out where the research is silent (Ashkenaz). The old unverified `genealogy` block is gone. |
| `halacha_index` | One entry per topic: `category`, `topic`, `topic_he`, `summary`, `summary_he`, plus optional `common_practices`, `variants` (one position each), `source`, `source_url`, `references`, `confidence`, `notes` and `review_notes`. `(category, topic)` is unique within a file. |
| `unique_minhagim` | Distinctive customs of this community, each with `name`, `name_he`, `description`, `description_he`, and optional `when`, `source`, `source_url`, `references`, `confidence`. |
| `source_registry` | A list of the works the file leans on (`title`, `author`, `type`, `url`, `used_for`). The community page shows these as "Major Authorities". |
| `gaps`, `disputes` | Topics with no community-specific source, and points where sources conflict. Both are listed rather than padded with generic text, and both are shown to users. |
| `needs_rabbinic_review` | Reviewer-only worklist. |

`confidence` on an entry is one of `well-attested` (several independent sources agree), `disputed` (rabbis or sources conflict), `regional` (limited to a place or subgroup) or `needs-review` (thin or single-source). The last three show a chip and a line under the entry telling the reader to ask their own rabbi (`static/js/community-labels.js`, `renderCommunityContent` in `templates/index.html`), and the stored text carries the same instruction right after the summary, so the AI passes it on (`_CONFIDENCE_NOTES` in the migration script).

**Both sides of a machloket.** A `disputed` entry lists every position as its own `variants` item (a guard test needs at least two), because every rabbi gives their own answer and the page must not pick one. The page never rules; it shows the positions and sends the reader to their rabbi.

**Hebrew.** `topic_he` and `summary_he` (entries) and `name_he` and `description_he` (distinctive customs) are hand-written Hebrew for each item, shown when the UI is in Hebrew (`/api/community/<name>` returns them as `topic_he`, `ruling_he`, `name_he` and `description_he`). A guard test (`TestRealCustomsFiles`) fails if any is missing. Variants, common practices and notes stay English. Do not machine-translate: write the Hebrew from the English entry and keep the facts identical.

**Reviewer-only fields never reach users or the model.** `review_notes` (on an entry) and `needs_rabbinic_review` (on the file) record what was and was not opened during research. `/api/community/<name>` strips them, including from `raw_data`, the Supabase rows leave them out, and the AI tools never read them. Put anything a reader should see in `summary`, `notes` or `variants` instead.

## The `runtime` block

`runtime.lens_key` is required. It is the exact key `backend/helpers.COMMUNITIES` uses for lens filtering (`Ashkenaz`, `Sefardic`, `Kavkazi`, `Turkish-Ottoman`, and so on), and it is what the migration writes into Supabase's `community_knowledge.community_name`. `/ask` retrieval matches that column with `ilike`, so a lens key that drifts from `COMMUNITIES` makes a community's rows invisible to the AI. `tests/test_customs.py::TestRealCustomsFiles` fails if any file's `lens_key` disagrees with `COMMUNITIES`. (`Israeli` has no file of its own; it routes to the Sephardic file.)

The rest of the block:

- `practice_baseline`: one sentence naming the codes and poskim the community's practice rests on. `backend/claude.py` feeds it to the AI as that community's lens (`_community_lens_instruction`). Where the file has none, `claude.py` falls back to a generic baseline.
- `parameters`: a list of `{key, value, unit, source}` entries for numeric or categorical settings. Today the one in use is Bukharian `sunset_display_offset_minutes` (18): `backend/zmanim_engine.py` subtracts it from the sunset it shows for that community and labels the time `(-18m)`. A community with no such parameter shows plain sunset. This one is set by the Sh'elah maintainers, not by the research, and its `source` says so.

`backend.customs.runtime_config()` returns `{lens_key.lower(): {name, lens_key, practice_baseline, parameters}}` for every file that declares a `lens_key`. It is cached and re-read when a file's name or modification time changes, like `load_all_customs()`.

## Where the data is used

- **`backend/customs.py`** loads every community file (everything except `customs_db.json` and `schema.json`) into an in-memory map, re-reading when a file's name or modification time changes. `search_customs(topic)` does exact and fuzzy matching and ranks results by match score and confidence (well-attested first, disputed and needs-review later, distinctive minhagim ahead of generic entries). Callers: `ShelahEngine` (`backend/data_service.py`) and the `search_community_customs` tool in the AI tool registry (`backend/ai_tools.py`). `runtime_config()` serves `claude.py` and the profile tool.
- **`backend/routes_community.py`** serves `/api/community/*` (the community pages) from the structured files. The response adds `major_authorities`, `distinctive_customs`, `disputes` and `gaps`, and each entry keeps its original casing and carries `variants`, `confidence`, `source_url` and `references`.
- **`backend/ai_tools.py`** `get_community_profile` returns the practice baseline, parameters, authorities, gaps and a short history from these files.
- **Live `/ask` retrieval** reads Supabase's `community_knowledge` table (`backend/rag.py`), not these files. The JSON is the source `scripts/migrate_customs_to_supabase.py` seeds that table from, with deterministic upserts. After editing a customs file, run the migration so answers see the change (next section).
- **Startup validation** (`validate_all_customs_at_startup`) checks every structured file for `heritage_id`, `name` and a non-empty `halacha_index`, and logs an error per bad file without stopping the app. It skips `customs_db.json` and `schema.json`. It is a structure check only; the `lens_key` guard above is a test, not a startup check.

## Pushing a change to Supabase

```bash
python scripts/migrate_customs_to_supabase.py --dry-run              # parse, print sample payloads, write nothing
python scripts/migrate_customs_to_supabase.py --community Sefardic   # one lens key only
python scripts/migrate_customs_to_supabase.py --prune                # upsert, then delete rows no longer in the JSON
python scripts/migrate_customs_to_supabase.py --emit-sql ./out       # write SQL files instead of connecting
```

- Row ids are `sha256(community|topic|source)[:32]`, so re-running is idempotent. Changing a topic or source label makes a new id, which leaves the old row behind until you run with `--prune`.
- `--prune` only deletes rows whose `community_name` is one this corpus has used (each file's `lens_key` and `name`, plus the retired aggregate's `Kafkazi`, `Sephardic`, `Ashkenazic` and `Bukharian`), and only ids that are not in the current JSON. It never touches any other `community_name`.
- `--emit-sql DIR` writes one transaction per community (`delete` that community's rows, then `insert ... on conflict (id) do update`) plus `00_retired_customs_db_cleanup.sql`. Paste the files into the Supabase SQL Editor, which runs as the table owner. Use this when no Supabase secret key is available locally. The output is a snapshot of the corpus, so keep it out of git.
- A direct run needs `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in the environment. Writes to `community_knowledge` are server-side only; the table has no insert, update or delete policy for `anon` or `authenticated`.

## Editing guidance

- Keep each file valid JSON in UTF-8 with the required fields populated, and `runtime.lens_key` equal to the key in `backend/helpers.COMMUNITIES`.
- Keep topic and category fields consistent across communities; the matcher's quality depends on it.
- Give every entry a `source` (and a `source_url` or `references` where a stable link exists), and a `confidence`. Do not write a ruling you cannot source: list the topic under `gaps` instead.
- Record the real split in `variants` (one object per subgroup or position) rather than flattening a disputed practice into one sentence, and mark it `disputed`. Do not write process talk ("search excerpt only", "unverified") into user-facing fields; put that in `review_notes`.
- Write the Hebrew fields for every new entry.
- Keep prompt size in mind. The AI sees at most 600 characters of each customs row (`CUSTOMS_ROW_MAX_CHARS` in `backend/claude.py`; retrieval reads 700, `RAG_KNOWLEDGE_CONTENT_CHARS` in `backend/rag.py`), and the whole dynamic context is capped at 4200 characters. The stored text runs summary, confidence caveat, variants, common practices, notes, so the competing positions of a machloket survive the cut; lead with the ruling and keep the long tail in `notes`, since the tail is what gets cut.
- A changed file is not live in `/ask` until it has been migrated to Supabase (see above).
