"""
commands/terminal.py
终端相关命令：watch, tty, esc 等
"""

import os
import sys
import time
import signal
from display import show_line, read_line
from commands.core import cmd_show


def cmd_watch(state, args):
    """
    监控屏幕内容变化，定时刷新显示
    
    支持快捷键：g(生成) e(执行) i(输入) c(Ctrl-C) d(Ctrl-D) q(退出)
    """
    state.total_chars = 0
    
    def show_screen(*args):
        if state.total_chars != state.screen.total_chars:
            state.total_chars = state.screen.total_chars
            cmd_show(state)
            print('\r\n')
        time_text = time.asctime(time.localtime(time.time()))
        print(f'\033[1A\033[2K\rEvery 2.0s: show\t{time_text}', end='\r\n')
        signal.setitimer(signal.ITIMER_REAL, 2)
    
    signal.signal(signal.SIGALRM, show_screen)
    show_screen()
    
    while True:
        c = read_line('', max_chars=1, backspace='b')
        signal.setitimer(signal.ITIMER_REAL, 0)
        if c in ['\x03', '\x04', 'q']:
            break
        elif c in ['g']:
            # 导入 generate.py 中的函数以避免循环导入
            from commands.generate import cmd_generate
            cmd = cmd_generate(state, None)[0]
            if cmd:
                os.write(state.master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['e']:
            from commands.generate import cmd_exec_handler
            cmd, instruct = cmd_exec_handler(state)
            if cmd:
                state.ai.save(instruct, state.screen.text(), cmd)
                cmd += '\n'
                os.write(state.master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['i']:
            from commands.generate import cmd_exec_handler
            cmd, instruct = cmd_exec_handler(state, 'input', id='cmd_input')
            if cmd:
                os.write(state.master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['b']:
            os.write(state.master_fd, '\b'.encode())
            time.sleep(0.1)
        elif c in ['n']:
            os.write(state.master_fd, '\n'.encode())
            time.sleep(0.1)
        elif c in ['c']:
            os.write(state.master_fd, '\x03'.encode())
            time.sleep(0.1)
        elif c in ['d']:
            os.write(state.master_fd, '\x04'.encode())
            time.sleep(0.1)
        show_screen()
    
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    del state.total_chars


def cmd_tty(state, args):
    """
    进入原始终端显示模式，支持回调实时刷新
    """
    from common import print_context
    
    def callback_fun():
        cmd_show(state, raw=True)
    
    os.write(sys.stdout.fileno(), b'\033[?25l')
    callback_fun()
    state.slave_callback = callback_fun
    run = True
    
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in ['\005']:
                run = False
                break
            os.write(state.master_fd, c.encode())
        if not run:
            break
    
    state.slave_callback = None
    os.write(sys.stdout.fileno(), b'\033[?25h')
    print_context(state)
    return 'exit'


def cmd_esc(state, args):
    """
    处理终端转义序列调试
    
    子命令：err|saved|status|debug
    """
    if args is None:
        args = 'show'
    
    if args in ['e', 'err', 'error']:
        print('\033[2J\033[H\r', end='')
        if len(state.screen.esc_err) == 0:
            print('no catched unknown escape sequences', end='\r\n')
        else:
            print('catched unknown escape sequences:', end='\r\n')
        for esc in state.screen.esc_err:
            print('esc:', esc.encode(), end='\r\n')
    elif args in ['s', 'save', 'saved']:
        print('\033[2J\033[H\r', end='')
        if len(state.screen.esc_record) == 0:
            print('no saved escape sequences', end='\r\n')
        else:
            print('saved escape sequences:', end='\r\n')
        for esc in state.screen.esc_record:
            print('esc:', esc, end='\r\n')
    elif args in ['d', 'debug']:
        state.screen.esc_debug = not state.screen.esc_debug
        print(f'debug mode: {state.screen.esc_debug}', end='\r\n')
    elif args in ['show', 'status']:
        print(f'debug mode: {state.screen.esc_debug}', end='\r\n')
    else:
        print('usage: esc [err|saved|status|debug]', end='\r\n')
