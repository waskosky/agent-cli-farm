# Memory protection choice during setup

The user requested an optional setup question explaining the benefits and costs
of the already implemented gentle memory policy. This changes installation
discovery and consent, not the resource policy itself. Existing authorization
covers implementation, verification, publication, and merging to main.

## Choice and explanation

After installing helpers, interactive setup asks whether to enable gentle memory
settings for future managed jobs. The default is no. The explanation precedes
the question and describes:

- Shared 1 GiB protection for memory already used by interactive agents, where
  the Linux/systemd host layer is available. It is not reserved free RAM or OOM
  immunity, and full hierarchical protection needs the separate root step.
- Relative CPU/I/O preference for interactive agents. Batch jobs receive lower
  priority under contention, so builds can take longer.
- New managed background jobs wait at 1024 MiB available headroom or critical
  stalls, recover at 1536 MiB for 30 seconds, and time out after five minutes.
  Running agents are never stopped by those thresholds. No default resource
  ceilings are introduced.
- Declining or pressing Enter preserves existing resource settings. It is not
  an uninstall or a request to disable previously enabled options.

Accepting sets only `protect_agents` and `queue_background` to true through the
existing validated private settings API. Every other setting, including model,
investigator, and automatic-action consent, is preserved. No model is invoked.

Questions require both stdin and stdout to be terminals. Piped input, redirected
output, CI, and an unavailable terminal must not hang or consume scripted input.
EOF defaults to no; invalid interactive answers are retried with a short hint.

`--with-memory-protection` explicitly enables these two user preferences without
the first question, including unattended setup. `--without-memory-protection`
skips the entire feature and preserves settings. Conflicting memory flags fail
before installation. Ordinary unattended setup remains inert.

## Separate host step

On Linux with the required fixed system executables and a positive non-root UID,
interactive acceptance also offers a separately consented host step. Print the
existing standalone helper's complete read-only plan for the current UID with
`--with-maintenance`, then ask whether to apply it with sudo, default no.

Explain that this step adds dedicated hierarchical MemoryLow preferences and a
30-second root timer that maintains relative OOM preferences for future managed
jobs. Named farm slice drop-ins also affect other accounts using those slices;
OOM changes are limited to the configured UID. Reclaim can favor these workloads
over others. The helper adds no ceilings and never moves/restarts existing agents.
It preserves stronger preferences and all operator masks. Existing processes are
not selected by name and no `--agent` targets are invented by setup.

Use exact argv with the fixed `/usr/bin/python3 -I`, the installed standalone
helper path, `apply --uid UID --with-maintenance`, and sudo. No shell evaluation
or interpreter/root-path environment override is introduced. Host apply is never
automatic in unattended setup, even with the user-preference enable flag.

If the host step is declined, unsupported, or its plan is unavailable, clearly
state that setup has not applied system protection and display the exact manual
plan/apply commands where supported. Never run sudo after failed preview. If
an explicitly accepted host apply fails, return nonzero and explain that user
preferences were saved but the host operation failed. Do not remove/reinstall
an existing differing host configuration or unmask anything to recover.

## Structure and verification

Keep `setup.sh` responsible for flags, copying helpers, and invoking the setup
controller with its selected Python. A small `codex_looper.resource_setup`
module handles explanation, prompts, configuration, preview, and application;
an installed `bin/codex-setup-memory.py` entry point locates the package like the
existing resource CLI. No policy modules or root helper behavior change.

Tests use private HOME/XDG directories, synthetic input and real terminal
fixtures, and subprocess doubles/private fake helpers for host preview/apply.
No test touches real systemd, sudo privileges, running agents, or login state.
Verify affirmative/negative/default/EOF/invalid answers, both terminal checks,
unattended default and explicit flags, unrelated settings preservation, private
permissions, exact preview/apply argv, no sudo without second consent, failed
preview/apply, unsupported/root accounts, and sourced setup isolation. Required
repository checks and exact-head CI gate publication and merging.
