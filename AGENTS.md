# AGENTS.md

## Documentation Maintenance Rule

After every code change, documentation must be checked and updated if needed.

The project must maintain two types of documentation:

1. Historical change record
2. Current-state documentation

---

## 1. Revision History

The file `docs/revision_history.md` must be updated after every code change. No include document change.

It must record:

* Change date
* Changed files or modules
* Summary of the change
* Related documentation updates, if any

The revision history must be written in English.

New entries should be appended to the existing history. Do not remove previous entries.

---

## 2. Current-State Documentation

The following documents must always reflect the latest project state:

* All files under `docs/`, except `docs/revision_history.md` and `docs/archive/`
* `./CONTEXT`

These documents are current-state documents.

They do not need to preserve historical information.

When code changes affect behavior, interfaces, parameters, data structures, algorithms, configuration, file I/O, IBAPI/TWS flow, order flow, error handling, or phase scope, update the relevant current-state documents so they describe the latest state only.

Remove or rewrite outdated descriptions when necessary.

### Local Archive Exception

Files under `docs/archive/` are local archives. They do not require maintenance,
current-state synchronization, revision-history entries, or GitHub publication.

---

## 3. Final Response Requirement

Every task completion response must include:

* Code changes made
* Documentation files updated
* Whether `docs/revision_history.md` was updated
* If no current-state documentation was updated, explain why

If no documentation update is needed, state:

`Docs: not updated, because ...`
