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

from generate import generate_cmd
from terminal import Screen, print_perfect

old_tty = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin.fileno())
master_fd, slave_fd = pty.openpty()
winsize = os.get_terminal_size()
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
context = ''
running = True
slave_tty = termios.tcgetattr(slave_fd)

def read_stdout():
    global context, mode
    while running:
        try:
            chars = os.read(master_fd, 10240)
            if chars:
                if mode != 'line':
                    os.write(sys.stdout.fileno(), chars)
                if len(chars) == 1 and chars[0] == 127:
                    context = context[:-1]
                else:
                    context += chars.decode()
                screen.write(chars)
        except Exception as e:
            print('error:', e, end='\r\n')

stdout_thread = threading.Thread(target=read_stdout)
stdout_thread.daemon = True
stdout_thread.start()

#cur = ''
#def main_input():
#    global cur
#    d = os.read(sys.stdin.fileno(), 10240)
#    if d[0] != 13:
#        cur += d.decode()
#    else:
#        if cur.strip(' ') == 'show':
#            os.write(sys.stdout.fileno(), context.encode())
#            return
#        cur = ''
#    os.write(master_fd, d)

def print_context():
    global context
    last_line = context.split('\n')[-1]
    os.write(sys.stdout.fileno(), ('\033[2K\r' + last_line).encode())

def read_line(prompt=':', include_last=True, max_chars=-1, begin='\n', value='', cancel=None, backspace=None):
    global mode
    if begin:
        os.write(sys.stdout.fileno(), begin.encode())
    buf = Screen()
    buf.insert_mode = True
    buf.limit_move = True
    buf.max_height = 1
    buf.write_chars(value)
    os.write(sys.stdout.fileno(), (f'\r{prompt}' + buf.current_line()).encode())
    def print_prompt():
        os.write(sys.stdout.fileno(), b'\033[2K\r')
        line = buf.current_line()
        line_prev = line[:buf.x]
        os.write(sys.stdout.fileno(), (prompt + line).split('\n')[-1].encode())
        os.write(sys.stdout.fileno(), b'\r')
        os.write(sys.stdout.fileno(), (prompt + line_prev).split('\n')[-1].encode())
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if cancel is not None and c in ['\x03','\x04']:
                return cancel
            if c in ['\x03','\x04','\r','\n']:
                os.write(sys.stdout.fileno(), f'\r{prompt}'.encode())
                line = buf.current_line()
                if include_last:
                    line += c
                return line
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
                return buf.current_line()
        print_prompt()

def save_history(prompt, context, cmd):
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print('error:', e, end='\r\n')

def cmd_generate():
    prompt = read_line('(gen-prompt): ', cancel='', begin='\033[2K\r', include_last=False)
    if prompt == '':
        return ''
    cmd, think = generate_cmd(prompt, screen.text())
    confirm_info = ', confirm?'
    flags = '[y/u/n/e/r/k/t]'
    default = 'u'
    save = False
    show_think = False
    while True:
        cmd_display = cmd.replace('\n', '\n\r')
        flags_display = flags.replace(default, default.upper())
        confirm = read_line(f'(gen-cmd): {cmd_display}{confirm_info} {flags_display} ', cancel='n', begin='\033[2K\r', include_last=False)
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
            think_display = think.replace('\n', '\n\r')
            os.write(sys.stdout.fileno(), b'\r')
            os.write(sys.stdout.fileno(), think_display.encode())
        elif confirm in ['r','re','retry']:
            cmd, think = generate_cmd(prompt, context)
        elif confirm in ['e','edit']:
            prompt = read_line('(gen-prompt): ', begin='\033[2K\r', include_last=False, value=prompt)
            if prompt == '':
                break
            cmd, think = generate_cmd(prompt, context)
        elif confirm in ['t','teach']:
            default = 'y'
            cmd = read_line(f'(gen-cmd): ', begin='\033[2K\r', include_last=False)
            if cmd == '':
                break
        else:
            confirm_info = ", please input 'y' or 'n':"
    if save:
        save_history(prompt, context, cmd)
    return cmd

def cmd_exec(prompt='cmd'):
    cmd = read_line(f'({prompt}): ', cancel='', begin='\033[2K\r', include_last=False)
    print('\033[2K\r', end='')
    return cmd

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
        c = read_line('', begin='\033[2K\r', max_chars=1, backspace='b')
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
            cmd = cmd_exec()
            if cmd:
                cmd += '\n'
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['i']:
            cmd = cmd_exec('input')
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

def prompt_mode():
    global context, mode
    try:
        return cmd_generate()
    finally:
        print_context()
        mode = 'char'

def line_mode():
    global mode
    try:
        cmd = ''
        while True:
            cmd = read_line(begin='\033[2K\r', cancel='q', include_last=False)
            if cmd == '':
                pass
            elif cmd in ['q','quit','exit']:
                print_context()
                return ''
            elif cmd in ['s','show','status']:
                print('\033[2J\033[H\r', end='')
                print_perfect(screen, end='\r\n')
            elif cmd in ['r','raw']:
                print('\033[2J\033[H\r', end='')
                print(screen.raw(), end='\r\n')
            elif cmd in ['reset']:
                print_context()
                termios.tcsetattr(slave_fd, termios.TCSADRAIN, slave_tty)
                screen.mode = 'normal'
                screen.esc = ''
                return ''
            elif cmd in ['c','clear']:
                print('\033[2J\033[H\r', end='')
            elif cmd in ['t','tail','w','watch']:
                cmd_watch()
            elif cmd in ['g','gen','generate']:
                cmd = cmd_generate()
                if cmd == '':
                    continue
                cmd += '\n'
                os.write(master_fd, cmd.encode())
                cmd_watch()
            elif cmd in ['e','exec']:
                cmd = cmd_exec()
                if cmd:
                    cmd += '\n'
                    os.write(master_fd, cmd.encode())
                    cmd_watch()
            elif cmd in ['i','input']:
                cmd = cmd_exec('input')
                if cmd:
                    os.write(master_fd, cmd.encode())
                    cmd_watch()
            else:
                read_line(f"{cmd}: command not found", begin='\033[2K\r', max_chars=1)
    finally:
        mode = 'char'

def char_mode():
    global mode
    char = ''
    o = os.read(sys.stdin.fileno(), 10240)
    i = None
    if len(o) == 1:
        i = o[0]
    if i == 5:
        mode = 'line'
    elif i == 6:
        mode = 'prompt'
    else:
        char = o.decode()
    return char

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

