"""Sender domain rules use actual From addresses, never brand mentions in text."""
from email.utils import getaddresses
import re


def normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Domain must be text")
    try:
        domain = value.strip().rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError("Invalid domain") from None
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if len(domain) > 253 or not re.fullmatch(label + r'(?:\.' + label + r')+', domain):
        raise ValueError("Invalid domain")
    return domain


def sender_is_excluded(sender: str, domains) -> bool:
    if not domains:
        return False
    rules = tuple(normalize_domain(domain) for domain in domains)
    for _, address in getaddresses([sender]):
        _, separator, domain = address.rpartition('@')
        if not separator:
            continue
        try:
            domain = normalize_domain(domain)
        except ValueError:
            continue
        if any(domain == rule or domain.endswith('.' + rule) for rule in rules):
            return True
    return False
