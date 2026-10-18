"""Repository exam resolution: which commands decide whether a change passes.

The exam lives in the project repository (``.code-loop/verify.json``) and is
read through the platform adapter at the base commit the observation reports as
head. No PR head reaches this module, so a pull request cannot alter the exam it
is judged by; merging is the only way a new exam takes effect, and merging moves
the base commit, which re-versions every item through its payload. The machine
configuration stays as the fallback track while projects migrate.
"""
from __future__ import annotations

from .runtime import EXAM_PATH, parse_exam

# Exam guard (deterministic, server-assembled): appended to reviewer verdict
# bodies when the platform reports the PR diff touching the exact exam path.
GUARD_NOTE = ('> ⚠️ 考题护栏：本 PR 改动了 %s（review 验证标准）。考题按 base commit 只读，'
              '本 PR 不影响自身验证；合并后新考题对后续所有轮次生效，请人工确认。' % EXAM_PATH)


def resolve(platform, cached, sha):
    """Exam record for one base commit, reusing the cached record for the same
    commit id (zero network). Malformed content or a read failure raises: a
    broken exam must stall the sync loudly instead of silently falling back to
    the machine track the repository exam was meant to replace."""
    if isinstance(cached, dict) and cached.get('base_sha') == sha:
        return cached
    body = platform.raw_file(EXAM_PATH, sha)
    if body is None:
        return {'base_sha': sha, 'source': 'machine'}
    return dict(parse_exam(body), base_sha=sha, source='repo')


def overlay(record):
    """Policy values the repository exam supplies, empty for the machine track.

    Callers read these values instead of a mutated config, so the machine track
    stays live for a caller that adjusts its config after construction. The
    crawl needs no counterpart: platform facts (including PR statuses) are
    always observed, and only the gate consults required_checks.
    """
    if not isinstance(record, dict) or record.get('source') != 'repo':
        return {}
    values = {'verification': record.get('verification') or [],
              'required_checks': record.get('required_checks') or []}
    # 考题声明了加速层才透传——未声明的项目 policy 键集合逐字节不变。
    if record.get('verification_fast'):
        values['verification_fast'] = record['verification_fast']
    return values
