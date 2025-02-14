#!/usr/bin/env python3

import unicodedata
import subprocess
import threading
import termios
import string
import time
import tty
import pty
import sys
import os

import signal
import struct
import fcntl

from chat import Chat, print_chat_perfect
from terminal import Screen, print_perfect

old_tty = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin.fileno())
master_fd, slave_fd = pty.openpty()
winsize = os.get_terminal_size()
chat = Chat()
screen = Screen()
screen.keep_logs_when_clean_screen = True

def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

def sync_winsize(*args, **kwargs):
    global winsize
    winsize = os.get_terminal_size()
    screen.max_height = winsize.lines
    set_winsize(slave_fd, winsize.lines, winsize.columns)

sync_winsize()
signal.signal(signal.SIGWINCH, sync_winsize)

if len(sys.argv) > 1:
    command = sys.argv[1:]
else:
    command = ['bash', '-i']

try:
    proc = subprocess.Popen(
        command,
        preexec_fn=os.setsid,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        shell=False,
        text=False,
        bufsize=0,
    )
except Exception as e:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    raise e

mode = 'char'
running = True
slave_tty = termios.tcgetattr(slave_fd)

def read_stdout():
    global mode
    while running:
        try:
            chars = os.read(master_fd, 10240)
            if chars:
                if mode != 'line':
                    os.write(sys.stdout.fileno(), chars)
                screen.write(chars)
        except Exception as e:
            print('error:', e, end='\r\n')

stdout_thread = threading.Thread(target=read_stdout)
stdout_thread.daemon = True
stdout_thread.start()

def print_context():
    if screen.buffer == 'main' and screen.y == len(screen.lines) - 1:
        line = screen._raw.split('\n')[-1]
        os.write(sys.stdout.fileno(), ('\033[2K\r' + line).encode())
    else:
        os.write(sys.stdout.fileno(), ('\033[2K\r' + screen._raw).encode())

def wrap_multi_lines(display, padding=4):
    global winsize
    display = display.split('\n')
    tmp = []
    width = round((winsize.columns - padding) / 2)
    for line in display:
        for i in range(0, len(line) + 1, width):
            tmp.append(line[i:i+width])
    display = tmp
    return '\r\n'.join(display), len(display)

def clear_lines(lines_all, lines_cur):
    if lines_all != lines_cur:
        for _ in range(lines_all - lines_cur):
            os.write(sys.stdout.fileno(), b'\r\033[1B')
    for _ in range(lines_all - 1):
        os.write(sys.stdout.fileno(), b'\033[2K\r\033[1A')
    os.write(sys.stdout.fileno(), b'\033[2K\r')
    return 1, 1

def print_lines(display, cursor=None):
    line, lines_all = wrap_multi_lines(display)
    os.write(sys.stdout.fileno(), b'\033[2K\r')
    os.write(sys.stdout.fileno(), line.encode())
    if cursor is not None and cursor != len(display):
        for _ in range(lines_all - 1):
            os.write(sys.stdout.fileno(), b'\r\033[1A')
        line_prev, lines_cur = wrap_multi_lines(display[:cursor])
        os.write(sys.stdout.fileno(), b'\r')
        os.write(sys.stdout.fileno(), line_prev.encode())
    else:
        lines_cur = lines_all
    return lines_all, lines_cur

def read_line(prompt=':', include_last=True, max_chars=-1, value='', begin=None, cancel=None, backspace=None):
    global mode
    if begin:
        os.write(sys.stdout.fileno(), begin.encode())
    buf = Screen()
    buf.insert_mode = True
    buf.limit_move = True
    buf.max_height = 1
    buf.write_chars(value)
    cmd = None
    lines_all, lines_cur = print_lines(prompt + buf.current_line(), len(prompt) + buf.x)
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if cancel is not None and c in ['\x03','\x04']:
                cmd = cancel
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
    return cmd

def save_history(prompt, context, cmd):
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print('error:', e, end='\r\n')

