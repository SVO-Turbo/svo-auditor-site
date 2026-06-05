"""
URL validator with SSRF / DNS rebinding / private IP protection.

Every user-submitted URL passes through validate_url() before any
network request is made. Failure raises ValidationError.
"""
import ipaddress
import socket
import re
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
            raise ValidationError(f"Hostnames ending in '{suffix}' are not allowed.")


# Schemes we explicitly reject even when written without "://"
_DANGEROUS_SCHEMES = {"javascript", "data", "vbscript", "file", "ftp", "mailto", "tel", "blob"}


def validate_url(raw_url):
    """
    Validate and canonicalize a user-submitted URL.

    Returns a ValidatedURL on success. Raises ValidationError on any failure
    (bad scheme, malformed/blocked hostname, IP literal, DNS failure, or a
    hostname that resolves to a private/loopback/link-local address).
    """
    if not raw_url or not isinstance(raw_url, str):
        raise ValidationError("A URL is required.")

    candidate = raw_url.strip()
    if not candidate:
        raise ValidationError("A URL is required.")
    if len(candidate) > MAX_URL_LENGTH:
        raise ValidationError(f"URL exceeds {MAX_URL_LENGTH} characters.")

    # Resolve the scheme safely.
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", candidate)
    if m:
        pre_scheme = m.group(1).lower()
        if "://" in candidate:
            # Absolute URL like https://host — scheme must be allowed.
            if pre_scheme not in ALLOWED_SCHEMES:
                raise ValidationError(
                    f"URL scheme '{pre_scheme}' is not allowed. Use http or https."
                )
        else:
            # "scheme:" with no // — reject dangerous schemes (javascript:, data:, …).
            # Otherwise treat it as a schemeless host[:port] the user typed.
            if pre_scheme in _DANGEROUS_SCHEMES:
                raise ValidationError(
                    f"URL scheme '{pre_scheme}' is not allowed. Use http or https."
                )
            candidate = "https://" + candidate
    else:
        candidate = "https://" + candidate

    # Strip embedded credentials (user:pass@) before parsing.
    candidate = _strip_credentials(candidate)

    parsed = urlparse(candidate)

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValidationError(
            f"URL scheme '{scheme or '(none)'}' is not allowed. Use http or https."
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValidationError("URL has no hostname.")

    # Reject bare IP-literal hosts (v4 or v6) — require real domain names.
    is_ip_literal = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        raise ValidationError("Provide a domain name, not an IP address.")

    _validate_hostname_format(hostname)

    # Guard against a non-numeric port (urlparse raises on access).
    try:
        parsed_port = parsed.port
    except ValueError:
        raise ValidationError("URL contains an invalid port.")

    # Resolve DNS, then apply the private-IP / SSRF filter to the result.
    try:
        resolved_ip = socket.gethostbyname(hostname)
    except (socket.gaierror, socket.herror, UnicodeError, OSError):
        raise ValidationError(f"Could not resolve hostname '{hostname}'.")

    if _is_private_ip(resolved_ip):
        raise ValidationError("URL resolves to a private or disallowed IP address.")

    port = parsed_port or DEFAULT_PORTS[scheme]
    path = parsed.path or "/"

    if parsed_port and parsed_port not in (80, 443):
        netloc = f"{hostname}:{parsed_port}"
    else:
        netloc = hostname

    # Canonical URL: drop params, query, and fragment.
    canonical = urlunparse((scheme, netloc, path, "", "", ""))

    return ValidatedURL(
        url=canonical,
        scheme=scheme,
        hostname=hostname,
        port=port,
        path=path,
        resolved_ip=resolved_ip,
    )
