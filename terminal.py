#!/usr/bin/env python3
"""
terminal.py
终端屏幕模拟与ANSI转义序列处理模块。
负责屏幕缓冲、光标移动、行列管理、转义序列解析等。
"""

import types
import sys
import re

def to_int(i):
    """将输入转换为整数，支持str/bytes/int。"""
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

def esc_delete(screen):
    """处理删除键的行为。"""
    line = screen.lines[screen.y]
    if screen.insert_mode:
        if screen.x < len(line):
            line = line[:screen.x] + line[screen.x+1:]
        else:
            if screen.y < len(screen.lines) - 1 and screen.auto_remove_line:
                line += screen.lines[screen.y+1]
                screen.lines = screen.lines[:screen.y] + screen.lines[screen.y+1:]
    else:
        line = line[:screen.x] + ' ' + line[screen.x+1:]
    screen.lines[screen.y] = line

def esc_arrow_and_keypad(s, c):
    s.move_cursor(1, c)
    padkey_map = { 'p': '0', 'q': '1', 'r': '2', 's': '3', 't': '4', 'u': '5', 'v': '6', 'w': '7', 'x': '8', 'y': '9', 'm': '-', 'l': ',', 'n': '.', 'M': '\r', }
    return padkey_map.get(c)

def esc_move_cursor(s, i, c):
    if i == '':
        i = 1
    if c == 'E':
        c = 'B'
    if c == 'F':
        c = 'A'
    s.move_cursor(int(i), c)

def esc_set_cursor(s, y, x):
    x = 1 if x == '' else int(x)
    y = 1 if y == '' else int(y)
    s.set_cursor(x, y)

def esc_set_cursor_x(s, x):
    x = 1 if x == '' else int(x)
    s.set_cursor(x, None)

def esc_set_cursor_y(s, y):
    y = 1 if y == '' else int(y)
    s.set_cursor(None, y)

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

def esc_clear_screen(s, mode=None):
    if mode is None:
        mode = '2'
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

def esc_end(s):
    s.y = len(s.lines)
    s.x = len(s.lines[s.y])

def esc_use_alter_buffer(s):
    s.buffer = 'alter'

def esc_use_main_buffer(s):
    s.buffer = 'main'

def esc_raw(s, chars):
    return '^' + chars

# Reference: https://vt100.net/docs/vt100-ug/chapter3.html
# Reference: https://learn.microsoft.com/zh-cn/windows/console/console-virtual-terminal-sequences
esc_patterns = {
    r'\033\[2~': '', # Insert
    r'\033\[3~': esc_delete, # 删除键
    r'\033\[5~': '', # Page Up
    r'\033\[6~': '', # Page Down
    r'\033\[F': esc_end, # End
    r'\033[O\?](.)': esc_arrow_and_keypad, # 光标键+数字小键盘
    r'\033\[([ABCD])': esc_arrow_and_keypad, # 光标键+数字小键盘
    r'\033[ABCD]': esc_arrow_and_keypad, # TODO 光标键(VT52兼容模式)
    # VT52兼容模式
    r'\033[FGZ=><]': '',
    r'\033I': '', # Reverse line feed
    #r'\033O[P-S]': '', # F1-F4
    r'\033\[(0-9)*~': '', # F5-F12
    # 光标定位
    r'\033\[([0-9]*)([ABCDEF])': esc_move_cursor, # 光标移动
    r'\033\[[0-9]*;([0-9]*)([ABCDEF])': esc_move_cursor, # 光标移动(按住Ctrl)
    r'\033\[([0-9]*)G': esc_set_cursor_x, # 光标绝对定位X
    r'\033\[([0-9]*)d': esc_set_cursor_y, # 光标绝对定位Y
    r'\033\[?([0-9]*);?([0-9]*)[Hf]': esc_set_cursor, # 光标绝对定位X,Y
    r'\033Y([0-9]{2})([0-9]{2})': '', # 光标定位(VT52兼容模式)
    r'\0337|\033\[s': esc_save_cursor_pos, # 光标位置保存
    r'\0338|\033\[u': esc_restore_cursor_pos, # 光标位置还原
    r'\033\[20[hl]': '\n', # 换行
    # 光标形状
    r'\033\[[0-9]*\x20q': '',
    # LED控制
    r'\033\[[0-9]*q': '',
    # 滚动屏幕
    r'\033\[[0-9]*[ST]': '',
    # 文本修改
    r'\033\[[0-9]*[@PXLM]': '',
    r'\033\[?([0-9]?)K': esc_clear_line,
    r'\033\[?([0-9]?)J': esc_clear_screen,
    # 文本格式,终端窗口配置
    r'\033\[[0-9;%>]*[mt]': '',
    # OSC序列
    r'\033.*(\a|\033\\)': '',
    # 模式更改
    r'\033[=>]': '',
    # 查询状态
    r'\033\[[0-9]*[nc]': '',
    # 光标位置报告
    r'\033\[[0-9;]*R': '',
    # 制表符
    r'\033H': '\t',
    r'\033\[[0-9]*[IZg]': '',
    # 指定字符集
    r'\033[\(\)].': '',
    # 设置滚动边距
    r'\033\[([0-9]*);?([0-9]*)r': '',
    # 设置幕缓冲区
    r'\033\[\?(?:1049|47)h': esc_use_alter_buffer, # 使用备用屏幕缓冲区
    r'\033\[\?(?:1049|47)l': esc_use_main_buffer, # 使用主用屏幕缓冲区
    # 光标可见性,设置窗口宽度
    r'\033\[\?[0-9;]*[hl]': '',
    # 换行模式设置
    r'\033E': '', # TODO 光标移动到下一行第一个位置
    r'\033M': '', # TODO 光标移动到上一行相同水平位置
    # 重置状态/模式
    r'\033c': '', # TODO 重置为初始状态
    r'\033\[[0-9;]+l': '', # TODO 重置一个或多个VT100模式参数
    # 软重置
    r'\033\[!p': '',
    # 请求终端参数
    r'\033\[.*x': '',
    # 行高设置
    r'\033#[0-9]': '',
    # 终止控制序列
    r'\033.*(\030|\032)': '',
}

