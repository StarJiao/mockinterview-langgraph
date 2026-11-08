"""Ownership convention shared by the CLI and the Web UI.

Sessions created from the command line belong to a fixed user so that both
frontends can share one data directory without leaving sessions unowned.
"""

CLI_USER = "cli"
