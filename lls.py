#!/usr/bin/env python3

import importlib.util
import subprocess
import traceback
import sys
import os

config_file_path = os.path.join(os.environ['HOME'], '.llsrc.py')
err = None

if os.path.exists(config_file_path):
    try:
        spec = importlib.util.spec_from_file_location(name='lls_config', location=config_file_path)
        lls_config_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lls_config_module)
    except Exception as e:
        print('error:', e, file=sys.stderr)
        err = traceback.format_exc()

if len(sys.argv) > 2 and sys.argv[1] == '--':
    main_cmd = sys.argv[2]
    argv = sys.argv[3:]
else:
    if os.path.realpath(os.environ['SHELL']) != os.path.realpath(sys.argv[0]):
        main_cmd = os.environ['SHELL']
    else:
        main_cmd = os.environ.get('LLS_FALLBACK_SHELL', 'bash')
    argv = sys.argv[1:]

command = [main_cmd, *argv]

if not sys.stdin.isatty():
    result_code = subprocess.call(command)
    exit(result_code)

import unicodedata
import threading
import termios
import string
import time
import tty
import pty

import signal
import struct
import fcntl

from chat import Chat, print_chat_perfect
from terminal import Screen, print_screen_perfect
from display import wrap_multi_lines

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
slave_callback = None
slave_tty = termios.tcgetattr(slave_fd)

def read_stdout():
    global mode, err
    while running:
        try:
            chars = os.read(master_fd, 10240)
            if chars:
                if mode != 'line':
                    os.write(sys.stdout.fileno(), chars)
                screen.write(chars)
            if slave_callback is not None:
                slave_callback()
        except Exception as e:
            print('error:', e, end='\r\n')
            err = traceback.format_exc()

stdout_thread = threading.Thread(target=read_stdout)
stdout_thread.daemon = True
stdout_thread.start()

def print_context():
    if screen.buffer == 'main' and screen.y == len(screen.lines) - 1:
        line = screen._raw.split('\n')[-1]
        os.write(sys.stdout.fileno(), ('\033[2K\r' + line).encode())
    else:
        os.write(sys.stdout.fileno(), ('\033[2K\r' + screen._raw).encode())

def clear_lines(lines_all, lines_cur):
    if lines_all != lines_cur:
        for _ in range(lines_all - lines_cur):
            os.write(sys.stdout.fileno(), b'\r\033[1B')
    for _ in range(lines_all - 1):
        os.write(sys.stdout.fileno(), b'\033[2K\r\033[1A')
    os.write(sys.stdout.fileno(), b'\033[2K\r')
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

def record_line(value, id):
    read_line(value=value, id=id, skip_input=True)

def read_line(prompt=':', include_last=True, max_chars=-1, value='', begin=None, cancel=None, backspace=None, id=None, no_save=None, skip_input=False):
    if id is not None:
        buf = bufs.get(id)
        if buf is None:
            buf = Screen()
            buf.insert_mode = True
            buf.limit_move = True
            buf.max_height = 1
            buf.auto_move_to_end = True
            bufs[id] = buf
    else:
        buf = Screen()
        buf.insert_mode = True
        buf.limit_move = True
        buf.max_height = 1
        buf.auto_move_to_end = True
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
                if cancel is not None and c in ['\x03','\x04']:
                    cancelled = True
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
    if id is not None:
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

def save_history(prompt, context, cmd):
    global err
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print('error:', e, end='\r\n')
        err = traceback.format_exc()

def cmd_generate(instrct=None, prompt='gen'):
    if instrct is None:
        instrct = read_line(f'({prompt}-instrct): ', cancel='', include_last=False, id='instrct')
    else:
        record_line(instrct, id='instrct')
    if instrct == '':
        return '', ''
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
    flags = '[y/u/n/e/s/r/k/t]'
    default = 'u'
    save = False
    show_think = False
    while True:
        if output is not None:
            lines_all, lines_cur = 1, 1
            os.write(sys.stdout.fileno(), f'\033[2K\r({prompt}-cmd): waiting...'.encode())
            for chunk in output:
                clear_lines(lines_all, lines_cur)
                cmd, think = chunk[0], chunk[1]
                if cmd == '':
                    text = f'({prompt}-think): ' + think
                else:
                    text = f'({prompt}-cmd): ' + cmd
                lines_all, lines_cur = print_lines(text)
            lines_all, lines_cur = clear_lines(lines_all, lines_cur)
            output = None
        if cmd:
            record_line(cmd, id='cmd')
        flags_text = flags.replace(default, default.upper())
        text = f'({prompt}-cmd): {cmd}{confirm_info} {flags_text} '
        if show_think:
            text = f"({prompt}-think): {think}\n{text}"
        show_think = False
        prefix_info = ''
        confirm_info = ', confirm?'
        confirm = read_line(text, cancel='n', include_last=False)
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
            instrct = read_line(f'({prompt}-instrct): ', cancel='', include_last=False, value=instrct, id='instrct')
            if instrct == '':
                cmd = ''
                break
            output = chat.try_generate(instrct, context)
        elif confirm in ['t','teach']:
            default = 'y'
            cmd = read_line(f'({prompt}-cmd): ', include_last=False, id='cmd')
            if cmd == '':
                break
        elif confirm in ['s','show','status']:
            cmd_show()
        else:
            confirm_info = ", please input 'y' or 'n':"
    if save:
        save_history(instrct, context, cmd)
    if cmd:
        chat.add_chat(instrct, context, cmd)
    return cmd, instrct

