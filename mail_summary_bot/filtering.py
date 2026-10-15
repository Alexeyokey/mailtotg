"""Sender rules use one actual From address, never brand mentions in text."""
from email import policy
from email.errors import ObsoleteHeaderDefect
import re


def normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Domain must be text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Invalid domain")
    try:
        domain = value.strip().removesuffix('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError("Invalid domain") from None
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if len(domain) > 253 or not re.fullmatch(label + r'(?:\.' + label + r')+', domain):
        raise ValueError("Invalid domain")
    return domain


def normalize_address(value: str) -> str:
    """Normalize a bare, unambiguous address suitable for an exact rule.

    Deliberately accept dot-atom local parts only; display names, address lists,
    quoted local parts, and control characters must not create broader rules.
    """
    if not isinstance(value, str):
        raise ValueError("Address must be text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Invalid address")
    address = value.strip()
    local, separator, domain = address.rpartition('@')
    atom = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+"
    if not separator or len(local) > 64 or not re.fullmatch(atom + r'(?:\.' + atom + r')*', local):
        raise ValueError("Invalid address")
    # Whitespace is allowed around the user's input, never inside an addr-spec.
    if domain != domain.strip():
        raise ValueError("Invalid address")
    address = local.lower() + '@' + normalize_domain(domain)
    if len(address) > 254:
        raise ValueError("Invalid address")
    return address


def sender_address(sender: str) -> str | None:
    """Return the sole actual From mailbox, or None for unsafe/ambiguous input."""
    if not isinstance(sender, str) or not sender.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in sender):
        return None
    try:
        header = policy.default.header_factory('From', sender)
        # Dots in an unquoted display name are common in real From headers.
        # Obsolete syntax is still unambiguous; actual parsing defects are not.
        if any(not isinstance(defect, ObsoleteHeaderDefect) for defect in header.defects) or len(header.addresses) != 1:
            return None
        if any(group.display_name is not None for group in header.groups):
            return None
        return normalize_address(header.addresses[0].addr_spec)
    except (ValueError, IndexError, TypeError):
        return None


def sender_is_excluded(sender: str, domains, addresses=()) -> bool:
    if not domains and not addresses:
        return False
    rules = tuple(normalize_domain(domain) for domain in domains)
    exact = tuple(normalize_address(address) for address in addresses)
    address = sender_address(sender)
    if address is None:
        return False
    domain = address.rsplit('@', 1)[1]
    return address in exact or any(domain == rule or domain.endswith('.' + rule) for rule in rules)
