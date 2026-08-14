"""
Connection data model for the Qizhi (齐治) SSH Bridge.

This module defines the canonical dataclass used by all components to describe
an SSH (or Telnet) session initiated by the 齐治堡垒机 console.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ConnectionParams:
    """Parameters required to establish a remote shell session.

    Attributes:
        host: Target server hostname or IP address.
        port: Target server port (default 22 for SSH).
        username: Remote login username.
        password: Remote login password, if available. May be empty when a key is used.
        protocol: Connection protocol flavour, one of "ssh2", "ssh1", "telnet".
        key_path: Path to a private key file, if key-based auth is requested.
        bastion_host: Optional intermediate jump host / bastion host.
        extra: Catch-all for raw, unknown, or gateway-specific arguments.
    """

    host: str
    port: int
    username: str
    password: Optional[str] = None
    protocol: str = "ssh2"  # ssh2, ssh1, telnet
    key_path: Optional[str] = None
    bastion_host: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize common input mistakes."""
        self.host = (self.host or "").strip()
        self.username = (self.username or "").strip()
        self.protocol = (self.protocol or "ssh2").strip().lower()
        if self.password is not None:
            self.password = self.password.strip() or None
        if self.key_path is not None:
            self.key_path = self.key_path.strip() or None
        if self.bastion_host is not None:
            self.bastion_host = self.bastion_host.strip() or None
        if not isinstance(self.port, int):
            try:
                self.port = int(self.port)
            except (TypeError, ValueError):
                self.port = 22

        # Default fallback for safety; callers should still validate.
        if not self.port:
            self.port = 22

    def is_valid(self) -> bool:
        """Return True when the absolute minimum required fields are present."""
        return bool(self.host and self.username and self.port > 0)

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Serialize to a flat dictionary (password intentionally masked)."""
        return {
            "host": self.host,
            "port": str(self.port),
            "username": self.username,
            "password": "***" if self.password else None,
            "protocol": self.protocol,
            "key_path": self.key_path,
            "bastion_host": self.bastion_host,
            "extra": str(self.extra),
        }
