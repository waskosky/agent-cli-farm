from __future__ import annotations

import json
from string import Formatter

from .models import AgentConfig, CommandContext, CommandTemplateError


def render_template(parts: list[str], context: CommandContext) -> list[str]:
    values = context.as_format_dict()
    rendered: list[str] = []
    formatter = Formatter()
    for part in parts:
        for _, field_name, _, _ in formatter.parse(part):
            if field_name and field_name not in values:
                raise CommandTemplateError(f"unknown command placeholder: {{{field_name}}}")
        try:
            rendered.append(part.format(**values))
        except KeyError as exc:
            raise CommandTemplateError(f"unknown command placeholder: {{{exc.args[0]}}}") from exc
    return rendered


def agent_extra_args(agent: AgentConfig) -> list[str]:
    args = list(agent.extra_args)
    if agent.model:
        args.extend(["--model", agent.model])
    if agent.effort:
        if agent.kind == "codex":
            args.extend(
                ["--config", f"model_reasoning_effort={json.dumps(agent.effort)}"]
            )
        elif agent.kind == "claude":
            args.extend(["--effort", agent.effort])
    return args


def build_command(
    *,
    agent: AgentConfig,
    context: CommandContext,
    is_first_prompt_in_session: bool,
) -> list[str]:
    custom_template = agent.first_command if is_first_prompt_in_session else agent.resume_command
    if custom_template:
        return render_template(custom_template, context)

    if agent.kind == "claude":
        base = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        base.extend(agent_extra_args(agent))
        if is_first_prompt_in_session:
            return [*base, "--name", context.session, context.prompt]
        return [*base, "--resume", context.session, context.prompt]

    if agent.kind == "codex":
        base = ["codex", "exec", "--json"]
        base.extend(agent_extra_args(agent))
        if is_first_prompt_in_session:
            return [*base, context.prompt]
        if context.session_id:
            return [*base, "resume", context.session_id, context.prompt]
        return [*base, "resume", "--last", context.prompt]

    if agent.kind == "generic":
        if not agent.first_command:
            raise CommandTemplateError(
                f"generic agent {agent.name!r} needs first_command in agent-looper.toml"
            )
        template = agent.first_command if is_first_prompt_in_session else agent.resume_command
        return render_template(template or agent.first_command, context)

    raise CommandTemplateError(f"unsupported agent kind: {agent.kind}")
