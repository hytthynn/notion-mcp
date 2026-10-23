"""Общие русские сообщения командной строки."""
import argparse


class RussianParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs['add_help'] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = 'Позиционные аргументы'
        self._optionals.title = 'Параметры'
        self.add_argument('-h', '--help', action='help', help='показать справку и выйти')

    def format_usage(self):
        return super().format_usage().replace('usage:', 'Использование:', 1)

    def format_help(self):
        return super().format_help().replace('usage:', 'Использование:', 1)

    def error(self, message):
        for source, target in (('the following arguments are required:', 'не указаны обязательные аргументы:'),
                               ('unrecognized arguments:', 'неизвестные аргументы:'),
                               ('argument ', 'аргумент '), ('invalid choice:', 'недопустимое значение:'),
                               ('choose from', 'допустимые значения:'), ('expected one argument', 'ожидается одно значение')):
            message = message.replace(source, target)
        self.print_usage(__import__('sys').stderr)
        self.exit(2, f'{self.prog}: ошибка: {message}\n')
