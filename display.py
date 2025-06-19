#!/usr/bin/env python3

import os
import sys
import unicodedata
from terminal import Screen

_char_widths = [
    (126,    1), (159,    0), (687,     1), (710,   0), (711,   1), 
    (727,    0), (733,    1), (879,     0), (1154,  1), (1161,  0), 
    (4347,   1), (4447,   2), (7467,    1), (7521,  0), (8369,  1), 
    (8426,   0), (9000,   1), (9002,    2), (11021, 1), (12350, 2), 
    (12351,  1), (12438,  2), (12442,   0), (19893, 2), (19967, 1),
    (55203,  2), (63743,  1), (64106,   2), (65039, 1), (65059, 0),
    (65131,  2), (65279,  1), (65376,   2), (65500, 1), (65510, 2),
    (120831, 1), (262141, 2), (1114109, 1),
]
 
def get_width(c):
    """(from urwid) Return the screen column width for unicode ordinal o."""
    global _char_widths
    o = ord(c)
    if o == 0xe or o == 0xf:
        return 0
    for num, wid in _char_widths:
        if o <= num:
            return wid

def wrap_multi_lines(display, width=None, padding=0, end='\r\n'):
    if width is None:
        width = os.get_terminal_size().columns - padding
    output = ''
    lines = 1
    n = 0
    for c in display:
        if c == '\n':
            output += end
            lines += 1
            n = 0
            continue
        c_width = get_width(c)
        if n + c_width > width:
            output += end
            lines += 1
            n = 0
        output += c
        n += c_width
    return output, lines

def clear_lines(lines_all, lines_cur, clear=True):
    if lines_all != lines_cur:
        for _ in range(lines_all - lines_cur):
            os.write(sys.stdout.fileno(), b'\r\033[1B')
    for _ in range(lines_all - 1):
        if clear:
            os.write(sys.stdout.fileno(), b'\033[2K')
        os.write(sys.stdout.fileno(), b'\r\033[1A')
    if clear:
        os.write(sys.stdout.fileno(), b'\033[2K')
    os.write(sys.stdout.fileno(), b'\r')
    return 1, 1

def print_lines(text, cursor=None):
    line, lines_all = wrap_multi_lines(text)
    os.write(sys.stdout.fileno(), b'\033[2K\r')
    os.write(sys.stdout.fileno(), line.encode())
    if cursor is not None and cursor != len(text):
        for _ in range(lines_all - 1):
            os.write(sys.stdout.fileno(), b'\r\033[1A')
        line_prev, lines_cur = wrap_multi_lines(text[:cursor])
        os.write(sys.stdout.fileno(), b'\r')
        os.write(sys.stdout.fileno(), line_prev.encode())
    else:
        lines_cur = lines_all
    return lines_all, lines_cur

bufs = {}

def get_bufs():
    global bufs
    return bufs

def show_line(msg):
    read_line(msg, max_chars=1, backspace='b')

def read_lines(prompt='> ', include_last=False, value='', begin=None, cancel='', exit=None, backspace=None, buf=None):
    if buf is None:
        buf = Screen()
    buf.insert_mode = True
    buf.limit_move = True
    buf.max_height = 1
    buf.auto_remove_line = True
    buf.auto_move_between_line = True
    buf.write_chars(value)
    cmd = None
    cancelled = False
    if begin:
        os.write(sys.stdout.fileno(), begin.encode())
    cursor = 0
    for line in buf.lines[:buf.y]:
        cursor += len(prompt) + len(line) + 1
    cursor += len(prompt) + buf.x
    lines_all, lines_cur = print_lines(buf.text(begin=prompt), cursor)
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in ['\x03']:
                if cancel is not None:
                    cancelled = True
                    cmd = cancel
                    break
            if c in ['\x04']:
                if exit is not None:
                    cancelled = True
                    cmd = exit
                    break
            if c in ['\x03','\x04']:
                lines = buf.text()
                if include_last:
                    lines += c
                cmd = lines
                break
            if c in ['\r','\n']:
                buf.write_chars('\n')
            elif c in ['\x7f']:
                if backspace is not None:
                    buf.write_chars(backspace)
                else:
                    buf.write_chars('\b')
            elif c in ['\033']:
                buf.write_char(c)
            elif unicodedata.category(c)[0] == "C":
                pass
            else:
                buf.write_char(c)
        if cmd is not None:
            break
        clear_lines(lines_all, lines_cur)
        cursor = 0
        for line in buf.lines[:buf.y]:
            cursor += len(prompt) + len(line) + 1
        cursor += len(prompt) + buf.x
        lines_all, lines_cur = print_lines(buf.text(begin=prompt), cursor)
    clear_lines(lines_all, lines_cur)
    return cmd

def read_line(prompt=':', include_last=True, max_chars=-1, value='', begin=None, cancel=None, exit=None, backspace=None, id=None, no_save=None, skip_input=False, buf=None):
    global bufs
    if buf is not None:
        pass
    elif id is not None:
        buf = bufs.get(id)
        if buf is None:
            buf = Screen()
            bufs[id] = buf
    else:
        buf = Screen()
    buf.insert_mode = True
    buf.limit_move = True
    buf.max_height = 1
    buf.auto_move_to_end = True
    if len(buf.lines) > 1 and buf.lines[-2] == value:
        buf.lines = buf.lines[:-1]
        buf.y = len(buf.lines) - 1
        buf.x = len(buf.lines[-1])
    else:
        buf.write_chars(value)
    cmd = None
    cancelled = False
    if skip_input:
        cmd = buf.lines[buf.y]
    else:
        if begin:
            os.write(sys.stdout.fileno(), begin.encode())
        lines_all, lines_cur = print_lines(prompt + buf.current_line(), len(prompt) + buf.x)
        while True:
            chars = os.read(sys.stdin.fileno(), 10240).decode()
            for c in chars:
                if c in ['\x03']:
                    if cancel is not None:
                        cancelled = True
                        cmd = cancel
                        break
                if c in ['\x04']:
                    if exit is not None or cancel is not None:
                        cancelled = True
                        cmd = exit if exit is not None else cancel
                        break
                if c in ['\x03','\x04','\r','\n']:
                    line = buf.current_line()
                    if include_last:
                        line += c
                    cmd = line
                    break
                elif c in ['\x7f']:
                    if backspace is not None:
                        buf.write_chars(backspace)
                    else:
                        buf.write_chars('\b')
                elif c in ['\033']:
                    buf.write_char(c)
                elif unicodedata.category(c)[0] == "C":
                    pass
                else:
                    buf.write_char(c)
                if max_chars != -1 and len(buf.current_line()) >= max_chars:
                    cmd = buf.current_line()
                    break
            if cmd is not None:
                break
            clear_lines(lines_all, lines_cur)
            lines_all, lines_cur = print_lines(prompt + buf.current_line(), len(prompt) + buf.x)
        clear_lines(lines_all, lines_cur)
    buf.y = len(buf.lines) - 1
    if cancelled or cmd == '' or (len(buf.lines) > 1 and buf.lines[buf.y - 1] == cmd
            ) or (no_save is not None and cmd in no_save):
        buf.lines[buf.y] = ''
        buf.x = 0
    else:
        buf.lines[buf.y] = cmd
        buf.x = len(buf.lines[buf.y])
        buf.write_char('\n')
    return cmd

if __name__ == '__main__':
    print(wrap_multi_lines('123456789金木水火土ABCDEFG\n一二三四五', 8)[0])

