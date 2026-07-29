---
name: antigravity-cli
description: "Operate the Antigravity CLI (agy): plugins, auth, sandbox."
version: 0.1.0
author: Tony Simons (asimons81), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Antigravity, CLI, Auth, Plugins, Sandbox]
    related_skills: [grok, codex, claude-code, hermes-agent]
---

# Antigravity CLI (`agy`)

Operator guide for the Antigravity CLI, invoked as `agy`. Run all `agy`
commands through the Hermes `terminal` tool; inspect its config and logs with
`read_file`. This skill is reference + procedure — it does not wrap a network
API, so there is nothing to authenticate from Hermes itself.

## When to Use

- Installing, updating, or smoke-testing the `agy` binary
- Driving non-interactive `agy --print` / `agy -p` one-shots
- Debugging Antigravity auth, sandbox, permissions, or plugin state
- Reading Antigravity settings, keybindings, conversations, or logs

## Mental model

Antigravity has two layers — keep them distinct or the guidance will be wrong:

1. **Shell wrapper commands** — `agy help`, `agy install`, `agy plugin`,
   `agy update`, `agy changelog`. Run these through the `terminal` tool.
2. **Interactive in-session slash commands** — `/config`, `/permissions`,
   `/skills`, `/agents`, etc. These only exist inside a running `agy` TUI
   session, not on the shell wrapper.

`agy help` shows the shell wrapper surface, NOT the in-session slash commands.

## Prerequisites

- The `agy` binary on PATH. Verify through the `terminal` tool:
  `command -v agy && agy --version`.
- No env vars or API keys required by this skill — Antigravity manages its own
  auth via the OS keyring / browser sign-in (see Authentication below).

## How to Run

Invoke every `agy` command through the `terminal` tool. Examples:

```
terminal(command="agy --version")
terminal(command="agy help")
terminal(command="agy plugin list")
terminal(command="agy --print 'Summarize the repo in 3 bullets'", workdir="/path/to/project")
```

For an interactive multi-turn TUI session, launch `agy` with `pty=true` (and
tmux for capture/monitoring), the same pattern the `codex` / `claude-code`
skills use. For one-shot smoke tests and scripted prompts, prefer
`agy --print` (non-interactive).

To inspect Antigravity's own files, use `read_file` on the paths under Core
paths below — do not `cat` them through the terminal.

## Core paths

- Binary / entrypoint: `agy`
- App data dir: `~/.gemini/antigravity-cli/`
- Settings file: `~/.gemini/antigravity-cli/settings.json`
- Keybindings file: `~/.gemini/antigravity-cli/keybindings.json`
- Logs: `~/.gemini/antigravity-cli/log/cli-*.log`
- Conversations: `~/.gemini/antigravity-cli/conversations/`
- Brain artifacts: `~/.gemini/antigravity-cli/brain/`
- History: `~/.gemini/antigravity-cli/history.jsonl`
- Plugin staging: `~/.gemini/antigravity-cli/plugins/<plugin_name>/`

## Quick Reference

### Wrapper commands
- `agy changelog`
- `agy help`
- `agy install`
- `agy plugin` / `agy plugins`
- `agy update`

### Useful flags
- `--add-dir`
- `--continue` / `-c`
- `--conversation`
- `--dangerously-skip-permissions`
- `--print` / `-p`
- `--print-timeout`
- `--prompt`
- `--prompt-interactive` / `-i`
- `--sandbox`
- `--log-file`
- `--version`

### Plugin subcommands (`agy plugin --help`)
- `list`, `import [source]`, `install <target>`, `uninstall <name>`,
  `enable <name>`, `disable <name>`, `validate [path]`, `link <mp> <target>`,
  `help`

### Install flags (`agy install --help`)
- `--dir`, `--skip-aliases`, `--skip-path`

### In-session slash commands
- **Conversation control:** `/resume` (`/switch`), `/rewind` (`/undo`),
  `/rename <name>`, `/clear`, `/fork`, `/reset`, `/new`
