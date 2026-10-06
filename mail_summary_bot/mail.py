"""Read-only IMAP polling and bounded, plain-text MIME extraction."""

from __future__ import annotations

import imaplib
import base64
import os
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .config import AccountConfig, ServiceConfig
from .models import MailMessage, PollResult


RAW_TRUNCATION_NOTICE = "[Письмо ограничено по размеру; часть содержимого не загружена.]"
BODY_TRUNCATION_NOTICE = "[Текст письма сокращён.]"


class MailReadError(RuntimeError):
    """An IMAP failure with a safe message (never a server authentication reply)."""


def _mailbox_argument(value: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise MailReadError("Invalid IMAP mailbox name.")
    # Preserve ASCII wire names from LIST; encode human-readable Unicode names.
    wire = value
    if not value.isascii():
        parts, encoded = [], []

        def flush():
            if encoded:
                data = "".join(encoded).encode("utf-16-be")
                parts.append("&" + base64.b64encode(data).decode("ascii").rstrip("=").replace("/", ",") + "-")
                encoded.clear()

        for char in value:
            if 32 <= ord(char) <= 126:
                flush()
                parts.append("&-" if char == "&" else char)
            else:
                encoded.append(char)
        flush()
        wire = "".join(parts)
    if wire.upper() == "INBOX":
        return wire
    return '"' + wire.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_bytes(value: bytes, charset: str | None = None) -> str:
    try:
        return value.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return value.decode("utf-8", errors="replace")


def _header(value: object) -> str:
    pieces: list[str] = []
    for piece, charset in decode_header(str(value or "")):
        pieces.append(_decode_bytes(piece, charset) if isinstance(piece, bytes) else piece)
    return re.sub(r"[\x00-\x1f\x7f]+", " ", "".join(pieces)).strip()


class _PlainHTML(HTMLParser):
    _hidden = {"script", "style", "head", "template", "noscript", "iframe", "object"}
    _blocks = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden:
            self.hidden_stack.append(tag)
        elif not self.hidden_stack and tag in self._blocks:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.hidden_stack and tag in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_stack:
            if tag in self.hidden_stack:
                # Also recover from malformed nesting inside a hidden element.
                del self.hidden_stack[self.hidden_stack.index(tag):]
        elif tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_stack:
            self.parts.append(data)


def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [re.sub(r"[\t \f\v]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _has_plain(part: Message) -> bool:
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return False
    if part.get_content_type() == "text/plain":
        return True
    if part.get_content_maintype() == "multipart":
        return any(_has_plain(child) for child in part.get_payload())
    return False


def _body(part: Message) -> str:
    # Text attachments (including inline files) must never become the summary body.
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return ""
    if part.get_content_maintype() == "message":
        return ""
    if part.is_multipart():
        children = part.get_payload()
        if not isinstance(children, list):
            return ""
        if part.get_content_subtype() == "alternative":
            for child in sorted(children, key=lambda p: not _has_plain(p)):
                text = _body(child)
                if text.strip():
                    return text
            return ""
        return "\n\n".join(text for child in children if (text := _body(child)).strip())
    content_type = part.get_content_type()
    if content_type not in {"text/plain", "text/html"}:
        return ""
    payload = part.get_payload(decode=True)
    text = _decode_bytes(payload or b"", part.get_content_charset())
    if content_type == "text/html":
        parser = _PlainHTML()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    return text


def _date(value: str, fallback: str) -> str:
    for candidate in (value, fallback):
        try:
            parsed = parsedate_to_datetime(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return ""


def _fetch_records(data: list):
    """Group IMAP metadata fragments, keeping literal contents separate.

    imaplib returns each literal as (prefix, bytes), followed by a separate
    trailer. UID/SIZE can occur in that trailer. A new sequence-number prefix
    begins another FETCH response, including an unsolicited one.
    """
    headers: list[bytes] = []
    literals: list[tuple[bytes, bytes]] = []
    for item in data:
        header = item[0] if isinstance(item, tuple) and item else item
        if not isinstance(header, bytes):
            continue
        if re.match(rb"^\d+\s+\(", header):
            if headers:
                yield b" ".join(headers), literals
            headers, literals = [], []
        elif not headers:
            continue
        headers.append(header)
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            literals.append((header, item[1]))
    if headers:
        yield b" ".join(headers), literals


def _fetch_number(header: bytes, name: bytes) -> int | None:
    # Quoted strings belong to values (e.g. dates/envelopes), not attributes.
    attributes = re.sub(rb'"(?:[^"\\]|\\.)*"', b'""', header)
    found = re.search(rb"\b" + re.escape(name) + rb"\s+(\d+)\b", attributes, re.I)
    return int(found.group(1)) if found else None


def parse_message(
    raw: bytes,
    *,
    account_id: str,
    uidvalidity: int,
    uid: int,
    max_body_chars: int,
    internaldate: str = "",
    truncated: bool = False,
) -> MailMessage:
    """Extract displayable text without fetching links or decoding attachments."""
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    body = _clean_text(_body(parsed))
    notices = [RAW_TRUNCATION_NOTICE] if truncated else []
    raw_notice_size = len("\n\n".join(notices)) + (2 if notices and body else 0)
    if len(body) + raw_notice_size > max_body_chars:
        notices.append(BODY_TRUNCATION_NOTICE)
    if notices:
        suffix = "\n\n".join(notices)
        if len(suffix) > max_body_chars:
            suffix = "[Сокращено]" if max_body_chars >= len("[Сокращено]") else "…"
        available = max(0, max_body_chars - len(suffix) - 2)
        body = "\n\n".join([body[:available].rstrip(), suffix]).strip()
    return MailMessage(
        account_id=account_id,
        uidvalidity=uidvalidity,
        uid=uid,
        message_id=_header(parsed.get("Message-ID")),
        sender=_header(parsed.get("From")),
        subject=_header(parsed.get("Subject")),
        date=_date(str(parsed.get("Date", "")), internaldate),
        body=body,
    )


class MailReader:
    def __init__(self, account: AccountConfig, settings: ServiceConfig):
        self.account = account
        self.settings = settings
        if min(settings.max_messages_per_poll, settings.max_body_chars, settings.max_email_bytes) <= 0:
            raise MailReadError("Mail reader limits must be positive.")

    @staticmethod
    def _request(client: imaplib.IMAP4_SSL, command: str, *args: object) -> list:
        try:
            status, data = client.uid(command, *args)
        except Exception:
            raise MailReadError("IMAP request failed.") from None
        if status != "OK" or data is None:
            raise MailReadError("IMAP request failed.")
        return data

    def _uids(self, client: imaplib.IMAP4_SSL, cursor: int, lookback: bool) -> list[int]:
        args: list[object] = [None, "UID", f"{cursor + 1}:*"]
        if lookback:
            since = datetime.now(timezone.utc) - timedelta(days=self.settings.lookback_days)
            # IMAP date tokens must use English month names regardless of locale.
            months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
            args.extend(["SINCE", f"{since.day:02d}-{months[since.month - 1]}-{since.year}"])
        data = self._request(client, "search", *args)
        try:
            found = {int(token) for item in data if isinstance(item, bytes) for token in item.split()}
        except ValueError:
            raise MailReadError("Invalid IMAP search response.") from None
        # A reversed range N:* can include the mailbox's highest UID when N is
        # above it. Filtering is necessary even though SEARCH has a UID criterion.
        return sorted(uid for uid in found if uid > cursor)

    @staticmethod
    def _metadata(data: list, uid: int) -> tuple[int, str]:
        for header, _ in _fetch_records(data):
            match_uid = _fetch_number(header, b"UID")
            size = _fetch_number(header, b"RFC822.SIZE")
            date = re.search(rb'\bINTERNALDATE\s+"([^"]+)"', header, re.I)
            if match_uid == uid and size is not None:
                return size, _decode_bytes(date.group(1), "ascii") if date else ""
        raise MailReadError("Incomplete IMAP message metadata.")

    def _message(self, client: imaplib.IMAP4_SSL, epoch: int, uid: int) -> MailMessage:
        metadata = self._request(client, "fetch", str(uid), "(UID INTERNALDATE RFC822.SIZE)")
        size, internaldate = self._metadata(metadata, uid)
        # Partial PEEK is used even for small messages: a stale or incorrect SIZE
        # must not trigger an unbounded attachment download. Never fall back to
        # an unrestricted FETCH if a server rejects partial fetching.
        limit = self.settings.max_email_bytes
        data = self._request(
            client, "fetch", str(uid), f"(UID INTERNALDATE RFC822.SIZE BODY.PEEK[]<0.{limit}>)"
        )
        raw = None
        for response, literals in _fetch_records(data):
            if _fetch_number(response, b"UID") != uid:
                continue
            for prefix, payload in literals:
                if re.search(rb"\bBODY(?:\.PEEK)?\[\](?:<0>)?(?:\s|$)", prefix, re.I):
                    raw = payload
                    break
            if raw is not None:
                # Providers may return a different RFC822.SIZE with the body
                # than with the preceding metadata request. Use the size paired
                # with this literal, retaining the earlier one only as fallback.
                current_size = _fetch_number(response, b"RFC822.SIZE")
                if current_size is not None:
                    size = current_size
                current_date = re.search(rb'\bINTERNALDATE\s+"([^"]+)"', response, re.I)
                if current_date:
                    internaldate = _decode_bytes(current_date.group(1), "ascii")
                break
        if raw is None:
            raise MailReadError("IMAP message content was not returned.")
        if len(raw) < min(size, limit):
            raise MailReadError("IMAP message content was incomplete.")
        truncated = size > limit or len(raw) > limit
        return parse_message(
            raw[:limit], account_id=self.account.id, uidvalidity=epoch, uid=uid,
            max_body_chars=self.settings.max_body_chars, internaldate=internaldate,
            truncated=truncated,
        )

    def poll(self, checkpoint: tuple[int, int] | None) -> PollResult:
        client = None
        try:
            mailbox = _mailbox_argument(self.account.mailbox)
            password = os.environ[self.account.password_env]
            client = imaplib.IMAP4_SSL(
                self.account.host, self.account.port,
                ssl_context=ssl.create_default_context(), timeout=self.account.timeout_seconds,
            )
            status, _ = client.login(self.account.username, password)
            if status != "OK":
                raise MailReadError("IMAP authentication failed.")
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise MailReadError("IMAP mailbox could not be selected.")
            _, data = client.response("UIDVALIDITY")
            epoch = int(data[0]) if data and data[0] is not None else 0
            if epoch <= 0:
                raise MailReadError("IMAP UIDVALIDITY is missing or invalid.")
            epoch_changed = checkpoint is not None and checkpoint[0] != epoch
            bootstrap = checkpoint is None or epoch_changed
            cursor = 0 if bootstrap else checkpoint[1]
            # Renumbering invalidates the old UID cursor. Recover recent mail even
            # when the initial setup was configured to ignore existing messages.
            lookback = epoch_changed or (bootstrap and self.settings.bootstrap == "lookback")
            # Capture the empty-lookback boundary before the date search. A mail
            # arriving between the two searches must remain eligible next poll.
            boundary = max(self._uids(client, 0, False), default=0) if lookback else 0
            uids = self._uids(client, cursor, lookback)
            if checkpoint is None and self.settings.bootstrap == "new":
                return PollResult(epoch, max(uids, default=0), [], epoch_changed=False)
            messages = []
            for uid in uids[:self.settings.max_messages_per_poll]:
                messages.append(self._message(client, epoch, uid))
                cursor = uid
            # An empty lookback should still establish today's boundary. Without
            # it, the next ordinary poll would import the entire old mailbox.
            if lookback and not uids:
                cursor = boundary
            return PollResult(epoch, cursor, messages, epoch_changed=epoch_changed)
        except MailReadError:
            raise
        except KeyError:
            raise MailReadError("IMAP password environment variable is missing.") from None
        except Exception:
            # IMAP exceptions may contain raw server replies or credentials.
            raise MailReadError("IMAP connection or message processing failed.") from None
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