def cmd_generate(instrct=None):
    if instrct is None:
        instrct = read_line('(gen-instrct): ', cancel='', include_last=False)
    if instrct == '':
        return ''
    context = screen.text()
    cmd, think = '', ''
    if '#' in instrct:
        args = instrct.split('#')
        instrct = args[0].strip()
        cmd = args[-1].strip()
        output = None
    else:
        output = chat.try_generate(instrct, context)
    confirm_info = ', confirm?'
    flags = '[y/u/n/e/r/k/t]'
    default = 'u'
    save = False
    show_think = False
    while True:
        if output is not None:
            lines_all, lines_cur = 1, 1
            os.write(sys.stdout.fileno(), b'\033[2K\r(gen-cmd): waiting...')
            for chunk in output:
                clear_lines(lines_all, lines_cur)
                cmd, think = chunk[0], chunk[1]
                if cmd == '':
                    display = '(gen-think): ' + think
                else:
                    display = '(gen-cmd): ' + cmd
                lines_all, lines_cur = print_lines(display)
            lines_all, lines_cur = clear_lines(lines_all, lines_cur)
            output = None
        flags_display = flags.replace(default, default.upper())
        display = f'(gen-cmd): {cmd}{confirm_info} {flags_display} '
        if show_think:
            display = f"(gen-think): {think}\n{display}"
        show_think = False
        prefix_info = ''
        confirm_info = ', confirm?'
        confirm = read_line(display, cancel='n', include_last=False)
        confirm = confirm.lower()
        if confirm == '':
            confirm = default
        if confirm in ['y','yes']:
            save = True
            break
        elif confirm in ['u','use']:
            break
        elif confirm in ['n','no','q','quit','exit']:
            cmd = ''
            break
        elif confirm in ['k','think']:
            show_think = True
        elif confirm in ['r','re','retry']:
            output = chat.try_generate(instrct, context)
        elif confirm in ['e','edit']:
            instrct = read_line('(gen-instrct): ', cancel='', include_last=False, value=instrct)
            if instrct == '':
                cmd = ''
                break
            output = chat.try_generate(instrct, context)
        elif confirm in ['t','teach']:
            default = 'y'
            cmd = read_line(f'(gen-cmd): ', include_last=False)
            if cmd == '':
                break
        else:
            confirm_info = ", please input 'y' or 'n':"
    if save:
        save_history(instrct, context, cmd)
    if cmd:
        chat.add_chat(instrct, context, cmd)
    return cmd

def cmd_exec(prompt='cmd', cmd=None):
    if cmd is None:
        cmd = read_line(f'({prompt}): ', cancel='', include_last=False)
    instrct = None
    if '#' in cmd:
        args = cmd.split('#')
        cmd = args[0].strip()
        instrct = args[-1].strip()
    print('\033[2K\r', end='')
    return cmd, instrct

def cmd_watch():
    global total_chars
    total_chars = 0
    def show_screen(*args):
        global total_chars
        if total_chars != screen.total_chars:
            total_chars = screen.total_chars
            print('\033[2J\033[H\r', end='')
            print_perfect(screen, end='\r\n')
            print('\r\n')
        time_display = time.asctime(time.localtime(time.time()))
        print(f'\033[1A\033[2K\rEvery 2.0s: show\t{time_display}', end='\r\n')
        signal.setitimer(signal.ITIMER_REAL, 2)
    signal.signal(signal.SIGALRM, show_screen)
    show_screen()
    while True:
        c = read_line('', max_chars=1, backspace='b')
        signal.setitimer(signal.ITIMER_REAL, 0)
        if c in ['\x03','\x04','q']:
            break
        elif c in ['g']:
            cmd = cmd_generate()
            if cmd:
                cmd += '\n'
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['e']:
            cmd, instrct = cmd_exec()
            if cmd:
                chat.add_chat(instrct, screen.text(), cmd)
                cmd += '\n'
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['i']:
            cmd, instrct = cmd_exec('input')
            if cmd:
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['b']:
            os.write(master_fd, '\b'.encode())
            time.sleep(0.1)
        elif c in ['n']:
            os.write(master_fd, '\n'.encode())
            time.sleep(0.1)
        elif c in ['c']:
            os.write(master_fd, '\x03'.encode())
            time.sleep(0.1)
        elif c in ['d']:
            os.write(master_fd, '\x04'.encode())
            time.sleep(0.1)
        show_screen()
    print('\033[2K\r^C')
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    del total_chars

