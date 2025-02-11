#!/usr/bin/env python3

import types
import sys
import re

def to_int(i):
    try:
        if isinstance(i, str):
            return i.encode()[0]
        if isinstance(i, bytes):
            return i[0]
        if isinstance(i, int):
            return i
    except Exception as e:
        print(e)
    return 0

def esc_arrow_and_keypad(s, c):
    s.move_cursor(1, c)
    padkey_map = { 'p': '0', 'q': '1', 'r': '2', 's': '3', 't': '4', 'u': '5', 'v': '6', 'w': '7', 'x': '8', 'y': '9', 'm': '-', 'l': ',', 'n': '.', 'M': '\r', }
    return padkey_map.get(c)

def esc_move_cursor(s, i, c):
    s.move_cursor(int(i), c)

def esc_set_cursor(s, y, x):
    s.set_cursor(int(x), int(y))

def esc_reset_cursor(s):
    s.set_cursor(0, 0)

def esc_save_cursor_pos(s):
    s.saved_cursor_pos = (s.x, s.y)

def esc_restore_cursor_pos(s):
    s.x = s.saved_cursor_pos[0]
    s.y = s.saved_cursor_pos[1]

def esc_clear_line(s, mode):
    if mode == '' or mode == '0': 
        s.lines[s.y] = s.lines[s.y][:s.x]
    if mode == '1':
        s.lines[s.y] = ' ' * s.x + s.lines[s.y][s.x:]
    if mode == '2':
        s.lines[s.y] = ''
    while len(s.lines[s.y]) < s.x + 1:
        s.lines[s.y] += ' '

def esc_clear_screen(s, mode):
    if mode == '' or mode == '0': 
        s.lines = s.lines[:s.y+1]
    if mode == '1':
        for i in range(s.real_y(0), s.y):
            s.lines[i] = ''
    if mode == '2':
        s.lines = s.lines[:s.real_y(0)]
    while len(s.lines) - 1 < s.y:
        s.lines.append('')
    esc_clear_line(s, mode)

esc_patterns = {
    r'\033O(.)': esc_arrow_and_keypad,
    r'\033\[([0-9]+)m': '',
    r'\033\[([0-9]+)([ABCD])': esc_move_cursor,
    r'\033\[([0-9]+);([0-9]+)[Hf]': esc_set_cursor,
    r'\033\[;?[Hf]': esc_reset_cursor,
    r'\0337': esc_save_cursor_pos,
    r'\0338': esc_restore_cursor_pos,
    r'\033\[([0-9]?)K': esc_clear_line,
    r'\033\[([0-9]?)J': esc_clear_screen,
    r'\033(........)': '',
}

class Screen:

    def __init__(self):
        self.lines = ['']
        self.saved_lines = None
        self.saved_cursor_pos = (0, 0)
        self.x = 0
        self.y = 0
        self._start_y = 0
        self.mode = 'normal'
        self.esc = ''
        self.max_height = 100

    def start_y(self):
        start = 0
        all_lines = len(self.lines)
        if all_lines > self.max_height:
            start = all_lines - self.max_height
        if start > self._start_y:
            self._start_y = start
        return self._start_y

    def real_x(self, x):
        if x < 0:
            x = 0
        return x

    def real_y(self, y):
        if y < 0:
            y = 0
        if y > self.max_height - 1:
            y = self.max_height - 1
        y = self.start_y() + y
        return y

    def set_cursor(self, x=None, y=None):
        lines = self.lines
        if y is not None:
            y = self.real_y(y - 1)
            while len(lines) - 1 < y:
                lines.append('')
            self.y = y
        if x is not None:
            line = lines[self.y]
            x = self.real_x(x - 1)
            while len(line) < x:
                line += ' '
            self.x = x
        self.lines = lines

    def move_cursor(s, i, c):
        if c == 'A':
            s.y -= i
        if c == 'B':
            s.y += i
        if c == 'C':
            s.x += i
        if c == 'D':
            s.x -= i
        if s.x < 0:
            s.x = 0
        if s.y < 0:
            s.y = 0

    def write(self, b):
        try:
            self.write_chars(b.decode())
        except Exception as e:
            print(e)

    def write_chars(self, chars):
        for char in chars:
            self.write_char(char)

    def write_char(self, c):
        if self.mode == 'normal':
            self._write_char_normal_mode(c)
        elif self.mode == 'esc':
            self._write_char_esc_mode(c)

    def _write_char_normal_mode(self, c):
        x = self.x
        y = self.y
        lines = self.lines
        i = to_int(c)

        if c == '\033':
            self._write_char_esc_mode(c)
            return
        if c == '\b':
            if x > 0:
                x -= 1
        elif c == '\r':
            x = 0
        elif c == '\n':
            y += 1
        else:
            x += 1
            line = lines[y]
            while len(line) < x:
                line += ' '
            line = line[:x-1] + c + line[x:]
            line.rstrip(' ')
            lines[y] = line

        self.x = x
        self.y = y
        while len(lines) - 1 < y:
            lines.append('')
        self.lines = lines

    def _write_char_esc_mode(self, c):
        self.mode = 'esc'
        self.esc += c
        esc = self.esc

        for pattern in esc_patterns.keys():
            match_res = re.match(pattern, esc)
            if match_res:
                groups = match_res.groups()
                opr = esc_patterns[pattern]
                self.mode = 'normal'
                self.esc = ''

                if isinstance(opr, str):
                    self.write_chars(opr)
                elif callable(opr):
                    res = opr(self, *groups)
                    if isinstance(res, str):
                        self.write_chars(res)
                else:
                    print('error: not a opr', opr)

                return

    def text(self):
        return '\n'.join(self.lines)