- **Settings & tools:** `/config`, `/settings`, `/permissions`, `/model`,
  `/keybindings`, `/statusline`, `/tasks`, `/skills`, `/mcp`, `/open <path>`,
  `/usage`, `/logout`, `/agents`
- **Prompt helpers:** `@` path autocomplete, `esc esc` clears the prompt (when
  not streaming), `!` runs a terminal command directly, `?` opens help

## Settings and permissions

### Common settings keys (`settings.json`)
- `allowNonWorkspaceAccess`
- `colorScheme`
- `permissions.allow`
- `trustedWorkspaces`

### Permission modes
`request-review`, `always-proceed`, `strict`, `proceed-in-sandbox`.

### Sandbox behavior
- `enableTerminalSandbox` is a boolean in `settings.json`; default `false`.
- Launch-time overrides (`--sandbox`, `--dangerously-skip-permissions`) can
  supersede persistent settings for the current session.

## Authentication behavior

- The CLI tries the OS secure keyring first.
- With no saved session, it falls back to browser-based Google sign-in.
- Locally it opens the default browser; over SSH it prints an authorization URL
  and expects the auth code pasted back.
- `/logout` removes saved credentials.

### Session-scoped auth and Herdr reuse

A fresh SSH-launched `agy models` can report signed out while a long-lived authenticated Agy TUI continues refreshing models with in-memory OAuth state. Before asking the user to sign in again:

1. Inspect `pgrep -fl '(^|/)agy|antigravity'` and safe auth lines in the latest CLI log.
2. Run `herdr agent list` and locate the registered `agy` pane.
3. Read it with `herdr agent read agy --source visible --lines 40 --format text`.
4. Route prompts through `herdr agent send` and `herdr pane send-keys`; never copy keyring/token material or write directly to the TTY.
5. Explicitly select and read back the model with `/model EXACT DISPLAY NAME`. The picker may require one additional `enter` to confirm.
6. For multiline prompts, verify the agent transitions to `working`; if text remains in the editor, send one additional `enter`, not repeated blind keystrokes.

Use the `herdr-agy-adjudication` skill for the complete review-and-receipt workflow.

## Plugins

- Plugins stage under `~/.gemini/antigravity-cli/plugins/<plugin_name>/`.
- They can bundle skills, agents, rules, MCP servers, and hooks.
- `agy plugin list` returning no imported plugins is a valid empty state.

## Pitfalls

- **Inline `--print` prompts break under tmux send-keys.** When launching agy in a tmux session via `tmux send-keys`, inlining a long prompt as `agy --print "$(cat file.md)"` or `agy --print 'multi-line text...'` gets mangled by shell quoting — backticks, `$()`, apostrophes, and markdown code fences all break. The tmux pane shows `cmdand dquote>` continuation prompts and parse errors.
  **Fix:** Write the prompt to a temp file, then pipe via stdin:
  ```
  write_file(path="/tmp/agy-prompt.md", content="...full prompt...")
  tmux send-keys -t session 'cd /project && cat /tmp/agy-prompt.md | agy --print-timeout 600s --print --allowedTools "Read,Bash,Write" 2>&1 | tee /tmp/agy-output.txt' Enter
  ```
  This sidesteps all shell quoting. Same pattern as Claude Code's `cat file | claude -p`.
- `agy help` shows wrapper commands, not interactive slash commands.
- `agy --version` is the safe non-interactive version check; `agy version` is
  interactive and can fail without a real TTY.
- First place to look for failures: `~/.gemini/antigravity-cli/log/cli-*.log`
  (read with `read_file`).
- Startup logs can show `error getting token source: You are not logged into
  Antigravity` before silent keyring auth completes. Do **not** call that an
  auth/model-access outage if the same run later shows `ChainedAuth:
  authenticated via keyring`, `OAuth: authenticated successfully`,
  `fetchAvailableModels`, or `streamGenerateContent`.
