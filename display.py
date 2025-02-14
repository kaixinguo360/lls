#!/usr/bin/env python3

import os

char_widths = [
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
    global char_widths
    o = ord(c)
    if o == 0xe or o == 0xf:
        return 0
    for num, wid in char_widths:
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

if __name__ == '__main__':
    print(wrap_multi_lines('123456789金木水火土ABCDEFG\n一二三四五', 8)[0])

