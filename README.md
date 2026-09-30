# gitdesk

One panel over **every** repository on the machine — not one at a time.

GitHub Desktop can't scan a disk (requests open since 2017: [#1574](https://github.com/desktop/desktop/issues/1574), [#19662](https://github.com/desktop/desktop/issues/19662)),
so every repo is added by hand. But the missing scan isn't the real problem. The real
problem is the questions no client asks, because each one only sees a single repo:

- where there's no commit, and where there's a commit but no push
- what is public and what is private
- **which working copy is ahead**, when the same repo lives on the PC and on a USB drive
- where a secret is one `git add -A` away from leaking
- what a deployment copy without `.git` carries, which no client can see

## Usage

```
python gitdesk.py list            # report (remote data from the cache)
python gitdesk.py list --fetch    # refresh the remotes first - slower, but true
python gitdesk.py twins           # only the groups of twins
python gitdesk.py scan            # scan and save the cache
python gitdesk.py list --json     # for scripts
python gitdesk.py serve           # the panel; the cache at once, a fresh scan in the background
```

The panel queries repositories in parallel (up to 16 at a time) without blocking the first
screen. The default filter (`do-zrobienia`, "to do") shows only repos that are dirty, need
a push or a pull, or need a decision.

`synchronizuj bezpiecznie` ("sync safely") runs, in order: fetch everything, push ready
commits, fetch again and `pull --ff-only` the clean copies that are behind. It never
commits a dirty repo on its own — the commit message goes in next to it, and the
`commit + push` button then does both. If a dirty copy is also behind, the histories have
to be joined by hand first: the panel deliberately doesn't resolve conflicts.

The first run creates `config.json` with the defaults.

## Fresh data, or why `--fetch`

`git status` counts ahead/behind against the `origin/...` stored locally at the **last
fetch**. A repo nobody fetched for 111 days can be 20 commits behind while status says zero
— and is formally right.

So the verdict "in sync" (`zgodne`) only appears with a fresh fetch (< 24 h). Without one
it's `niezweryfikowane` ("unverified"). Being diverged or ahead always shows — a local
commit is a fact no matter when the remote was last asked.

## Twins

Working copies pointing at the same remote. The verdict is computed **without reaching
across repos**: each copy has its own `origin/<branch>`, so comparing each one's
ahead/behind is enough. `git merge-base A B` doesn't work here — they are separate object
databases; the PC copy doesn't know the USB drive's commits.

## Intent labels

Without them the tool reports every repo without a remote as a problem — and teaches you
to ignore red. In `config.json`:

- `local_only` — deliberately without a remote, a private tool. Shown in grey, no nagging
  about GitHub.
- `foreign` — not my code. No write actions at all.

## Deployments

Copies without `.git` (e.g. dropgate on a USB drive). They work, but quietly go stale —
`git log` can't answer, there's nothing to ask. `gitdesk` compares the content file by file
with the source repo's HEAD, and separately reports what the copy carries that the repo
doesn't: that's exactly where keys end up, the ones gitignore rightly keeps out of the
repo and that leave the house with the drive.

## History graph

`graf` next to each repo draws the DAG as SVG — lane assignment is the same idea
`git log --graph` draws in ASCII. A merge is a hollow circle with two incoming edges.

`graf blizniakow` ("twins graph") shows **both working copies in one picture**: the common
ancestor and two diverging tails, each commit marked "both", "only A" or "only B". No git
client does this, because none knows you have the same repo in two places.

The trap that forces this: **`git merge-base A B` between two clones doesn't work** —
separate object databases, the PC copy doesn't know the drive's commits. So the other copy's
history is fetched over a local path (no network) into a temporary ref
`refs/gitdesk/twin`, deleted right after drawing. Archive copies are left out — the fetch
would add objects to them.

## Where to expose it

`--bind local` (the default) or `--bind tailnet`. **Never through a public tunnel.**

The panel runs `add`, `commit`, `reset`, `push` and `pull` on dozens of repositories with
the credentials the machine already has — Git Credential Manager and `gh` are logged in.
There's no token to steal because none is needed: whoever reaches the panel pushes as the
account owner. The session token protects against CSRF; it is not a login system.

Need it from a phone? A private network where WireGuard identity is the authentication —
`--bind tailnet`. The panel then faces the whole tailnet, and the tool says so loudly at
start.

## Tests

```
python gitdesk.py --selftest
```

20 assertions on temporary repos in `%TEMP%`. They check what would fail **silently**: a
commit block for secrets doesn't shout when it stops working — it just lets things through.

The first run of this test found two real bugs: `probe()` read the fetch time only from
`FETCH_HEAD`, which a fresh clone doesn't have (every newly cloned repo reported "state
unknown"), and the secret-block test itself would have passed for free, because this
machine's global gitignore contains `.env` and the file never reached the index.

## Dependencies

None outside the standard library (Python 3.14).

The secret scan isn't written again here: `gitdesk` loads
[`workspace-doctor`](../workspace-doctor) as a module and uses its patterns and
`looks_synthetic()`. **A missing doctor is a hard error at start**, not a silent skip — a
tool that quietly switches off its failsafe is worse than no tool.

## Status

Done: discovery, repo state, fetch, visibility, twins, deployments, a browser panel with
actions, three views (list / tiles / groups), selftest, and the history graph including
the twins graph.

Deliberately out of scope: a **merge tool**. Not because of the work, but the risk — a
conflict is rare, so such a tool is least tested exactly when it's needed most, and the
cost of a bug is lost work. From a conflict on, it's `git`.
