"""Private Telegram mailbox views. Callback data contains only local IDs."""
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from .filtering import normalize_address, normalize_domain, sender_address
from .telegram import TelegramError

PAGE_SIZE = 6
MENU = {"keyboard": [["📥 Почта", "🚫 Заблокированные"], ["⚙️ Чёрный список"]],
        "resize_keyboard": True, "is_persistent": True}
COMMANDS = [
    {"command": "mail", "description": "Почта: открыть сохранённые письма"},
    {"command": "blocked", "description": "Заблокированные письма"},
    {"command": "blacklist", "description": "Управление чёрным списком"},
    {"command": "block", "description": "Заблокировать адрес или домен"},
    {"command": "unblock", "description": "Разблокировать адрес или домен"},
    {"command": "status", "description": "Состояние почтовых ящиков"},
    {"command": "help", "description": "Как пользоваться ботом"},
    {"command": "cancel", "description": "Отменить ввод правила"},
]


def clean(value, limit=100):
    text = ''.join(c for c in str(value) if unicodedata.category(c) not in {'Cc', 'Cf'})
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def button(text, data):
    return {"text": text, "callback_data": data}


def navigation():
    return [[button('📥 Почта', 'mb:i:0'), button('🚫 Заблокированные', 'mb:b:0')],
            [button('⚙️ Чёрный список', 'mb:r:0')]]


def notification_keyboard(mail_id):
    return {"inline_keyboard": [[button('📖 Читать', f'mb:v:{mail_id}:i:0:0'),
                                 button('🚫 Заблокировать отправителя', f'mb:block:{mail_id}')],
                                [button('🚫 Заблокированные письма', 'mb:b:0'),
                                 button('⚙️ Чёрный список', 'mb:r:0')]]}


