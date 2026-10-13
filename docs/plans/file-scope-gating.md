# Plan: File-Scope Permission Gating

## Context

Permission gating read a tool's tier and nothing else.  A `read_file` is
`READ`, so it was auto-approved wherever it reached — `~/.ssh/id_rsa`, or the
project's own `.env`.  And the shell tool made it worse: `classify_command`
reads command *names*, so `cat .env` was `SHELL_SAFE` and auto-approved too.

This adds the other half of the question.  A tier says what a call does; a
**file scope** says how far it reaches:

- `WORKSPACE` — inside the directory the agent was given,
- `EXTERNAL` — outside it, the user's call rather than the agent's,
- `RESTRICTED` — on a configured list, refused at every tier.

The gate's verdict is now one of **allow**, **ask** or **deny**, and `deny` is
new: it is policy, so the approval prompt never offers it.

| tier | WORKSPACE | EXTERNAL | RESTRICTED |
|---|---|---|---|
| READ | allow | ask | deny |
| WRITE | allow in AUTO / ask in MANUAL | ask | deny |
| SHELL_SAFE | allow | ask | deny |
| SHELL_DANGEROUS | ask | ask | deny |

## Changes

### 1. `toddler/tools/base.py` — `FileScope`, `Decision`, `FileScopePolicy`

- `FileScope` and `Decision` sit beside `Permission` and `PermissionMode`.
- `FileScopePolicy(workspace_root, restricted)` resolves the root and every
  entry once, at construction, so all later comparisons are resolved against
  resolved — a root reached through a symlink (`/tmp` on macOS) and a path
  spelled through the same link compare equal.  Relative entries name a path
  inside the workspace; `~` expands to home.
  - `classify(path)` — restricted, then workspace, else external; a path that
    cannot be resolved at all is external (ask, never allow).
  - `classify_many(paths)` — the strictest of the lot; no paths is workspace.
  - `restricted_match(paths)` — which path matched, for the denial message.
- `PermissionManager.decide(perm, scope)` replaces `needs_confirmation(perm)`.
- `BaseTool` gains `path_params`, an injected `scope_policy`, `get_scope_paths`
  and `get_file_scope`.

### 2. `toddler/tools/{files,search,git,shell,plan}.py` — declaring the reach

- `read_file`, `write_file`, `edit_file`: `path_params = ("file_path",)`.
- `grep`, `glob`: `("path",)`, defaulting to the working directory, which is
  where the tool actually searches.
- the git tools share `_RepoScopedTool`: one repository, `repo_path` or the
  working directory, as `_resolve_repo_path` reads it.
- `shell` overrides `get_scope_paths`, since its paths are in the command
  string.
- `plan_update` names nothing and stays ungated.

### 3. `toddler/tools/shell.py` — the files a command names

The command is tokenized with the classifier's own `_split_segments`, then
each segment yields the tokens that could name a file:

- operands, plus the value of a `--flag=value`, plus a redirection target —
  the last of those is a path *before* it exists, which is how `echo x > .env`
  gets caught;
- a pattern position is not a path: `grep -rn .env .` searches for the string,
  `find . -name .env` matches by name.  Reading them as files would refuse a
  call that opens nothing, and a refusal is not overrulable;
- a token the shell expands at run time (`$HOME`, `${VAR}`) is unknowable
  rather than local, so the call asks; only a positive restricted match
  outranks that;
- inclusion by evidence: a bare word counts when it exists, or when it is a
  glob that matches something (capped, so a broad pattern is a scan);
- the working directory is part of the reach — the command runs in it.

### 4. `toddler/agent/loop.py` and `toddler/tools/executor.py` — the gate

- the loop decides once per call (`_decision_for`) and hands the verdict to
  `_execute_with_gating`, so the pause and the execution cannot disagree;
- `ASK` pauses as before; `DENY` yields no `AgentPaused` — there is nothing to
  ask — and returns a failed result carrying `blocked_message(...)`, which
  names the path and says retrying is pointless (it reaches the model and stays
  in the transcript);
