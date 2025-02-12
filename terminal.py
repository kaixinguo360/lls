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

def esc_delete(s):
    line = s.lines[s.y]
    if s.insert_mode:
        line = line[:s.x] + line[s.x+1:]
    else:
        line = line[:s.x] + ' ' + line[s.x+1:]
    s.lines[s.y] = line

def esc_arrow_and_keypad(s, c):
    s.move_cursor(1, c)
    padkey_map = { 'p': '0', 'q': '1', 'r': '2', 's': '3', 't': '4', 'u': '5', 'v': '6', 'w': '7', 'x': '8', 'y': '9', 'm': '-', 'l': ',', 'n': '.', 'M': '\r', }
    return padkey_map.get(c)

def esc_move_cursor(s, i, c):
    if i == '':
        i = 1
    s.move_cursor(int(i), c)

def esc_set_cursor(s, y, x):
    s.set_cursor(int(x), int(y))

def esc_reset_cursor(s):
    if s.keep_logs_when_clean_screen:
        s._start_y = s.y
    s.set_cursor(0, 0)

def esc_save_cursor_pos(s):
    s.saved_cursor_pos = (s.x, s.y)

def esc_restore_cursor_pos(s):
    s.x = s.saved_cursor_pos[0]
    s.y = s.saved_cursor_pos[1]
    s.nor()

def esc_clear_line(s, mode):
    if mode == '' or mode == '0': 
        s.lines[s.y] = s.lines[s.y][:s.x]
    if mode == '1':
        s.lines[s.y] = ' ' * s.x + s.lines[s.y][s.x:]
    if mode == '2':
        s.lines[s.y] = ''
    s.nor()

def esc_clear_screen(s, mode):
    if mode == '' or mode == '0': 
        s.lines = s.lines[:s.y+1]
    if mode == '1':
        for i in range(s.real_y(0), s.y):
            s.lines[i] = ''
    if mode == '2':
        if s.keep_logs_when_clean_screen:
            s._start_y = s.y
        s.lines = s.lines[:s.real_y(0)]
    s.nor()
    esc_clear_line(s, mode)

def esc_raw(s, chars):
    return '^' + chars

esc_patterns = {
    r'\033.*\a': '',
    r'\033\[3~': esc_delete,
    r'\033O(.)': esc_arrow_and_keypad,
    r'\033\[([0-9;]+)m': '',
    r'\033\[([0-9]*)([ABCD])': esc_move_cursor,
    r'\033\[([0-9]+);([0-9]+)[Hf]': esc_set_cursor,
    r'\033\[;?[Hf]': esc_reset_cursor,
    r'\0337': esc_save_cursor_pos,
    r'\0338': esc_restore_cursor_pos,
    r'\033\[([0-9]?)K': esc_clear_line,
    r'\033\[([0-9]?)J': esc_clear_screen,
}