- For model-access smoke tests, use a clean temp workspace rather than `/tmp` or
  a long-lived project: `d=$(mktemp -d /tmp/agy-smoke-XXXXXX); cd "$d"; agy
  --print-timeout 60s --model 'Gemini 3.5 Flash (High)' --print 'Do not inspect
  files. Reply exactly: AGY_OK'`.
- **Treat an off-prompt `--print` response as session/workspace contamination, even when exit code is 0.** Before granting write tools, run from a fresh temp workspace with a unique nonce and require the response to echo that nonce. If the output discusses unrelated files, commits, or prior tasks, exclude it from evidence, verify any claimed side effects independently, and restart from a fresh workspace/conversation. Do not let a contaminated run modify the target repository.
- Do **not** assume a named fleet agent is Antigravity/Agy just because the
  symptom mentions model access. First verify the identity mapping from the
  fleet source of truth. Example: Jack corrected that **Aegis is the Mac mini
  Hermes agent**, not the local Paperclip Antigravity/Agy lane.
- **Model names are display strings with spaces and parentheses, not slugs.**
  `--model gemini-3.1-pro` fails with "model not recognized." The correct form
  is `--model "Gemini 3.1 Pro (High)"` (case-sensitive, with quotes). Run
  `agy models` (or trigger an invalid model name) to list available models and
  their exact display strings. Known models as of 2026-07-14:
  `Gemini 3.5 Flash (Medium|High|Low)`, `Gemini 3.1 Pro (Low|High)`,
  `Claude Sonnet 4.6 (Thinking)`, `Claude Opus 4.6 (Thinking)`,
  `GPT-OSS 120B (Medium)`.
- **Do not use `--mode plan` when exact boundary markers are part of the output
  protocol.** Antigravity 1.1.2 prefixes the submitted prompt with `/plan` in
  this mode and can append `/plan` to an otherwise exact nonce, breaking strict
  marker parsing. For a read-only bridge, prefer a dedicated empty workspace
  with `--sandbox --model "Gemini 3.1 Pro (High)"` in the default
  request-review mode; never send approval keystrokes, and fail closed on any
  tool/trust/permission modal.
- **Plugins can hijack `--print` sessions and produce off-prompt output.** The
  `mandates-loader` plugin (and similar skill-injecting plugins) intercept the
  prompt before the model sees it, causing `agy -p` to write daily notes, commit
  to git, and run permission checks instead of answering the actual question.
  The exit code is 0 and the output looks "successful" but it's contaminated —
  the architect query was never answered.
  **Fix:** Disable plugins before running one-shot architect/review queries:
  ```
  agy plugin disable mandates-loader
  agy -p --dangerously-skip-permissions --model "Gemini 3.1 Pro (High)" "prompt"
  agy plugin enable mandates-loader
  ```
  Re-enable after. The disable/enable cycle is safe and non-destructive.
  This is distinct from the existing "workspace contamination" pitfall —
  that was about prior session state; this is about active plugin
  interception of the prompt itself.
- Don't confuse persistent JSON settings with launch-time overrides.
- `~/.gemini/antigravity-cli/bin/agentapi` is a thin wrapper to `agy agentapi`.
- On WSL, token storage is file-based, so auth issues are usually local-file /
  session-state problems, not browser-only problems.
- Workspace identity can depend on launch directory and the `.antigravitycli`
  project marker.

## Verification

Confirm the install is real and usable, all through the `terminal` tool (read
files with `read_file`):

1. `terminal(command="command -v agy")`
2. `terminal(command="agy --version")`
3. `terminal(command="agy help")`
4. `terminal(command="agy plugin list")`
5. `read_file` on `~/.gemini/antigravity-cli/settings.json`
6. `read_file` on the latest `~/.gemini/antigravity-cli/log/cli-*.log`
7. If needed, `read_file` on `~/.gemini/antigravity-cli/keybindings.json`

## Support files

- `references/cli-docs.md` — condensed notes from the getting-started, usage,
  and features docs.
