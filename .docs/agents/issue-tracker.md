# Issue tracker

This repository declares its ticket root here. The format and the dispatch rule are in [Tickets](../../references/tickets.md).

## Root

`.scratch/<feature-slug>/`

`<feature-slug>` is English kebab-case. One feature is one directory. A directory named `issues` at the repository root is not this tracker.

## Layout

```text
.scratch/<feature-slug>/
  spec.md
  issues/<NN>-<slug>.md
```

`spec.md` is the feature spec. Ticket files start at `01`, use two digits, and use an English kebab-case slug. The number in the filename matches the number in the title.

`scripts/collab.py` does not read these files.