- the executor reaches the same verdict through the shared manager and returns
  on `DENY` *before* its confirmation callback, so an auto-approving callback
  (or no callback at all) cannot launder a denial.

### 5. `toddler/config/` — the restricted list

`RESTRICTED_PATHS = (".env", ".env.local", "~/.toddler/.env")`, overridable
with the pathsep-separated `TODDLER_RESTRICTED_PATHS` (a blank value means
unset, not "no restrictions").  `SessionManager` builds the session's policy
from its working directory and these settings, and hands it to every tool
through `create_default_registry`.

## Key Design Decisions

- **The tool answers for its own call.**  Only the tool knows what base its
  arguments resolve against: `git diff -- <target>` is repo-relative, `grep`
  defaults to `"."`, shell to `working_dir or cwd`.  A gating layer that
  derived paths itself would need per-tool knowledge, which is exactly what
  `get_permission` exists to avoid.  The split between `get_scope_paths` (what
  a tool overrides) and `get_file_scope` (derived) keeps that knowledge in one
  place per tool and still lets shell express something a path list cannot.
- **Restricted is checked before the mode, and is not a question.**  A wrong
  `deny` is recoverable by editing the list; a secret in the transcript is not.
- **External never auto-approves.**  AUTO widens what the agent may do inside
  the workspace it was given.  It is not a statement about everywhere else.
- **A call that names no file is workspace.**  No reach, no file risk; the tier
  alone decides.  It is a fail-open default for a future tool that forgets to
  declare its paths, which is why `path_params` sits next to `permission`.
- **The shell scan errs toward asking.**  Missing a path costs a fallback to
  the command's own scope; inventing one costs a refusal the user cannot
  overrule.  Hence "inclusion by evidence" and the pattern-position rule.
- **Two hardenings beyond the literal asks**: an expansion marker (`$`) reads
  as unknowable rather than local — otherwise `cat $HOME/.toddler/.env` is
  SHELL_SAFE and workspace and runs unasked — and a restricted path is read as
  written as well as as resolved, so a listed file swapped for a link out of
  the tree mid-session stays a refusal instead of becoming a prompt.
- **A visible cost, accepted**: any shell command naming a path outside the
  workspace (`ls ..`, `find /tmp`, `git -C /other status`) now prompts where it
  used to run unasked.  The classifier cannot tell a harmless look from a
  reach, and the agent can usually say what it wants inside the workspace.
- **Messages name the path as written; the disk is given the resolved one.**
  `resolve()` is what opening needs, and on macOS it rewrites whole trees
  (`/tmp` → `/private/tmp`, `/home` → `/System/Volumes/Data/home`), so a
  refusal or a "wrote N bytes to …" that echoed it would name a file the
  reader never asked for.  `display_path()` keeps the spelling and resolves
  only a `..` standing after a link, where the written form genuinely names a
  different file.  The one place the two must not be conflated is `grep`:
  BSD grep will not descend into a directory handed to it as a link, so the
  search runs against the resolved path and the match lines are renamed back.

## Verification

1. `.venv/bin/python -m pytest tests/` — including the new
   `tests/test_file_scope.py`, `tests/test_shell_scope.py`, and the scope cases
   in `tests/test_agent_loop.py`.
2. `.venv/bin/python -m toddler` in a repository with a `.env`:
   - `read the file .env` → refused with no prompt, error names the path;
   - `cat .env`, `echo x > .env`, `cat .env*` → refused; `cat .env.example` →
     runs;
   - `read ~/.zshrc` → prompts in both MANUAL and AUTO;
   - `ls -la`, `grep -rn todo toddler/` → still unasked;
   - `ls ..` → prompts.
3. `/mode` reports the policy; `TODDLER_RESTRICTED_PATHS=/tmp/foo` in
   `~/.toddler/.env`, then a restart, refuses `/tmp/foo`.

## Out of Scope

- Telling the model about the restricted list in the system prompt, so it
  stops attempting `.env` reads in the first place.
- A scope badge in the console UI — the wire protocol is untouched, and a
  refused call already arrives as a failed call row.
- Pattern entries in the restricted list (`*.pem`, nested `pkg/.env`): v1
  lists exact paths and directories.
