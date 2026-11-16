"""Password policy and cryptographic temporary-password generation."""
from pathlib import Path
import re
import string
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from panel_accounts import PASSWORD_PATTERN, generate_temporary_password, validate_password


class PasswordPolicyTests(unittest.TestCase):
    def test_length_boundaries(self):
        for length in (12, 256):
            value = 'A1!' + 'a' * (length - 3)
            self.assertEqual(validate_password(value), value)
        for length in (3, 11, 257):
            with self.subTest(length=length), self.assertRaisesRegex(ValueError, 'between 12 and 256'):
                validate_password('A1!' + 'a' * (length - 3))

    def test_non_text_values_are_rejected(self):
        for value in (None, 123456789012, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_password(value)

    def test_missing_categories_have_specific_errors(self):
        for value, missing in (
            ('lowercase123!', 'uppercase letter'),
            ('Uppercaseonly!', 'number'),
            ('Uppercase1234', 'punctuation symbol'),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, missing) as caught:
                validate_password(value)
            self.assertNotIn(value, str(caught.exception))

    def test_all_missing_categories_are_reported(self):
        with self.assertRaises(ValueError) as caught:
            validate_password('onlylowercase')
        for label in ('uppercase letter', 'number', 'punctuation symbol'):
            self.assertIn(label, str(caught.exception))

    def test_lowercase_is_not_required(self):
        self.assertEqual(validate_password('UPPERCASE123!'), 'UPPERCASE123!')

    def test_all_ascii_punctuation_is_accepted_as_a_symbol(self):
        for symbol in string.punctuation:
            with self.subTest(symbol=symbol):
                validate_password('Uppercase123' + symbol)

    def test_spaces_and_unicode_letters_are_not_symbols(self):
        for value in ('Uppercase123 ', 'Uppercase123\t', 'Uppercase123\u00e9', 'Uppercase123\U0001f600'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'punctuation symbol'):
                validate_password(value)
        # Other Unicode characters and spaces remain allowed alongside the required categories.
        validate_password('Uppercase123! caf\u00e9')

    def test_generated_passwords_always_meet_the_policy(self):
        passwords = {generate_temporary_password() for _ in range(200)}
        self.assertEqual(len(passwords), 200)
        for value in passwords:
            self.assertEqual(len(value), 24)
            self.assertEqual(validate_password(value), value)
            self.assertFalse(any(char.isspace() for char in value))

    def test_generation_guarantees_categories_instead_of_relying_on_chance(self):
        with patch('panel_accounts.secrets.choice', side_effect=lambda alphabet: alphabet[0]):
            value = generate_temporary_password()
        self.assertEqual(len(value), 24)
        self.assertIn('A', value)
        self.assertIn('0', value)
        self.assertIn('!', value)
        validate_password(value)

    def test_html_pattern_agrees_with_server_validation(self):
        for value in ('Uppercase123!', 'UPPERCASE123!', 'onlylowercase', 'Uppercase1234',
                      'lowercase123!', 'Uppercaseonly!', 'A1!' + 'a' * 253,
                      'A1!' + 'a' * 254, 'Uppercase123\u00e9', 'Uppercase123! caf\u00e9'):
            with self.subTest(value=value):
                try:
                    validate_password(value)
                    valid = True
                except ValueError:
                    valid = False
                self.assertEqual(bool(re.fullmatch(PASSWORD_PATTERN, value)), valid)


if __name__ == '__main__':
    unittest.main()
