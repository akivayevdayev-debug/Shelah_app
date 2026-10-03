# Customs data notes

Community minhag data: thirteen community files, a JSON Schema, and one retired legacy aggregate.

## Files

| File | What it is |
|---|---|
| `ashkenaz.json`, `bukharian.json`, `ethiopian.json`, `georgian.json`, `greek-romaniote.json`, `iraqi.json`, `moroccan.json`, `mountain-jewish-kavkazi.json`, `persian.json`, `sefardic.json`, `syrian.json`, `turkish-ottoman-sefardic.json`, `yemenite.json` | One structured file per community |
| `schema.json` | JSON Schema for the structured shape. Required: `version`, `heritage_id`, `name`, `halacha_index` |
| `customs_db.json` | The legacy flat aggregate (`Kafkazi`, `Bukharian`, `Sephardic`, `Ashkenazic`). The loader skips it: it is retired from community and customs browsing and kept only as history |

A structured file carries `version`, `type`, `heritage_id`, `name`, `aliases`, `database_scope`, `identity`, `languages`, `genealogy`, `historical_background`, `core_halachic_authorities`, `halacha_index`, `unique_minhagim` and `source_registry`.

## Where the data is used

- **`backend/customs.py`** loads every `customs/*.json` (except `customs_db.json`) into an in-memory map, re-reading when a file's name or modification time changes, and answers `search_customs(topic)` with exact and fuzzy matching. Callers: `ShelahEngine` (`backend/data_service.py`) and the `search_community_customs` tool in the AI tool registry (`backend/ai_tools.py`).
- **`backend/routes_community.py`** reads the structured files directly for `/api/community/*` (the community pages).
- **Live `/ask` retrieval** reads Supabase's `community_knowledge` table (`backend/rag.py`), not these files. The JSON is the source that `scripts/migrate_customs_to_supabase.py` seeds that table from, with deterministic upserts. After editing a customs file, re-run the script (`--dry-run` first; `--community <name>` limits it to one community) so answers see the change.
- **Startup validation** (`validate_all_customs_at_startup`) checks every structured file for `heritage_id`, `name` and a non-empty `halacha_index`, and logs an error per bad file without stopping the app. It skips `customs_db.json` and `schema.json`.

## Editing guidance

- Keep each file valid JSON in UTF-8 with the three required fields populated.
- Keep topic and category fields consistent across communities; the matcher's quality depends on it.
- Cite a source in `source_registry` for each custom wherever one exists, so the UI can show where a ruling comes from.
- A changed file is not live in `/ask` until it has been migrated to Supabase (see above).
