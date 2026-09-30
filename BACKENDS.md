# Backends: choosing the model an agent runs on

Some skills in this pack start a second agent — a reviewer, a duel's opponent, a judge. A
**backend** is a named entry in a file you keep that says which model that agent runs on,
which provider serves it, and where its credential comes from. A skill refers to a backend by
name; you write the entry once and every skill can use it.

You do not need a backends file at all. Without one, every agent runs on its CLI's own
sign-in.

This file is reference material for writing your own entries. No skill points at it, because
installed skills carry nothing from the repository root.

## Where the file lives

`~/.portable-agent-skills/backends.json` — the `.portable-agent-skills` folder in your home
folder, on every platform, beside `~/.claude` and `~/.codex`. Set
`PORTABLE_AGENT_SKILLS_BACKENDS` to a path to use another file instead.

A default install (`python3 install.py`, no `--target`) creates it from the pack's
`backends.default.json` when you have none; see [The default backends](#the-default-backends).
From then on the file is yours. The installer never changes or removes an existing one, on
reinstall, upgrade or uninstall, and it says whether it created the file or kept yours. The
same holds for the key file described below. An install with `--target` touches only the
skills directories it names.

## The default backends

The file a default install writes holds eleven backends, plus a twelfth set aside: three
models, each for Claude Code and for Codex, each on OpenRouter and on Fireworks. Every one asks
its provider for inference on hardware in the United States:

- **Fireworks** — a `-us` router model id on `us.api.fireworks.ai`. Fireworks sends its
  standard serverless requests to sites around the world. Its US-only serverless "serves
  inference exclusively from the US", and the `-us` model id is what selects it. A plain model
  id would run anywhere, with no error. Fireworks' account-level data residency setting rejects
  any request outside the chosen region, but only Enterprise accounts have it. On any other
  plan its settings page does not appear, and the `-us` model ids are the only safeguard. On
  any Fireworks plan you can instead reach the same model through OpenRouter's US address,
  which refuses to run it outside the US; that address needs an OpenRouter Business or
  Enterprise plan.
- **OpenRouter** — its US endpoint `us.openrouter.ai`. It routes only to provider endpoints
  whose infrastructure is in the US, and it fails with an error rather than sending the
  request elsewhere. A guardrail on the key, with its allowed data regions set to `us`, also
  rejects a request that reaches the global address by mistake.

That is only each provider's stated guarantee, because no response says where the GPU that
served it was.

What each provider keeps also rests on its stated policy. Fireworks stores no prompts or
outputs for open models unless you opt in. Its Responses API, which the Codex entries use,
keeps a conversation for 30 days only when a request sets `store` to true, and Codex sends
`store: false`. OpenRouter stores no prompts or outputs unless you turn on its logging, and
it skips its own logging on the regional endpoints. The provider it routes to follows its own
policy, though. To route only to providers that keep nothing, turn on Zero Data Retention
enforcement in OpenRouter's privacy settings. The default file carries these notes as `//`
entries beside the backends.

| Model | Claude Code | Codex |
|---|---|---|
| GLM-5.3 | `glm53-claude-openrouter`, `glm53-claude-fireworks` | `glm53-codex-fireworks` (and `// glm53-codex-openrouter`, set aside) |
| DeepSeek V4.1 Flash | `deepseek41-claude-openrouter`, `deepseek41-claude-fireworks` | `deepseek41-codex-openrouter`, `deepseek41-codex-fireworks` |
| Kimi K3 | `kimik3-claude-openrouter`, `kimik3-claude-fireworks` | `kimik3-codex-openrouter`, `kimik3-codex-fireworks` |

**`glm53-codex-openrouter` is set aside**: its name starts with `//`, so nothing can select it.
Codex running GLM-5.3 through OpenRouter's US endpoint does not reliably return output matching
the requested schema: it can return an object with other keys, or prose. A plan-duel judge, a
plan-run worker whose result is read, or a review-panel lane (one of its two groups of agents)
on it would fail. The same model matches the schema through Fireworks under Codex, and through
Claude Code on both providers. So the fault lies in Codex reaching it through OpenRouter, not
in the model. Its entry keeps a `why` field saying so. To use it anyway, for
work whose output nothing reads, delete the `// ` before its name so it can be selected, and
delete the `why` field too: a backend with any field other than the five is refused, and the
whole file with it.

The default backends need `OPENROUTER_API_KEY` or `FIREWORKS_API_KEY` exported in the shell that
runs the skill. A backend whose key is not exported is refused before launch, naming the
variable. An agent with no backend keeps using its CLI's own sign-in either way. OpenRouter's US
endpoint needs a Business or Enterprise plan. Fireworks' US-only models cost 1.5 times its
standard price.

### Keeping the keys

The pack reads keys only from the environment. To help you export them, a default install
creates a key file beside `backends.json` when there is none: `keys.env` on Linux and macOS,
readable only by you, and `keys.ps1` on Windows, which takes your home folder's permissions.
When `PORTABLE_AGENT_SKILLS_BACKENDS` is set, the installer creates neither the key file nor the
`.gitignore`, since that path may be inside a repository of yours. Every key in it is empty:

```bash
export FIREWORKS_API_KEY=""
export OPENROUTER_API_KEY=""
```

Put each key between its quotes, then load the file from your shell startup file. The
installer never edits that file. The key file's own first lines show the line to add:

```bash
# in ~/.bashrc or ~/.zshrc
[ -f ~/.portable-agent-skills/keys.env ] && . ~/.portable-agent-skills/keys.env
```

On Windows the lines read `$env:OPENROUTER_API_KEY = ""`, and `$PROFILE` dot-sources `keys.ps1`,
which runs it in the current session so its variables stay set. A program started before the
line runs never sees the keys. A line left empty still sets its variable, to empty, over any
value exported before it. The installer never reads the key file, and never rewrites one that
exists.

The installer writes a `.gitignore` in `~/.portable-agent-skills` naming `keys.env` and
`keys.ps1`. If that folder already has a `.gitignore` of yours, it adds whichever of the two
names it does not already list, at the end, and changes nothing else in it. If that
`.gitignore` is a symbolic link, which git does not read, the installer leaves it alone and
creates no key file, and says why. So syncing that folder as a git repository leaves the keys
behind. Any other way of syncing it — a cloud drive, a copy — carries them with it; sync
`backends.json` alone.

## The shape

One JSON object. Each key is a backend's name, and each value has up to five fields:

| Field | Required | What it is |
|---|---|---|
| `harness` | yes | The CLI this backend is written for — `claude` or `codex`, as its program is named. |
| `model` | yes | The model id, exactly as the provider spells it. Nothing checks it against a list. |
| `args` | no | Arguments the harness takes on its command line: Codex's `-c` provider lines, or Claude's `--settings`. A list of strings. |
| `env` | no | Literal settings for the agent's environment, such as a provider's base URL. Name to string. **Never a secret**: these are written in the file. |
| `env_from_parent` | no | Credentials, by name only: maps the variable the harness reads to the variable in **your** shell that holds the key. |

Everything inside is a string and any other key is refused, so a typo fails when the file
is read, naming the backend and the field, rather than halfway through a paid run.

JSON has no comments, so a backend whose name begins with `//`, with nothing before it, is
treated as a comment. It is skipped whatever it holds, it cannot be selected, and it is not
listed among the defined backends. To set an entry aside, rename it `// name`. Give it an extra
field, such as `why`, to say why it was set aside.

`env_from_parent` is a mapping rather than a list because a harness reads one fixed variable
while each provider's key lives under its own. `{"ANTHROPIC_AUTH_TOKEN": "OPENROUTER_API_KEY"}`
gives the agent the value of your `OPENROUTER_API_KEY` under the name Claude Code reads. So
you export each key under its own name, and one run can put Claude on your own sign-in on one
side and on OpenRouter on the other.

A backend never says how an agent is launched. The command belongs to the skill that runs
the agent, because each role needs different flags. The backend fills two markers in that
command:

- `⟪model⟫` becomes the backend's `model` where it is a whole argument or a setting's
  whole value — `--model=⟪model⟫`, `model="⟪model⟫"` — the backend's own `args` included.
- `⟪backend_args⟫`, where it is a whole argument, becomes the backend's `args` — zero or
  more arguments.

Any other mention of a marker, such as one inside a review prompt, is text and is passed on
unchanged.

## How the launcher applies a backend

Agents are launched through `review_runner.py`, a small program in the `diff-review` skill that
starts each agent, watches it, and stops it if it hangs. The rest of this file calls it the
supervisor:

```text
python3 <diff-review skill dir>/review_runner.py --backend NAME [other flags] -- <agent argv>
```

With `--backend NAME` it:

1. reads the file and refuses a name that is not defined, listing the ones that are;
2. refuses the backend when its `harness` is not the program the agent command launches. A
   backend written for Claude sets variables Codex ignores. On a Codex command it would
   quietly reach Codex's default provider, while the run credited this backend's model. The
   check reads the program's name, so a CLI started through a wrapper (`npx …`, `node
   cli.js`) is refused. Launch the installed CLI by its own name or full path instead;
3. fills `⟪model⟫` and `⟪backend_args⟫`. It refuses a backend with `args` when the command has
   no `⟪backend_args⟫` and does not already carry those arguments, because the provider
   settings would be dropped. It refuses a command where no argument carries the model,
   because the run would credit a model that never ran. So the model must be on the command
   line. Setting it only through `env` or inside a CLI's settings file is refused, because the
   check cannot see it there;
4. refuses the run before launch when a variable named on the right of `env_from_parent`
   is unset or empty in your shell. The refusal names the entry by the variable the agent
   reads, never by the one in your shell. If you had pasted a key where the shell variable's
   name belongs, naming the entry by that variable would print the key;
5. gives the agent a **copy** of the supervisor's environment with `env` and
   `env_from_parent` merged on top, so `PATH` and everything else survive.

A caller supplying its own settings can pass `--env NAME=VALUE` and
`--env-from-parent CHILD=PARENT` instead, each repeatable. A name set twice, by any mix of
these, is refused. `--resolve-backend NAME`, on its own, prints the backend's fields as JSON
and exits. The pack's other programs read the file that way, so they do not parse it
themselves. They also pass `--backend-digest HEX` with every launch: a short fingerprint of
those fields taken when the run began. If the entry has been edited since, the supervisor
refuses the launch, naming the backend, and starts nothing.

A forwarded value is read when the agent starts and placed in its environment, and nowhere
else: not in the status line, the display log, the findings or verdict files, or an error
message. A key never goes in the command line, where anyone who can list processes can read it.

## Which skills take a backend

A backend reaches an agent only when the supervisor launches that agent as a program:

- `diff-review` — the different-model reviewer.
- `plan-duel` — each of the three roles. Every role is launched through the supervisor.
- `review-panel` — the workers in each lane (review-panel's name for each of its two groups
  of agents).
  Every worker is launched through the supervisor.
- `plan-run` — a phase worker, when Codex runs the plan unattended; only a `codex` backend fits.
- `security-review-codebase` — a deep-mode component reviewer, when Codex runs the review; only
  a `codex` backend fits.

It does not apply to an in-process sub-agent, one a host starts through its own tool, which
runs the host's own model. Nor does it apply to work a skill does in its own context. Where
`diff-review` is not installed and the host has no in-process sub-agent, `plan-run` runs the
phase in its own context and `security-review-codebase` reviews the components one after
another in its own context. Each says which path ran.

## Three ways a credential reaches the model

### 1. The harness's own sign-in

A subscription or SSO login that the CLI keeps for itself. This needs no backend at all: an
agent launched with no `--backend` runs on it. A backend naming only a model
keeps it too, and changes only the model:

```json
{
  "claude-other-model": { "harness": "claude", "model": "<model id>" }
}
```

You can use it alongside the other two ways. A backend changes only the agent it is attached
to, so one side of a duel can run on your sign-in while the other runs on a provider key.

### 2. An environment variable

You export the provider's key in the shell that runs the skill, and the backend names it in
`env_from_parent`. The pack stores nothing.

### 3. A gateway

The harness fetches a short-lived token itself by running a helper command, and no key sits
on the machine at all. The backend declares no credential field; it points the harness at the
gateway and names the helper through the harness's own setting:

- **Claude Code**: an `apiKeyHelper` in a settings file, passed with `--settings`. Claude Code
  runs the helper and uses what it prints, and runs it again on an interval.
- **Codex**: a `[model_providers.<id>.auth]` table with a `command` and a
  `refresh_interval_ms`. Codex does not allow it together with `env_key`.

```json
{
  "claude-gateway": {
    "harness": "claude",
    "model": "<model id>",
    "args": ["--settings", "/path/to/gateway-settings.json"],
    "env": {
      "ANTHROPIC_BASE_URL": "https://gateway.example.internal",
      "ANTHROPIC_API_KEY": "",
      "ANTHROPIC_AUTH_TOKEN": ""
    }
  },
  "codex-gateway": {
    "harness": "codex",
    "model": "<model id>",
    "args": [
      "-c", "model_provider=\"corp\"",
      "-c", "model_providers.corp.name=\"Corporate gateway\"",
      "-c", "model_providers.corp.base_url=\"https://gateway.example.internal/v1\"",
      "-c", "model_providers.corp.wire_api=\"responses\"",
      "-c", "model_providers.corp.auth.command=\"/path/to/token-helper\"",
      "-c", "model_providers.corp.auth.refresh_interval_ms=300000"
    ]
  }
}
```

where `gateway-settings.json` holds `{"apiKeyHelper": "/path/to/token-helper"}`. Both
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are set to empty, because Claude Code prefers
either over `apiKeyHelper`. Otherwise a key or token in your shell would be sent to the gateway
in place of the helper's. That is the rule in [Never send your own credential to another
provider](#never-send-your-own-credential-to-another-provider).

## A reviewer on a backend can still write files

The skills launch a reviewer with the CLI's own read-only flags. Neither CLI makes them a
guarantee, whatever model runs inside:

- **Claude Code**, `--permission-mode plan`: Claude Code's own permission check. The user's
  settings can widen it, and an allow rule for the shell lets a shell write through.
- **Codex**, `-s read-only`: the operating system refuses the shell's writes on Linux and macOS,
  less reliably on native Windows. Codex's own file-edit tool is checked only inside Codex.

Hooks, plugins and MCP servers you configured run outside both. Only running the reviewer on a
read-only copy of the tree is a guarantee.

## Never send your own credential to another provider

This is the one rule every recipe below follows, and the reason a backend with no credential
field is **not safe just because it holds no secret**.

There are two ways your own credential can reach a provider you pointed a harness at:

- **An inherited key.** The agent gets a copy of your environment, which is what keeps `PATH`.
  If `ANTHROPIC_API_KEY` is set in your shell, a Claude pointed at another provider inherits
  it and sends it there.
- **A stored login.** A harness signed in by subscription or SSO keeps that login itself. Given
  a new base URL and **no** credential, Claude Code falls back to that login and sends your
  token to whatever the base URL names. Codex, given a custom provider with no `env_key`,
  sends no credential at all — but a recipe does not rely on that either.

Setting the key variable to empty stops the first, but then the harness falls back to its
stored login, which is the second. So every recipe for a provider other than the harness's
own does both things: it **supplies the credential the harness will present to that
provider**, through `env_from_parent` or a gateway helper, and it sets to empty any other
variable the harness would prefer. The supervisor cannot do this for you: it
does not know which variables a harness treats as credentials, or which URLs belong to whom.

## Recipes

Keys below are placeholders: export your own under the named variable. Provider URLs and model
ids change; check each provider's current documentation before relying on one.

### Claude Code + OpenRouter

```json
{
  "claude-openrouter": {
    "harness": "claude",
    "model": "<openrouter model id>",
    "env": {
      "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
      "ANTHROPIC_API_KEY": ""
    },
    "env_from_parent": { "ANTHROPIC_AUTH_TOKEN": "OPENROUTER_API_KEY" }
  }
}
```

With `export OPENROUTER_API_KEY=sk-or-...` in your shell. `ANTHROPIC_API_KEY` is set to empty
because Claude Code would otherwise send your own key to OpenRouter.

### Claude Code + Fireworks

```json
{
  "claude-fireworks": {
    "harness": "claude",
    "model": "<fireworks model id>",
    "env": {
      "ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference",
      "ANTHROPIC_API_KEY": ""
    },
    "env_from_parent": { "ANTHROPIC_AUTH_TOKEN": "FIREWORKS_API_KEY" }
  }
}
```

With `export FIREWORKS_API_KEY=fw-...`.

### Codex + OpenRouter

Codex takes a provider as a block of settings. On the command line that is a run of `-c`
arguments declaring a **new** provider id: `openai`, `ollama` and `lmstudio` are reserved.
Codex accepts only providers that serve the Responses API, hence `wire_api`.

Codex offers the model its built-in web search tool on every request, and OpenRouter's US
endpoint refuses a request that carries it with a 403, "not available on region-specific
endpoints". The `web_search="disabled"` line removes the tool. The Fireworks recipe carries it
too, since a reviewer has no use for web search.

A provider can be given as one `-c` holding a TOML inline table instead of one `-c` per field:
`-c 'model_providers.openrouter={name="OpenRouter", base_url="...", wire_api="responses",
env_key="OPENROUTER_API_KEY"}'`. The default backends use that form.