class Screen:
    """
    终端屏幕缓冲区模拟。
    支持多行文本、光标移动、插入/删除、历史记录等。
    """
    def __init__(self, history_file=None):
        self.lines = ['']
        self._raw = ''
        self.saved_lines = None
        self.saved_cursor_pos = (0, 0)
        self.x = 0  # 光标x
        self.y = 0  # 光标y
        self._start_y = 0
        self.mode = 'normal'
        self.esc = ''
        self.esc_debug = False
        self.esc_record = []
        self.esc_err = []
        self.buffer = 'main'
        self.dropped_chars = 0
        self.dropped_lines = 0
        self.max_chars = 8000
        self.max_lines = 500
        self.max_height = 30
        self.total_chars = 0
        self.keep_logs_when_clean_screen = False
        self.insert_mode = False  # 插入模式
        self.limit_move = False
        self.auto_move_to_end = False
        self.auto_move_between_line = False
        self.auto_remove_line = False
        self.history_begin = '[lls is beginning]\n'
        self.history_end = '[lls is terminating]\n'
        self.history_file = history_file
        if history_file:
            self.history = open(history_file, 'a+')
            self.history.write(self.history_begin)
        else:
            self.history = None

    def write_history(self, line):
        """写入历史文件。"""
        if self.history:
            self.history.write(line + '\n')

    def dump_history(self, left=0):
        """将多余行写入历史文件。"""
        while len(self.lines) > left:
            line = self.lines[0]
            self.lines = self.lines[1:]
            self.write_history(line)
            self.y -= 1
            self._start_y -= 1
            self.dropped_lines += 1

    def close(self):
        """关闭历史文件。"""
        self.dump_history()
        if self.history:
            self.history.write(self.history_end)
            self.history.close()

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
        # TODO when y > max_height ?
        if y > self.max_height - 1:
            y = self.max_height - 1
        y = self.start_y() + y
        return y

    def set_cursor(s, x=None, y=None, limit=None):
        if x == 1 and y == 1 and s.keep_logs_when_clean_screen:
            s._start_y = s.y
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
            if s.auto_move_to_end:
                s.nor(limit)
                s.x = len(s.lines[s.y])
        if c == 'B':
            ox = s.x
            s.x = 0
            s.nor(limit)
            s.x = ox
            s.y += i
            if s.auto_move_to_end:
                s.nor(limit)
                s.x = len(s.lines[s.y])
        if c == 'C':
            if s.x < len(s.lines[s.y]):
                s.x += i
            else:
                if s.y < len(s.lines) - 1 and s.auto_move_between_line:
                    s.y += 1
                    s.x = 0
        if c == 'D':
            if s.x > 0:
                s.x -= i
            else:
                if s.y > 0 and s.auto_move_between_line:
                    s.y -= 1
                    s.x = len(s.lines[s.y])
        s.nor(limit)

    def write(self, b):
        """写入字节流。"""
        try:
            self.write_chars(b.decode())
        except Exception as e:
            print(e)

    def write_chars(self, chars):
        """写入字符串。"""
        for char in chars:
            self.write_char(char)

    def write_char(self, c):
        """写入单个字符。"""
        self.total_chars += 1
        self._raw += c
        if len(self._raw) > self.max_chars:
            offset = len(self._raw) - self.max_chars
            self.dropped_chars += offset
            self._raw = self._raw[offset:]
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
            s.dump_history(s.max_lines)

    def _write_char_normal_mode(s, c):
        if c in ['\a']:
            return
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
            else:
                if s.y > 0 and s.insert_mode and s.auto_remove_line:
                    s.x = len(s.lines[s.y-1])
                    s.lines[s.y-1] += s.lines[s.y]
                    s.lines = s.lines[:s.y] + s.lines[s.y+1:]
                    s.y -= 1
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
    
    def _check_esc(self, esc, prev=None):
        for pattern in esc_patterns.keys():
            try:
                match_res = re.match(pattern, esc)
                if match_res:
                    groups = match_res.groups()
                    opr = esc_patterns[pattern]
                    self.mode = 'normal'
                    self.esc = ''

                    if self.esc_debug:
                        self.esc_record.append((esc.encode(), pattern, opr))
                        self.esc_record = self.esc_record[-100:]

                    if prev is not None:
                        self.write_chars(prev)

                    if isinstance(opr, str):
                        self.write_chars(opr)
                    elif callable(opr):
                        res = opr(self, *groups)
                        if isinstance(res, str):
                            self.write_chars(res)
                    else:
                        print('error: not a opr', opr)

                    return True
            except Exception as e:
                print('regex error:', pattern)
                raise e
        return False

    def _write_char_esc_mode(self, c):

        #if self.mode == 'esc' and c == '\033':
        #    self.write_chars(self.esc[1:])
        #    self.esc = ''

        self.mode = 'esc'
        self.esc += c
        esc = self.esc

        if self._check_esc(esc):
            return

        if '\033' in esc[1:]:
            lines = esc.split('\033')
            prev_esc = ''.join(lines)
            last_esc = '\033' + lines[-1]
            if self._check_esc(last_esc, prev_esc):
                self.esc_err.append(esc)
                self.esc_err = self.esc_err[-100:]
                return

    def text(self, end='\n', begin=''):
        """获取当前屏幕所有文本。"""
        return begin + (end + begin).join(self.lines)

    def raw(self):
        """获取原始输入流。"""
        return self._raw

    def current_line(self):
        """获取当前行内容。"""
        return self.lines[self.y]

