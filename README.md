# review-loop

Automated code review loop between [Claude Code](https://code.claude.com/) and [Codex CLI](https://github.com/openai/codex).

Claude implements your task, Codex reviews it, Claude fixes the issues, repeat until the review passes.

Supports two modes:
- **Code review** (default) - reviews commit diffs for bugs and issues
- **Architecture review** (`--arch`) - reviews plans for implementation/operational risks, outputs P1/P2/P3 findings

## How it works

```
review-loop "implement feature X"
    |
    v
[Claude Code] --> implements + commits
    |
    v
[Codex CLI] --> reviews the commit
    |
    v
Issues found? --NO--> Done
    |YES
    v
[Claude Code] --> fixes issues + commits (same session)
    |
    v
Loop (max 5 iterations)
```

Key details:
- Claude Code runs via `claude -p` (non-interactive mode)
- Codex reviews via `codex review --commit HEAD`
- Claude resumes the same session (`--resume`) so it has full context of previous work
- Stops when review is clean, max iterations reached, or no new commit is created

## Requirements

- [Claude Code CLI](https://code.claude.com/) (`claude` in PATH)
- [Codex CLI](https://github.com/openai/codex) (`codex` in PATH)
- Python 3.8+
- A git repository to work in

## Install

```bash
# Download the script
curl -o ~/bin/review-loop https://raw.githubusercontent.com/illlefr4u/review-loop/main/review-loop
chmod +x ~/bin/review-loop

# Add to PATH (if ~/bin is not already there)
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc

# Optional: add alias
echo 'alias rl="review-loop"' >> ~/.zshrc
```

## Usage

```bash
# From any git repository:
review-loop "implement retry logic for API calls"

# Architecture review (plan, not code):
review-loop --arch "design the retry system"

# With options:
review-loop --max-iter 3 "fix the parser bug"
review-loop --arch --max-iter 3 "design auth module"
review-loop --claude-model sonnet "quick refactor"
review-loop --codex-model o3 "complex task"
review-loop --dry-run "test what would happen"
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--arch` | false | Architecture review mode (P1/P2/P3) |
| `--max-iter` | 5 | Maximum review iterations |
| `--claude-model` | opus | Claude model to use |
| `--codex-model` | (default) | Codex model to use |
| `--timeout-claude` | 600 | Claude timeout in seconds |
| `--timeout-codex` | 180 | Codex timeout in seconds |
| `--no-commit` | false | Don't ask Claude to commit |
| `--dry-run` | false | Show plan without executing |

## Using from within Claude Code

You can also use this workflow directly inside a Claude Code session without the standalone script. Add this to your `CLAUDE.md`:

```markdown
| `review` | Codex code review loop |
| `review-arch` | Codex architecture review loop |

### review (Codex Code Review Loop)

1. Commit uncommitted changes (no push)
2. Run `codex review --commit HEAD -o /tmp/codex-review.txt` (timeout 300s)
3. Read the review output
4. If no actionable issues: done
5. If issues found: fix all, commit, go to step 2
6. Max 5 iterations

### review-arch (Codex Architecture Review Loop)

1. Commit uncommitted changes (no push)
2. Run `codex exec "Review the provided architecture plan and output findings only as P1/P2/P3 with section references, focused on implementation and operational risks." -o /tmp/codex-review.txt` (timeout 300s)
3. Read the review output
4. If no P1/P2/P3 findings: done
5. If findings: address all, revise plan, commit, go to step 2
6. Max 5 iterations
```

Then just say `review` or `review-arch` after Claude implements your task or writes a plan.

## How review detection works

The script uses heuristics to determine if a review contains actionable issues:

**Code review mode:**
- **Clean signals** (no issues): "lgtm", "looks good", "no issues found", "approved"
- **Issue signals**: "bug", "vulnerability", "missing", "should", "fix", "critical"
- If 3+ issue signals are found, the review is treated as having issues

**Architecture review mode (`--arch`):**
- Looks for P1/P2/P3 markers in the output
- If any P1/P2/P3 found, the review has issues
- Falls back to code review heuristics if no markers found

## License

MIT
