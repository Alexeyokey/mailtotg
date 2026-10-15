import unittest

from mail_summary_bot.filtering import normalize_address, normalize_domain, sender_address, sender_is_excluded


class SenderFilteringTests(unittest.TestCase):
    def test_actual_address_domain_and_subdomains_match(self):
        for sender in ('orders@ozon.ru', 'Ozon <order@sender.ozon.ru>',
                       'Alerts <alert@SENDER.OZON.RU>', 'global@ozon.com'):
            with self.subTest(sender=sender):
                self.assertTrue(sender_is_excluded(sender, ('ozon.ru', 'ozon.com')))

    def test_brand_text_and_similar_domains_do_not_match(self):
        for sender in ('Ozon <teacher@example.org>', 'ozon.ru@example.org',
                       'user@not-ozon.ru', 'user@ozon.ru.example.org', 'Ozon', ''):
            with self.subTest(sender=sender):
                self.assertFalse(sender_is_excluded(sender, ('ozon.ru',)))

    def test_quoted_display_name_cannot_replace_actual_domain(self):
        self.assertFalse(sender_is_excluded('"Ozon orders@ozon.ru" <real@example.org>', ('ozon.ru',)))

    def test_empty_rules_allow_every_sender(self):
        self.assertFalse(sender_is_excluded('order@ozon.ru', ()))

    def test_yoomoney_and_whoosh_actual_domains_are_blocked(self):
        rules = ('ozon.ru', 'ozon.com', 'yoomoney.ru', 'whoosh.bike')
        for sender in ('ЮMoney <info@yoomoney.ru>', 'Whoosh <support@d.whoosh.bike>'):
            with self.subTest(sender=sender):
                self.assertTrue(sender_is_excluded(sender, rules))
        self.assertFalse(sender_is_excluded('Whoosh <notice@whoosh.bike.example.org>', rules))
        self.assertFalse(sender_is_excluded('ЮMoney <teacher@example.org>', rules))

    def test_domain_normalization_and_idna(self):
        self.assertEqual(normalize_domain(' OZON.RU. '), 'ozon.ru')
        self.assertEqual(normalize_domain('пример.рф'), 'xn--e1afmkfd.xn--p1ai')
        self.assertTrue(sender_is_excluded('info@пример.рф', ('xn--e1afmkfd.xn--p1ai',)))

    def test_rules_reject_links_addresses_wildcards_and_invalid_labels(self):
        for domain in ('', 'https://ozon.ru', 'order@ozon.ru', '*.ozon.ru', 'localhost',
                       '-bad.ru', 'bad-.ru', 'bad..ru', 1, None):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                normalize_domain(domain)

    def test_exact_address_normalization_is_case_insensitive_and_preserves_local(self):
        self.assertEqual(normalize_address(' OPROS.Perekrestok+Shop@X5.RU '), 'opros.perekrestok+shop@x5.ru')
        self.assertEqual(normalize_address('First.Last@пример.рф'), 'first.last@xn--e1afmkfd.xn--p1ai')

    def test_exact_address_rules_reject_display_names_lists_and_unsafe_input(self):
        for value in ('', None, 12, 'a', '@x5.ru', 'a@', 'a@@x5.ru',
                      'Sender <a@x5.ru>', 'a@x5.ru,b@x5.ru', 'a@ x5.ru',
                      'a..b@x5.ru', '.a@x5.ru', 'a.@x5.ru', 'a b@x5.ru',
                      'a@x5.ru\n', 'a@x5.ru\r\nBcc: b@example.org',
                      'a@localhost', '"a b"@x5.ru', 'a' * 65 + '@x5.ru'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_address(value)

    def test_sender_address_uses_actual_mailbox_and_rejects_ambiguity(self):
        self.assertEqual(sender_address('"Other a@example.org" <OPROS.PEREKRESTOK@X5.RU>'), 'opros.perekrestok@x5.ru')
        self.assertEqual(sender_address('Опрос <opros.perekrestok@x5.ru>'), 'opros.perekrestok@x5.ru')
        self.assertEqual(sender_address('Ozon.ru <sale@ozon.ru>'), 'sale@ozon.ru')
        for value in ('', None, 'Ozon', 'a@x5.ru, b@x5.ru',
                      'a@x5.ru, broken', 'Name <a@x5.ru', 'a@x5.ru garbage',
                      'Name <a@x5.ru> <b@example.org>', 'a@x5.ru\nBcc: b@example.org',
                      'Group: a@x5.ru;', '"a@x5.ru" <actual@other.example>, b@x5.ru'):
            with self.subTest(value=value):
                self.assertIsNone(sender_address(value))
                self.assertFalse(sender_is_excluded(value, ('x5.ru',), ('a@x5.ru',)))

    def test_exact_x5_address_does_not_exclude_other_x5_mail(self):
        rule = ('opros.perekrestok@x5.ru',)
        self.assertTrue(sender_is_excluded('Опрос <OPROS.Perekrestok@X5.RU>', (), rule))
        for sender in ('other@x5.ru', 'opros.perekrestok@sub.x5.ru',
                       'prefix.opros.perekrestok@x5.ru',
                       '"opros.perekrestok@x5.ru" <other@x5.ru>'):
            with self.subTest(sender=sender):
                self.assertFalse(sender_is_excluded(sender, (), rule))

    def test_domain_rules_keep_subdomain_scope_with_address_rules(self):
        rules = ('perekrestok.ru',)
        self.assertTrue(sender_is_excluded('mail@club.perekrestok.ru', rules))
        self.assertFalse(sender_is_excluded('opros.perekrestok@x5.ru', rules))
        for domain in ('x5.ru\n', 'x5.ru..', 'x5.ru\x00'):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                normalize_domain(domain)


if __name__ == '__main__':
    unittest.main()