def print_screen_perfect(screen, end='\n', tail='', width=None, height=None, frame=False, raw=False):
    """
    以美观方式打印屏幕内容，支持高亮光标、边框、宽高自适应。
    """
    import os
    if width is None:
        winsize = os.get_terminal_size()
        if frame:
            width = winsize.columns - 4
        else:
            width = winsize.columns - 2
    if height is None:
        if frame:
            height = screen.max_height
        else:
            height = len(screen.lines)
    y_begin = len(screen.lines) - y_end if len(screen.lines) > height else 0
    y_end = len(screen.lines) if len(screen.lines) > height else height
    if not raw:
        print('+' + '-'*width + '+', end=end)
    for i in range(y_begin, y_end):
        line = screen.lines[i] if i < len(screen.lines) else ''
        line += tail
        x_begin = 0
        if frame:
            x_end = width if len(line) > width else len(line)
        else:
            x_end = len(line)
        display = line[x_begin:x_end] + ' ' * (width - (x_end - x_begin))
        if screen.y == i:
            if frame:
                if screen.x >= x_begin and screen.x < x_end:
                    cursor = screen.x + x_begin
                    display = display[:cursor] + '\033[7m' + display[cursor:cursor+1] + '\033[0m' + display[cursor+1:]
            else:
                while screen.x > len(display) - 1:
                    display += ' '
                display = display[:screen.x] + '\033[7m' + display[screen.x:screen.x+1] + '\033[0m' + display[screen.x+1:]
        if frame:
            print('|' + display + '|', end='')
        else:
            print(display.rstrip(' '), end='')
        if not (raw and i == (y_end - 1)):
            print('', end=end)
        print('\r', end='')
    if not raw:
        print('+' + '-'*width + '+', end=end)
        print(f"cursor: {{x={screen.x+1},y={screen.y-screen.start_y()+1}}}", end='')
        print(f", lines: {len(screen.lines)}", end='')
        print(f", offset: {screen._start_y}", end='')
        print(f", height: {screen.max_height}", end='')
        print(f", buffer: {screen.buffer}", end='')
        print(f", mode: {screen.mode}", end='')
        if screen.mode == 'esc':
            print(', esc=', screen.esc.encode(), end='')
        print('', end=end)

print_wait = False

def write_and_print(screen, chars, msg='', delay=0.05, sleep=0.5):
    """逐字符写入并打印屏幕，支持延时和调试。"""
    import time
    if isinstance(chars, bytes):
        chars = chars.decode()
    for c in chars:
        print('\033[H\033[2J', end='')
        screen.write_char(c)
        print('>>> ', chars.encode(), msg)
        print_screen_perfect(screen, end='\n', tail='<')
        if not print_wait and delay:
            time.sleep(delay)
    if print_wait:
        sys.stdin.read(1)
    elif sleep:
        time.sleep(sleep)

if __name__ == '__main__':
    s = Screen()
    s.max_height = 5
    print_wait = True
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
    write_and_print(s, '\033[d', '光标绝对定位，y=1')
    write_and_print(s, '\033[3d', '光标绝对定位，y=3')
    write_and_print(s, '\033[G', '光标绝对定位，x=1')
    write_and_print(s, '\033[3G', '光标绝对定位，x=3')
    write_and_print(s, '\033[10d', '光标绝对定位，y=10')
    write_and_print(s, '\033[H\033[2J', '重置光标位置并清除所有内容')