def cmd_show():
    print('\033[2J\033[H\r', end='')
    print_perfect(screen, end='\r\n')

def prompt_mode():
    global mode
    try:
        return cmd_generate()
    finally:
        print_context()
        mode = 'char'

def line_mode():
    global mode
    try:
        cmd = ''
        args = ''
        while True:
            cmd = read_line(cancel='q', include_last=False)
            cmd = cmd.strip()
            args = None
            if ' ' in cmd and cmd[:1] != ' ':
                index = cmd.find(' ')
                args = cmd[index+1:].strip()
                cmd = cmd[:index].strip()
            if cmd == '':
                pass
            elif cmd in ['q','quit','exit']:
                print_context()
                return ''
            elif cmd in ['s','show','status']:
                cmd_show()
            elif cmd in ['r','raw']:
                print('\033[2J\033[H\r', end='')
                print(screen.raw(), end='\r\n')
            elif cmd in ['ch','chat']:
                print('\033[2J\033[H\r', end='')
                print_chat_perfect(chat, end='\r\n')
            elif cmd in ['reset']:
                print_context()
                termios.tcsetattr(slave_fd, termios.TCSADRAIN, slave_tty)
                screen.mode = 'normal'
                screen.esc = ''
                return ''
            elif cmd in ['c','clear']:
                print('\033[2J\033[H\r', end='')
            elif cmd in ['w','watch']:
                cmd_watch()
            elif cmd in ['g','gen','generate']:
                cmd = cmd_generate(args)
                if cmd == '':
                    continue
                cmd += '\n'
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
                cmd_show()
            elif cmd in ['e','exec']:
                cmd, instrct = cmd_exec(cmd=args)
                if cmd:
                    chat.add_chat(instrct, screen.text(), cmd)
                    cmd += '\n'
                    os.write(master_fd, cmd.encode())
                    time.sleep(0.1)
                    cmd_show()
            elif cmd in ['i','input']:
                cmd, instrct = cmd_exec('input', cmd=args)
                if cmd:
                    os.write(master_fd, cmd.encode())
                    time.sleep(0.1)
                    cmd_show()
            elif cmd in ['err','error']:
                print('\033[2J\033[H\r', end='')
                if len(screen.err_esc) == 0:
                    print('no catched unknown escape sequences', end='\r\n')
                else:
                    print('catched unknown escape sequences:', end='\r\n')
                for err in screen.err_esc:
                    print('esc:', err.encode(), end='\r\n')
            else:
                read_line(f"{cmd}: command not found", max_chars=1)
    finally:
        mode = 'char'

def char_mode():
    global mode
    output = ''
    chars = os.read(sys.stdin.fileno(), 10240).decode()
    for c in chars:
        if c in ['\005']:
            mode = 'line'
        elif c in ['\007']:
            mode = 'prompt'
        else:
            output += c
    return output

def read_command():
    if mode == 'char':
        cmd = char_mode()
    elif mode == 'line':
        cmd = line_mode()
    elif mode == 'prompt':
        cmd = prompt_mode()
    return cmd

try:
    os.write(sys.stdout.fileno(), b'\033c')
    while proc.poll() is None:
        try:
            cmd = read_command()
            os.write(master_fd, cmd.encode())
        except Exception as e:
            print('error:', e, end='\r\n')
            mode = 'char'
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    running = False
    print('exited, if not exit, please input ctrl-c again')

