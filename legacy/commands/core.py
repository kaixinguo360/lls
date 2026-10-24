"""
commands/core.py
基础命令：状态、显示、清屏等
"""

import sys
import os
import termios
from terminal import print_screen_perfect
from display import show_line
from common import print_context, check_cancel, cancelable


def cmd_quit(state, args):
    """
    退出当前模式，恢复终端显示
    """
    print_context(state)
    return 'exit'


def cmd_show_status(state, args):
    """
    显示当前屏幕内容
    """
    cmd_show(state)


def cmd_show(state, **kwargs):
    """
    清屏并完美显示当前屏幕内容
    """
    print('\033[2J\033[H\r', end='')
    print_screen_perfect(state.screen, end='\r\n', **kwargs)


def cmd_raw(state, args):
    """
    清屏并以原始格式输出当前屏幕内容
    """
    print('\033[2J\033[H\r', end='')
    print(state.screen.raw(), end='\r\n')


def cmd_chat(state, args):
    """
    打印对话状态信息
    """
    print('\033[2J\033[H\r', end='')
    state.ai.print()


def cmd_reset(state, args):
    """
    重置终端和屏幕状态，恢复初始模式
    """
    print_context(state)
    termios.tcsetattr(state.slave_fd, termios.TCSADRAIN, state.slave_tty)
    state.screen.mode = 'normal'
    state.screen.esc = ''
    return 'exit'


def cmd_clear(state, args):
    """
    清屏
    """
    print('\033[2J\033[H\r', end='')


def cmd_err(state, args):
    """
    显示最近捕获的错误信息
    """
    print('\033[2J\033[H\r', end='')
    if state.err is None:
        print('no catched error', end='\r\n')
    else:
        print('catched error:', end='\r\n')
        os.write(sys.stdout.fileno(), state.err.replace('\n', '\r\n').encode())


def cmd_conf(state, args):
    """
    显示当前 AI 实例的参数配置
    """
    state.ai.printConfigs(end='\r\n')