```json
{
  "codex-openrouter": {
    "harness": "codex",
    "model": "<openrouter model id>",
    "args": [
      "-c", "model_provider=\"openrouter\"",
      "-c", "model_providers.openrouter.name=\"OpenRouter\"",
      "-c", "model_providers.openrouter.base_url=\"https://openrouter.ai/api/v1\"",
      "-c", "model_providers.openrouter.wire_api=\"responses\"",
      "-c", "model_providers.openrouter.env_key=\"OPENROUTER_API_KEY\"",
      "-c", "web_search=\"disabled\""
    ],
    "env_from_parent": { "OPENROUTER_API_KEY": "OPENROUTER_API_KEY" }
  }
}
```

Codex reads the key from the variable `env_key` names. The `env_from_parent` entry maps that
name to itself: it changes nothing in the agent's environment, but it makes the supervisor
refuse the run before launch when the key is not exported, instead of the provider refusing
it minutes in.

### Codex + Fireworks

```json
{
  "codex-fireworks": {
    "harness": "codex",
    "model": "<fireworks model id>",
    "args": [
      "-c", "model_provider=\"fireworks\"",
      "-c", "model_providers.fireworks.name=\"Fireworks\"",
      "-c", "model_providers.fireworks.base_url=\"https://api.fireworks.ai/inference/v1\"",
      "-c", "model_providers.fireworks.wire_api=\"responses\"",
      "-c", "model_providers.fireworks.env_key=\"FIREWORKS_API_KEY\"",
      "-c", "web_search=\"disabled\""
    ],
    "env_from_parent": { "FIREWORKS_API_KEY": "FIREWORKS_API_KEY" }
  }
}
```

Use a standard Fireworks API key. Codex does not accept Fire Pass keys.

### Using a backend in a command

The agent command carries the markers where the model and the provider arguments go:

```text
review_runner.py --backend claude-openrouter ... -- claude -p "<prompt>" --model ⟪model⟫ ⟪backend_args⟫ ...
review_runner.py --backend codex-fireworks ... -- codex exec -m ⟪model⟫ ⟪backend_args⟫ ...
```

## A backend is not a Codex profile

Codex has its own `--profile NAME` (and `-p NAME`), which selects a named set of Codex's own
settings. A backend is something else: an entry in this pack's file, applied by the supervisor
to whichever harness it names. `codex -p <backend>` does not select a backend. Neither
harness uses the word "backend" for anything, which is why this pack does.

## Rate limits

A provider key carries that provider's rate limit, not the harness's. A model that only one
provider serves in the US can be rate-limited when that provider's capacity, which many users
share, runs out.
OpenRouter then answers 429, "temporarily rate-limited upstream". Codex exits after its
retries, while Claude Code keeps retrying. To fix it, wait, or use the same model on the other
provider. A `review-panel` lane on a backend runs two workers at once by default (its `slots`
setting), and may need that lowered for a provider with a tighter limit.