def write_and_print(screen, i, msg=''):
    print('\033[2J\033[H', end='')
    if isinstance(i, bytes):
        s.write(i)
        print('>>> ', i, msg)
    if isinstance(i, str):
        s.write_chars(i)
        print('>>> ', i.encode(), msg)
    print('\r+--------+--------+--------+--------+')
    for i in range(len(s.lines)):
        line = s.lines[i]
        if s.y == i:
            while len(line) < s.x + 1:
                line += ' '
            line = line[:s.x] + '\033[7m' + line[s.x:s.x+1] + '\033[0m' + line[s.x+1:]
        print(line)
    print('\r+--------+--------+--------+--------+')
    sys.stdin.read(1)

if __name__ == '__main__':
    s = Screen()
    s.max_height = 5
    write_and_print(s, 'abcde')
    write_and_print(s, '\b\b\b+')
    write_and_print(s, '\naaaaa')
    write_and_print(s, '\n\rsssss\n\r\n\r')
    write_and_print(s, b'\033[34mcolortext\033[0m-nocolortext')
    write_and_print(s, '\n\r\033OAxxxxxx')
    write_and_print(s, '\033OD+')
    write_and_print(s, '\r\n\033Op111')
    write_and_print(s, '\r\n\033[3A\033[3CXXXXXX')
    write_and_print(s, '\033[1;1H+========+')
    write_and_print(s, '\033[2;10H+========+')
    write_and_print(s, '\033[;H+--------+')
    write_and_print(s, '\033[H+========+')
    write_and_print(s, '\0337\033[10;10f(+++++)')
    write_and_print(s, '\0338(+++++)')
    write_and_print(s, '\033[2;10H', '重置光标位置(1,9)')
    write_and_print(s, '\033[1K', '清除光标前的内容')
    write_and_print(s, '\033[K', '清除光标后的内容')
    write_and_print(s, '\033[3;10H', '重置光标位置(2,9)')
    write_and_print(s, '\033[2K', '清除本行所有内容')
    write_and_print(s, '\033[4;5Haaaaaaaaaaaaa\033[4;10H', '打印内容并重置光标位置(3,9)')
    write_and_print(s, '\033[J', '清除本行以下所有内容')
    write_and_print(s, '\033[4;5Haaaaaaaaaaaaa\033[4;10H', '打印内容并重置光标位置(3,9)')
    write_and_print(s, '\033[1J', '清除本行以上所有内容')
    write_and_print(s, '\033[Haaaaaaaaaaaaa\033[4;5Haaaaaaaaaaaa\033[3;10H', '打印内容并重置光标位置(3,9)')
    write_and_print(s, '\033[2J', '清除所有内容')

