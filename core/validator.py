"""
URL validator with SSRF / DNS rebinding / private IP protection.

Every user-submitted URL passes through validate_url() before any
network request is made. Failure raises ValidationError.
"""
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


class ValidationError(Exception):
    """Raised when a URL fails any validation check."""
    pass


@dataclass
class ValidatedURL:
    """Container for a validated URL and its resolved metadata."""
    url: str           # Canonical URL (scheme://hostname[:port]/path)
    scheme: str        # http or https
    hostname: str      # lowercase hostname
    port: int          # port number (default 80/443)
    path: str          # path component
    resolved_ip: str   # IP that the hostname resolved to


# ─── Configuration ───────────────────────────────────────────
MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253
MAX_LABEL_LENGTH = 63
ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_PORTS = {"http": 80, "https": 443}

# Hostname blocklist — exact match or suffix match
BLOCKED_HOSTNAMES = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
}
BLOCKED_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".private",
    ".test",
    ".example",
    ".invalid",
)


def _strip_credentials(url):
    """Remove user:pass@ from URLs to prevent confusion attacks."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        # Rebuild without userinfo
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))
    return url


def _is_private_ip(ip_str):
    """Block all non-public IP ranges including AWS metadata service."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # If we can't parse it, block it

    # Catches: private (10/8, 172.16/12, 192.168/16), loopback (127/8),
    # link-local (169.254/16 including 169.254.169.254 AWS metadata),
    # multicast, reserved, unspecified, etc.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_hostname_format(hostname):
    """Check hostname length, label length, and character set."""
    if not hostname:
        raise ValidationError("Hostname is empty.")

    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise ValidationError(f"Hostname exceeds {MAX_HOSTNAME_LENGTH} characters.")

    # Block IP-literal hostnames at the format stage — DNS resolution
    # will handle them separately and apply the private-IP filter.
    labels = hostname.split(".")
    for label in labels:
        if not label:
            raise ValidationError("Hostname contains empty label.")
        if len(label) > MAX_LABEL_LENGTH:
            raise ValidationError(f"Hostname label exceeds {MAX_LABEL_LENGTH} characters.")
        if label.startswith("-") or label.endswith("-"):
            raise ValidationError("Hostname label cannot start or end with a hyphen.")

    if hostname in BLOCKED_HOSTNAMES:
        raise ValidationError(f"Hostname '{hostname}' is not allowed.")

    for suffix in BLOCKED_SUFFIXES:
        if hostname.endswith(suffix):
            raise ValidationError(f"Hostnames ending in '{suff
