from dataclasses import dataclass


@dataclass(frozen=True)
class MailMessage:
    account_id: str
    uidvalidity: int
    uid: int
    message_id: str
    sender: str
    subject: str
    date: str
    body: str


@dataclass(frozen=True)
class PollResult:
    uidvalidity: int
    last_uid: int
    messages: list[MailMessage]
    epoch_changed: bool = False
