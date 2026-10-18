"""Backward-compatibility shim: aliases GitHub adapter as Gitea."""
from .github import GitHub, GitHubError

Gitea = GitHub
GiteaError = GitHubError

__all__ = ['GitHub', 'GitHubError', 'Gitea', 'GiteaError']
