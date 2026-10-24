# homelib collections

Served to homelib by `GET /api/homelib/collections` (see `app/homelib.py`).

- `collections.json` lists the library sections. Each takes its manifest links
  from a tracked post (`post_id`), a list file here (`manifests`), inline
  `links`, or any mix.
- `*.txt`: one module manifest URL per line; `#` starts a comment.

Edits take effect on homelib's next check (every few hours); no restart needed.
