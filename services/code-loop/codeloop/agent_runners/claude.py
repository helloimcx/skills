"""Claude Code plugin: the registered spawn command, named.

Claude Code has no models-list command, so this plugin has no picker — a
model sent next to it can only be a mistake, refused loudly. Its argv is
``None``: "use the machine's registered spawn command", declared explicitly
rather than fallen through an ``else``. The registered command *is* the
machine's default agent (``trigger.DEFAULT_COMMAND`` is only the config
default), so naming this plugin asks for exactly that — a plugin that
hardcoded ``claude -p --dangerously-skip-permissions`` would silently
override a machine registered to something else.
"""
from __future__ import annotations

from . import base, claude_transcript


class ClaudeRunner(base.Runner):
    id = 'claude'
    label = 'claude'
    sub = 'Claude Code'
    models_command = None
    supports_subagents = True

    def coordinator_flags(self, task_type=None):
        if task_type == 'pr.repair':
            return ['--tools', 'Task', '--output-format', 'stream-json', '--verbose']
        if task_type == 'worker.project_diagnose':
            return ['--tools', 'Read,Glob,Grep']
        raise ValueError('TASK_TYPE_UNKNOWN: %s' % task_type)

    def argv(self, model=None, which_fn=None, run_fn=None):
        if model is not None:
            raise ValueError('INVALID_COMMAND: model not supported for agent %s'
                             % self.id)
        return None

    # -- output face: the claude transcript under ~/.claude/projects ---------
    def resolve_session(self, repo_cwd, exec_id, created_at):
        return claude_transcript.resolve_session(repo_cwd, exec_id, created_at)

    def tail(self, handle, offset=0, limit=None):
        return claude_transcript.tail(handle, offset=offset, limit=limit)


RUNNER = ClaudeRunner()