def cmd_exec(prompt='cmd', cmd=None, id='cmd'):
    if cmd is None:
        cmd = read_line(f'({prompt}): ', cancel='', include_last=False, id=id)
    else:
        record_line(cmd, id=id)
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
            cmd_show()
            print('\r\n')
        time_text = time.asctime(time.localtime(time.time()))
        print(f'\033[1A\033[2K\rEvery 2.0s: show\t{time_text}', end='\r\n')
        signal.setitimer(signal.ITIMER_REAL, 2)
    signal.signal(signal.SIGALRM, show_screen)
    show_screen()
    while True:
        c = read_line('', max_chars=1, backspace='b')
        signal.setitimer(signal.ITIMER_REAL, 0)
        if c in ['\x03','\x04','q']:
            break
        elif c in ['g']:
            cmd = cmd_generate()[0]
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
            cmd, instrct = cmd_exec('input', id='cmd_input')
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
    #print('\033[2K\r^C')
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    del total_chars

def cmd_show(**kwargs):
    print('\033[2J\033[H\r', end='')
    print_screen_perfect(screen, end='\r\n', **kwargs)

def cmd_err():
    global err
    print('\033[2J\033[H\r', end='')
    if err is None:
        print('no catched error', end='\r\n')
    else:
        print('catched error:', end='\r\n')
        os.write(sys.stdout.fileno(), err.replace('\n', '\r\n').encode())

def cmd_tty():
    global slave_callback
    def callback_fun():
        cmd_show(raw=True)
    os.write(sys.stdout.fileno(), b'\033[?25l')
    callback_fun()
    slave_callback = callback_fun
    run = True
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in ['\005']:
                run = False
                break
            os.write(master_fd, c.encode())
        if not run:
            break
    slave_callback = None
    os.write(sys.stdout.fileno(), b'\033[?25h')
    print_context()

def cmd_auto(instrct):
    if instrct is None:
        instrct = read_line('(auto-instrct): ', cancel='', include_last=False, id='auto-instrct')
    else:
        record_line(instrct, id='auto-instrct')
    if instrct == '':
        return ''
    while True:
        cmd, instrct = cmd_generate(instrct, prompt='auto')
        if cmd == '':
            break
        cmd += '\n'
        os.write(master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show()
    #print('\033[2K\r^C')

def prompt_mode():
    global mode
    try:
        return cmd_generate()[0]
    finally:
        print_context()
        mode = 'char'

def line_mode():
    global mode
    try:
        cmd = ''
        args = ''
        while True:
            cmd = read_line(cancel='q', include_last=False, id='line_mode', no_save=['q'])
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
                cmd = cmd_generate(args)[0]
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
                cmd, instrct = cmd_exec('input', cmd=args, id='cmd_input')
                if cmd:
                    os.write(master_fd, cmd.encode())
                    time.sleep(0.1)
                    cmd_show()
            elif cmd in ['esc']:
                if args is None:
                    args = 'show'
                if args in ['e','err','error']:
                    print('\033[2J\033[H\r', end='')
                    if len(screen.esc_err) == 0:
                        print('no catched unknown escape sequences', end='\r\n')
                    else:
                        print('catched unknown escape sequences:', end='\r\n')
                    for esc in screen.esc_err:
                        print('esc:', esc.encode(), end='\r\n')
                elif args in ['s','save','saved']:
                    print('\033[2J\033[H\r', end='')
                    if len(screen.esc_record) == 0:
                        print('no saved escape sequences', end='\r\n')
                    else:
                        print('saved escape sequences:', end='\r\n')
                    for esc in screen.esc_record:
                        print('esc:', esc, end='\r\n')
                elif args in ['d','debug']:
                    screen.esc_debug = not screen.esc_debug
                    print(f'debug mode: {screen.esc_debug}', end='\r\n')
                elif args in ['show','status']:
                    print(f'debug mode: {screen.esc_debug}', end='\r\n')
                else:
                    print('usage: esc [err|saved|status|debug]', end='\r\n')
            elif cmd in ['t','tty']:
                cmd_tty()
                return ''
            elif cmd in ['a','auto']:
                cmd_auto(args)
            elif cmd in ['err']:
                cmd_err()
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
            err = traceback.format_exc()
            mode = 'char'
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    running = False
    print('exited, if not exit, please input ctrl-c again')

