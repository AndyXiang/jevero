# archive/

Reference code that is **not part of the package**. Nothing here is imported,
installed, linted, or covered by `pytest`. It exists so that a deliberate
decision to restore a feature does not mean rewriting it from memory.

## `zotero_web_api.py`

The zotero.org **Web API** client, snapshotted verbatim from
`src/jevero/zotero.py` at the moment the project switched to the Zotero desktop
**local API**.

Why it was archived:

- The local API needs no credentials for reads, works offline, and has no rate
  limits, which is what the dry-run workflow wants.
- Keeping two backends meant two credential paths, two version spaces (local
  object versions have **no relation** to Web API versions), and a `backend`
  switch in every config and test.

### What the active client still shares with it

`ZoteroItem`, `normalize_item`, `merge_tags`, pagination, and the error
hierarchy were backend-agnostic and stayed in `src/jevero/zotero.py`.

### To restore Web API support

1. Re-add `backend` / `library_type` / `library_id` to `ZoteroConfig` in
   `src/jevero/config.py`, plus a `base_url` default per backend.
2. Re-add the credentials and their helpers: `ZOTERO_API_KEY`,
   `ZOTERO_LIBRARY_ID`, `zotero_api_key()`, `zotero_library_id()`, and the
   `ZOTERO_API_KEY` line in `.env.example`.
3. Return the `apply_actions` PATCH branch to the active client, with
   `Authorization: Bearer <key>`.
4. Restore the client tests from git history (`git log -- tests/test_zotero.py`)
   and extend them to cover `Backoff` / `429 Retry-After`, which neither the
   archived snapshot nor the current client handles.
5. Never mix backends within one run: object versions from the local API and
   the Web API are unrelated, so a version read from one must not be used as a
   precondition against the other.

### Environment variables it used

```text
ZOTERO_API_KEY       # from zotero.org/settings/keys, needs write access
ZOTERO_LIBRARY_ID    # numeric user ID, or a group ID with library_type: group
```
