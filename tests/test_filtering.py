import unittest

from mail_summary_bot.filtering import normalize_domain, sender_is_excluded


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

    def test_domain_normalization_and_idna(self):
        self.assertEqual(normalize_domain(' OZON.RU. '), 'ozon.ru')
        self.assertEqual(normalize_domain('пример.рф'), 'xn--e1afmkfd.xn--p1ai')
        self.assertTrue(sender_is_excluded('info@пример.рф', ('xn--e1afmkfd.xn--p1ai',)))

    def test_rules_reject_links_addresses_wildcards_and_invalid_labels(self):
        for domain in ('', 'https://ozon.ru', 'order@ozon.ru', '*.ozon.ru', 'localhost',
                       '-bad.ru', 'bad-.ru', 'bad..ru', 1, None):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                normalize_domain(domain)


if __name__ == '__main__':
    unittest.main()