class Screen:

    def __init__(self):
        self.lines = ['']
        self._raw = ''
        self.saved_lines = None
        self.saved_cursor_pos = (0, 0)
        self.x = 0
        self.y = 0
        self._start_y = 0
        self.mode = 'normal'
        self.esc = ''
        self.dropped_lines = 0
        self.max_lines = 500
        self.max_height = 30
        self.total_chars = 0
        self.keep_logs_when_clean_screen = False
        self.insert_mode = False
        self.limit_move = False

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

    def set_cursor(s, x=None, y=None, limit=None):
        if y is not None:
            ox = s.x
            s.x = 0
            s.nor(limit)
            s.x = ox
            s.y = s.real_y(y - 1)
            s.nor(limit)
        if x is not None:
            s.x = s.real_x(x - 1)
            s.nor(limit)

    def move_cursor(s, i, c, limit=None):
        if c == 'A':
            ox = s.x
            s.x = 0
            s.nor(limit)
            s.x = ox
            s.y -= i
        if c == 'B':
            ox = s.x
            s.x = 0
            s.nor(limit)
            s.x = ox
            s.y += i
        if c == 'C':
            s.x += i
        if c == 'D':
            s.x -= i
        s.nor(limit)

    def write(self, b):
        try:
            self.write_chars(b.decode())
        except Exception as e:
            print(e)

    def write_chars(self, chars):
        for char in chars:
            self.write_char(char)

    def write_char(self, c):
        self.total_chars += 1
        self._raw += c
        if self.mode == 'normal':
            self._write_char_normal_mode(c)
        elif self.mode == 'esc':
            self._write_char_esc_mode(c)

    def nor(s, limit=None):
        if s.x < 0:
            s.x = 0
        if s.y < 0:
            s.y = 0
        if limit is None:
            limit = s.limit_move
        if limit:
            if s.y > len(s.lines) - 1:
                s.y = len(s.lines) - 1
            if s.x > len(s.lines[s.y]):
                s.x = len(s.lines[s.y])
        while s.y > len(s.lines) - 1:
            s.lines.append('')
        line = s.lines[s.y]
        line = line.rstrip(' ')
        while s.x > len(line):
            line += ' '
        s.lines[s.y] = line
        if len(s.lines) > s.max_lines:
            offset = len(s.lines) - s.max_lines
            s.lines = s.lines[offset:]
            s.y -= offset
            s._start_y -= offset
            s.dropped_lines += offset

    def _write_char_normal_mode(s, c):
        if c == '\033':
            s._write_char_esc_mode(c)
            return
        if c == '\b':
            if s.x > 0:
                s.x -= 1
                if s.insert_mode:
                    line = s.lines[s.y]
                    line = line[:s.x] + line[s.x+1:]
                    s.lines[s.y] = line
        elif c == '\r':
            s.x = 0
        elif c == '\n':
            if s.insert_mode:
                line = s.lines[s.y]
                lines_prev = s.lines[:s.y]
                lines_after = s.lines[s.y+1:]
                chars_prev = line[:s.x]
                chars_after = line[s.x:]
                s.lines = [*lines_prev, chars_prev, chars_after, *lines_after]
                s.y += 1
                s.x = 0
            else:
                s.y += 1
                s.nor(limit=False)
        else:
            s.x += 1
            s.nor(limit=False)
            line = s.lines[s.y]
            if s.insert_mode:
                line = line[:s.x-1] + c + line[s.x-1:]
            else:
                line = line[:s.x-1] + c + line[s.x:]
            s.lines[s.y] = line
        s.nor()

    def _write_char_esc_mode(self, c):

        if self.mode == 'esc' and c == '\033':
            self.write_chars(self.esc[1:])
            self.esc = ''

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

    def text(self, end='\n'):
        return end.join(self.lines)

    def raw(self):
        return self._raw

    def current_line(self):
        return self.lines[self.y]

def print_perfect(s, end='\n', tail=''):
    print('+---------+---------+---------+---------+', end=end)
    for i in range(len(s.lines)):
        line = s.lines[i] + tail
        if s.y == i:
            while s.x > len(line) - 1:
                line += ' '
            line = line[:s.x] + '\033[7m' + line[s.x:s.x+1] + '\033[0m' + line[s.x+1:]
        print(line, end=end)
    print('+---------+---------+---------+---------+', end=end)
    print(f"cursor: {{x={s.x+1},y={s.y-s.start_y()+1}}}", end='')
    print(f", lines: {len(s.lines)}", end='')
    print(f", offset: {s._start_y}", end='')
    print(f", height: {s.max_height}", end='')
    print(f", mode: {s.mode}", end='')
    if s.mode == 'esc':
        print(', esc=', s.esc.encode(), end='')
    print('', end=end)

def write_and_print(s, chars, msg=''):
    import time
    if isinstance(chars, bytes):
        chars = chars.decode()
    for c in chars:
        print('\033[H\033[2J', end='')
        s.write_char(c)
        print('>>> ', chars.encode(), msg)
        print_perfect(s, end='\n', tail='<')
        time.sleep(0.05)
    #sys.stdin.read(1)
    time.sleep(0.5)

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
    write_and_print(s, '\033[1;1H+=========+')
    write_and_print(s, '\033[2;11H+=========+')
    write_and_print(s, '\033[;H+---------+')
    write_and_print(s, '\033[H+=========+')
    write_and_print(s, '\0337\033[10;10f(+++++)')
    write_and_print(s, '\0338(+++++)')
    write_and_print(s, '\033[2;11H', '重置光标位置(2,11)')
    write_and_print(s, '\033[1K', '清除光标前的内容')
    write_and_print(s, '\033[K', '清除光标后的内容')
    write_and_print(s, '\033[3;11H', '重置光标位置(3,11)')
    write_and_print(s, '\033[2K', '清除本行所有内容')
    write_and_print(s, '\033[4;5Haaaaaaaaaaaaa\033[4;11H', '打印内容并重置光标位置(4,11)')
    write_and_print(s, '\033[J', '清除本行以下所有内容')
    write_and_print(s, '\033[4;5Haaaaaaaaaaaaa\033[4;11H', '打印内容并重置光标位置(4,11)')
    write_and_print(s, '\033[1J', '清除本行以上所有内容')
    write_and_print(s, '\033[Haaaaaaaaaaaaa\033[4;5Haaaaaaaaaaaa\033[3;11H', '打印内容并重置光标位置(3,11)')
    write_and_print(s, '\033[2J', '清除所有内容')