class MailboxUI:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.telegram = service.telegram

    def authorized(self, message, actor=None):
        if not isinstance(message, dict):
            return False
        chat = message.get('chat')
        actor = message.get('from') if actor is None else actor
        return (isinstance(chat, dict) and isinstance(actor, dict)
                and chat.get('type') == 'private'
                and str(chat.get('id')) == str(self.telegram.chat_id)
                and str(actor.get('id')) == str(self.telegram.chat_id)
                and not actor.get('is_bot', False))

    def show(self, text, rows=None, *, edit_id=None, mail_id=None):
        markup = {'inline_keyboard': rows if rows is not None else navigation()}
        if edit_id is not None:
            # Invalidate the old reply target before changing visible content.
            self.store.forget_telegram_mail(self.telegram.chat_id, edit_id)
            try:
                self.telegram.edit_text(edit_id, text, reply_markup=markup)
                tg_id = edit_id
            except TelegramError as error:
                # A deleted/old menu can be replaced. Never bypass rate limiting.
                if error.status_code != 400:
                    raise
                tg_id = self.telegram.send_chunk(text, reply_markup=markup)
        else:
            tg_id = self.telegram.send_chunk(text, reply_markup=markup)
        if mail_id is not None:
            self.store.remember_telegram_mail(self.telegram.chat_id, tg_id, mail_id)
        else:
            # Editing a letter into a menu must not leave a stale reply target.
            self.store.forget_telegram_mail(self.telegram.chat_id, tg_id)
        self.service.next_send_at = time.time() + 1.1

    def welcome(self):
        self.telegram.send_text(
            f'Читаю настроенные ящики: {len(self.service.config.accounts)}.\n\n'
            '📥 Почта — список и текст сохранённых писем.\n'
            '🚫 Заблокированные — письма, скрытые правилами.\n'
            '⚙️ Чёрный список — добавить или убрать адрес и домен.\n\n'
            'Под новым письмом нажмите «Заблокировать отправителя». '
            'Или смахните уведомление, чтобы ответить, и отправьте «блок» либо /block. '
            'Блокируется только адрес отправителя во всех подключённых ящиках.\n\n'
            '/block newsletter@example.org — один адрес\n'
            '/block example.org — домен с поддоменами\n'
            '/unblock newsletter@example.org — убрать правило\n'
            '/status — состояние подключений\n'
            '/summary — сводка, если подключена\n\n'
            'Письма не помечаются прочитанными. В боте доступен сохранённый текст без вложений.',
            reply_markup=MENU)

    def page(self, section, offset=0, *, edit_id=None):
        offset = max(0, offset)
        if section == 'r':
            total, rows = self.store.blacklist_page(offset, PAGE_SIZE)
        else:
            total, rows = self.store.browse_mail(section == 'b', self.service.account_ids, offset, PAGE_SIZE)
        last = max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE)
        if offset > last:
            return self.page(section, last, edit_id=edit_id)
        titles = {'r': '⚙️ Чёрный список', 'b': '🚫 Заблокированные письма', 'i': '📥 Почта'}
        lines = [titles[section], f'Всего: {total} · Страница {offset // PAGE_SIZE + 1}/{last // PAGE_SIZE + 1}', '']
        keyboard = []
        if section == 'r':
            lines.append('Адрес — один отправитель. Домен — все его адреса и поддомены.')
            for n, rule in enumerate(rows, offset + 1):
                kind = 'Адрес' if rule['kind'] == 'address' else 'Домен'
                lines.append(f"{n}. {kind}: {clean(rule['value'], 254)}")
                keyboard.append([button(f"Убрать: {clean(rule['value'], 42)}", f"mb:remove:{rule['id']}:{offset}")])
            if not rows:
                lines.append('Чёрный список пуст.')
            keyboard.append([button('➕ Добавить адрес или домен', 'mb:add')])
        else:
            for n, (mid, mail) in enumerate(rows, offset + 1):
                account = self.service.account_parents.get(mail.account_id, mail.account_id)
                lines += [f'{n}. {clean(mail.subject, 100) or "(без темы)"}',
                          f'   {clean(mail.sender, 105)} · {clean(account, 35)} · {self.date(mail.date)}']
                keyboard.append([button(f'{n}. {clean(mail.subject, 50) or "(без темы)"}',
                                        f'mb:v:{mid}:{section}:{offset}:0')])
            if not rows:
                lines.append('Здесь пока нет писем.')
            if section == 'b':
                lines += ['', f'Здесь хранятся письма, скрытые правилами, до {self.service.config.service.retention_days} дней. '
                          'Разблокировка разрешает новые письма; старые автоматически не пересылаются.']
            else:
                lines += ['', 'Показаны письма, сохранённые ботом. Скрытые правилами доступны в разделе «Заблокированные».']
        pages = []
        if offset:
            pages.append(button('← Назад', f'mb:{section}:{max(0, offset-PAGE_SIZE)}'))
        if offset + PAGE_SIZE < total:
            pages.append(button('Далее →', f'mb:{section}:{offset+PAGE_SIZE}'))
        if pages:
            keyboard.append(pages)
        self.show('\n'.join(lines), keyboard + navigation(), edit_id=edit_id)

    def date(self, value):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(ZoneInfo(self.service.config.service.timezone))
            return stamp.strftime('%d.%m.%Y %H:%M')
        except (ValueError, TypeError):
            return clean(value, 40)

    def view(self, mail_id, section='i', offset=0, part=0, *, edit_id=None):
        mail = self.store.get_message(mail_id, self.service.account_ids)
        if mail is None:
            self.show('Письмо больше не хранится в боте. Откройте актуальный список.', edit_id=edit_id)
            return
        body = ''.join(c for c in mail.body if c in '\n\t' or unicodedata.category(c) not in {'Cc', 'Cf'})
        # 1200 code points <= 2400 UTF-16 units, leaving space for bounded headers.
        parts = [body[i:i+1200] for i in range(0, len(body), 1200)] or ['(Текст письма отсутствует.)']
        part = min(max(0, part), len(parts)-1)
        account = self.service.account_parents.get(mail.account_id, mail.account_id)
        text = (f'✉️ {clean(mail.subject, 240) or "(без темы)"}\n'
                f'От: {clean(mail.sender, 256)}\n'
                f'Ящик: {clean(account, 64)} · {self.date(mail.date)}\n'
                f'Текст {part+1}/{len(parts)}\n\n{parts[part]}')
        controls = []
        if part:
            controls.append(button('← Текст', f'mb:v:{mail_id}:{section}:{offset}:{part-1}'))
        if part+1 < len(parts):
            controls.append(button('Текст →', f'mb:v:{mail_id}:{section}:{offset}:{part+1}'))
        rows = [controls] if controls else []
        rules = self.store.blacklist_matching(mail.sender)
        if rules:
            # Every matching rule is shown: removing one may leave another active.
            for rule in rules[:8]:
                rows.append([button('Разблокировать: '+clean(rule['value'], 40), f"mb:remove:{rule['id']}:0")])
            if len(rules) > 8:
                rows.append([button('Все правила', 'mb:r:0')])
        elif sender_address(mail.sender):
            rows.append([button('🚫 Заблокировать отправителя', f'mb:block:{mail_id}')])
        rows += [[button('← К списку', f'mb:{section}:{offset}')]] + navigation()
        self.show(text, rows, edit_id=edit_id, mail_id=mail_id)

    def add_rule(self, value, *, edit_id=None):
        value = value.strip()
        try:
            kind = 'address' if '@' in value else 'domain'
            normalized = normalize_address(value) if kind == 'address' else normalize_domain(value)
            rule_id = self.store.blacklist_add(kind, normalized)
        except ValueError:
            self.show('Укажите один адрес или домен без имени и ссылки. Например: newsletter@example.org или example.org.', edit_id=edit_id)
            return False
        self.store.exclude_pending(self.service.excluded, self.service.account_ids, include_sent=True)
        label = 'Адрес' if kind == 'address' else 'Домен и его поддомены'
        self.show(f'{label} добавлен в чёрный список:\n{normalized}\n\n'
                  'Уведомления от него отключены. Сохранённые письма доступны в разделе «Заблокированные».',
                  [[button('↩️ Отменить блокировку', f'mb:remove:{rule_id}:0')]] + navigation(), edit_id=edit_id)
        self.store.set('blacklist_input_until', '0')
        return True

    def block_mail(self, mail_id, *, edit_id=None):
        mail = self.store.get_message(mail_id, self.service.account_ids)
        address = sender_address(mail.sender) if mail is not None else None
        if address is None:
            self.show('Не удалось однозначно определить отправителя. Откройте письмо в почте или добавьте адрес через «Чёрный список».', edit_id=edit_id)
            return
        self.add_rule(address, edit_id=edit_id)

    def remove_rule(self, rule_id, *, edit_id=None):
        if not self.store.blacklist_remove(rule_id):
            self.show('Это правило уже недоступно. Откройте актуальный чёрный список.', edit_id=edit_id)
            return
        self.show('Правило удалено. Новые письма будут приходить, если для них нет других правил. '
                  'Ранее скрытые письма остаются в разделе «Заблокированные».', edit_id=edit_id)

    def reply_mail(self, message):
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict):
            return None
        author = reply.get('from', {})
        if (not isinstance(author, dict) or author.get('is_bot') is not True
                or author.get('id') != getattr(self.telegram, 'bot_id', None)):
            return None
        reply_chat = reply.get('chat')
        if reply_chat is not None and (not isinstance(reply_chat, dict)
                                      or str(reply_chat.get('id')) != str(self.telegram.chat_id)):
            return None
        mid = reply.get('message_id')
        if type(mid) is not int or mid <= 0:
            return None
        found = self.store.telegram_mail(self.telegram.chat_id, mid, self.service.account_ids)
        if found:
            return found
        text = reply.get('text')
        return self.store.notification_mail(text, self.service.account_ids) if isinstance(text, str) else None

    def handle_message(self, message):
        if not self.authorized(message):
            return False
        text = message.get('text')
        if not isinstance(text, str):
            return False
        text = text.strip()
        bits = text.split(maxsplit=1)
        command = bits[0].split('@', 1)[0].casefold() if bits else ''
        argument = bits[1] if len(bits) > 1 else ''
        sections = {'/mail': 'i', '/inbox': 'i', '/blocked': 'b', '/blacklist': 'r',
                    '📥 Почта': 'i', '🚫 Заблокированные': 'b', '⚙️ Чёрный список': 'r'}
        if command in {'/start', '/help'}:
            self.store.set('blacklist_input_until', '0')
            self.welcome()
        elif text in sections or command in sections:
            self.store.set('blacklist_input_until', '0')
            self.page(sections.get(text, sections.get(command)))
        elif command == '/cancel':
            self.store.set('blacklist_input_until', '0')
            self.show('Добавление правила отменено.')
        elif command == '/block' or text.casefold() in {'блок', 'блокировать', 'blacklist'}:
            if argument and command == '/block':
                self.add_rule(argument)
            else:
                found = self.reply_mail(message)
                if found:
                    self.block_mail(found[0])
                else:
                    self.show('Ответьте на уведомление этого бота словом «блок» или отправьте /block sender@example.org. '
                              'Для блокировки целого домена: /block example.org.')
        elif command == '/unblock':
            if not argument:
                self.page('r')
            else:
                try:
                    kind = 'address' if '@' in argument else 'domain'
                    value = normalize_address(argument.strip()) if kind == 'address' else normalize_domain(argument.strip())
                except ValueError:
                    self.show('Укажите адрес или домен. Например: /unblock sender@example.org')
                    return True
                matches = [r for r in self.store.blacklist_matching(value if kind == 'address' else 'check@'+value)
                           if r['kind'] == kind and r['value'] == value]
                if matches:
                    self.remove_rule(matches[0]['id'])
                else:
                    self.show('Такого активного правила нет. Другие правила можно проверить в чёрном списке.')
        elif not text.startswith('/') and time.time() < float(self.store.get('blacklist_input_until', '0')):
            self.add_rule(text)
        else:
            return False
        return True

    def handle_callback(self, query):
        if not isinstance(query, dict) or not self.authorized(query.get('message'), query.get('from')):
            return False
        callback_id, data = query.get('id'), query.get('data')
        if not isinstance(callback_id, str) or not isinstance(data, str) or not data.startswith('mb:'):
            return False
        edit_id = query['message'].get('message_id')
        if type(edit_id) is not int or edit_id <= 0:
            return False
        try:
            self.telegram.answer_callback(callback_id)
        except TelegramError as error:
            # A callback acknowledgement expires quickly. Its durable owner
            # action can still be applied after a service/network interruption.
            if error.status_code != 400:
                raise
        parts = data.split(':')
        try:
            # Bound forged client callback payloads before converting integers.
            if len(data.encode()) > 64 or any(len(p) > 18 for p in parts):
                raise ValueError('Invalid callback')
            action = parts[1]
            if action in {'i', 'b', 'r'} and len(parts) == 3:
                offset = int(parts[2]); assert offset >= 0
                self.page(action, offset, edit_id=edit_id)
            elif action == 'v' and len(parts) == 6 and parts[3] in {'i', 'b'}:
                mid, offset, part = int(parts[2]), int(parts[4]), int(parts[5])
                assert mid > 0 and offset >= 0 and part >= 0
                self.view(mid, parts[3], offset, part, edit_id=edit_id)
            elif action == 'block' and len(parts) == 3:
                mid = int(parts[2]); assert mid > 0
                # Keep the original notification visible and usable for replies.
                self.block_mail(mid)
            elif action == 'remove' and len(parts) == 4:
                rid = int(parts[2]); assert rid > 0
                self.remove_rule(rid, edit_id=edit_id)
            elif action == 'add' and len(parts) == 2:
                self.store.set('blacklist_input_until', time.time() + 600)
                self.show('Отправьте один адрес (newsletter@example.org) или домен (example.org). '
                          'Домен блокирует все адреса и поддомены. /cancel — отменить. Ожидаю ввод 10 минут.', edit_id=edit_id)
            else:
                raise ValueError('Unknown callback')
        except (ValueError, AssertionError, OverflowError):
            self.show('Эта кнопка устарела. Откройте актуальный раздел.', edit_id=edit_id)
        return True
