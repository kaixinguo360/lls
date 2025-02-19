#!/usr/bin/env python3

import importlib.util
import subprocess
import traceback
import sys
import os

err = None

config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.llsrc.py')
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
    if 'SHELL' in os.environ and os.path.realpath(os.environ['SHELL']) != os.path.realpath(sys.argv[0]):
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
import select
import queue
import time
import json
import tty
import pty

import signal
import struct
import fcntl

from chat import ChatAI
from generate import MixedAI, TextCompletionAI
from terminal import Screen, print_screen_perfect
from display import wrap_multi_lines

old_tty = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin.fileno())
master_fd, slave_fd = pty.openpty()
winsize = os.get_terminal_size()
ai = MixedAI()
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

def load_bufs():
    global bufs, err
    try:
        history_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_history')
        with open(history_file_path, 'r') as f:
            history = json.load(f)
        for id in history.keys():
            buf = Screen()
            buf.insert_mode = True
            buf.limit_move = True
            buf.max_height = 1
            buf.auto_move_to_end = True
            buf.lines = history.get(id)
            buf.x = 0
            buf.y = len(buf.lines) - 1
            bufs[id] = buf
    except Exception as e:
        print('error: load history failed', end='\r\n')
        err = 'load history failed\n' + traceback.format_exc()

def save_bufs():
    global bufs, err
    try:
        history_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_history')
        history = {}
        for id in bufs.keys():
            buf = bufs.get(id)
            history[id] = buf.lines
        text = json.dumps(history)
        with open(history_file_path, 'w') as f:
            f.write(text)
    except Exception as e:
        print('error: save history failed', end='\r\n')
        err = 'save history failed\n' + traceback.format_exc()

def record_line(value, id):
    read_line(value=value, id=id, skip_input=True)

def read_line(prompt=':', include_last=True, max_chars=-1, value='', begin=None, cancel=None, exit=None, backspace=None, id=None, no_save=None, skip_input=False):
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

def check_cancel(cancel_chars=['\x03','\x04']):
    f, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
    if sys.stdin.fileno() in f:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in cancel_chars:
                return True
    return False

def cancelable(generator):
    q = queue.Queue()
    is_exit = False
    def read_fun():
        try:
            for i in generator:
                if is_exit:
                    generator.close()
                    return
                q.put(i)
        finally:
            q.put(generator) # End of generator
    read_thread = threading.Thread(target=read_fun)
    read_thread.start()
    try:
        while True:
            if check_cancel():
                break
            if not q.empty():
                i = q.get_nowait()
                if i == generator: # End of generator
                    break
                else:
                    yield i
    except GeneratorExit:
        generator.close()
    finally:
        is_exit = True

def read_instrct(prompt, value=''):
    global ai
    instrct = None
    while instrct is None:
        instrct = read_line(f'({prompt}-instrct): ', cancel='', include_last=False, value=value, id='instrct')
        value = instrct
        instrct = instrct.strip()
        if instrct[:1] == '/':
            cmd = instrct[1:]
            args = None
            instrct = None
            value = ''
            if ' ' in cmd and cmd[:1] != ' ':
                index = cmd.find(' ')
                args = cmd[index+1:].strip()
                cmd = cmd[:index].strip()
            if cmd in ['s','show','status']:
                cmd_show()
            elif cmd in ['set']:
                cmd_set(args)
            elif cmd in ['get']:
                cmd_get(args)
            elif cmd in ['m','mode']:
                cmd_mode(args)
            elif cmd in ['c','ch','chat']:
                instrct = args
                cmd_mode('chat')
            elif cmd in ['t', 'text']:
                instrct = args
                cmd_mode('text')
    return instrct

def cmd_ls():
    global ai
    info = 'STATUS\tID\tTYPE\r\n'
    for id in ai.ais.keys():
        a = ai.ais[id]
        if ai.ai == a:
            info += f" [*]\t{id}\t{type(a).__name__}\r\n"
        else:
            info += f" [ ]\t{id}\t{type(a).__name__}\r\n"
    read_line(info, max_chars=1, backspace='b')

def cmd_create(id=None, type=None):
    global ai
    if not id:
        id = read_line('(create-ai) id: ', cancel='', include_last=False)
        if not id:
            return
    if not type:
        type = read_line('(create-ai) type: ', cancel='', include_last=False)
        if not type:
            return
    if type in ['t','text']:
        a = TextCompletionAI()
    elif type in ['c','ch','chat']:
        a = ChatAI()
    else:
        a = TextCompletionAI()
    ai.add(id, a)
    ai.switch(id)
    print(f"created new ai '{id}'")

def cmd_mode(id, quiet=True, end='\r\n'):
    global ai
    info = ''
    ids = '[' + ','.join(ai.ais.keys()) + ']'
    if not id:
        for id in ai.ais.keys():
            if ai.ais[id] == ai.ai:
                info = f"(select-ai) current ai is '{id}' {ids} "
                break
        id = read_line(info, cancel='', include_last=False)
    if not id:
        return
    if id in ai.ais:
        ai.switch(id)
        info = f"change ai to '{id}'"
    else:
        info = f"no such ai '{id}' {ids}"
    if info:
        if quiet:
            read_line(info, max_chars=1, backspace='b')
        else:
            print(info, end=end)

def cmd_generate(instrct=None, prompt='gen', default='u'):
    if instrct is None:
        instrct = read_instrct(prompt)
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
        output = ai.generate(instrct, context)
    confirm_info = ', confirm?'
    flags = '[y/u/i/n/e/s/r/k/t]'
    save = False
    show_think = False
    cancel_confirm = False
    gen_time = time.time()
    enter = True
    while True:
        if output is not None:
            lines_all, lines_cur = 1, 1
            os.write(sys.stdout.fileno(), f'\033[2K\r({prompt}-cmd): waiting...'.encode())
            cancelled = False
            gen_cmd, gen_think = '', ''
            for chunk in cancelable(output):
                clear_lines(lines_all, lines_cur)
                gen_cmd, gen_think = chunk[0], chunk[1]
                if gen_cmd:
                    text = f'({prompt}-cmd): ' + gen_cmd
                elif gen_think:
                    text = f'({prompt}-think): ' + gen_think
                else:
                    text = f'({prompt}-cmd): waiting...'
                lines_all, lines_cur = print_lines(text)
            lines_all, lines_cur = clear_lines(lines_all, lines_cur)
            if not cancelled:
                cmd, think = gen_cmd, gen_think
            gen_time = time.time()
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
        confirm = read_line(text, cancel='cancel', exit='n', include_last=False)
        confirm = confirm.lower()
        if confirm == '':
            confirm = default
        if confirm in ['y','yes']:
            save = True
            break
        elif confirm in ['u','use']:
            break
        elif confirm in ['i','input']:
            enter = False
            break
        elif confirm in ['u','use']:
            break
        elif confirm in ['n','no','q','quit','exit']:
            cmd = ''
            break
        elif confirm in ['k','think']:
            show_think = True
        elif confirm in ['r','re','retry']:
            output = ai.generate(instrct, context)
        elif confirm in ['e','edit']:
            instrct = read_instrct(prompt, value=instrct)
            if instrct == '':
                cmd = ''
                break
            output = ai.generate(instrct, context)
        elif confirm in ['t','teach']:
            default = 'y'
            cmd = read_line(f'({prompt}-cmd): ', include_last=False, id='cmd')
            if cmd == '':
                break
        elif confirm in ['s','show','status']:
            cmd_show()
        elif confirm in ['cancel']:
            if time.time() - gen_time > 0.6:
                cmd = ''
                break
            gen_time = time.time()
        else:
            confirm_info = ", please input 'y' or 'n':"
    if save:
        save_history(instrct, context, cmd)
    if cmd:
        ai.save(instrct, context, cmd)
        if enter:
            cmd += '\n'
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
                os.write(master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['e']:
            cmd, instrct = cmd_exec()
            if cmd:
                ai.save(instrct, screen.text(), cmd)
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
        instrct = read_line('(auto-instrct): ', cancel='', include_last=False, id='instrct')
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

def cmd_get(args):
    global err
    try:
        if not args:
            ai.printConfigs()
        else:
            key = args
            value = str(ai.get(key)).replace('\n', '\r\n')
            print(f'{key} = {value}')
    except Exception as e:
        print('error:', e, end='\r\n')
        err = traceback.format_exc()

def cmd_set(args):
    global err
    try:
        i = args.index(' ')
        key = args[:i].strip()
        value = args[i:].strip()
    except:
        print('usage: set [key] [value]', end='\r\n')
        return
    try:
        ai.set(key, value)
        print(f'set {key} = {value}')
    except Exception as e:
        print('error:', e, end='\r\n')
        err = traceback.format_exc()

def prompt_mode():
    global mode, err
    try:
        return cmd_generate(default='i')[0]
    except Exception as e:
        print('error:', e, end='\r\n')
        err = traceback.format_exc()
        return ''
    finally:
        print_context()
        mode = 'char'

def line_mode():
    global mode, err
    try:
        cmd = ''
        args = ''
        while True:
            try:
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
                    ai.print()
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
                    os.write(master_fd, cmd.encode())
                    time.sleep(0.1)
                    cmd_show()
                elif cmd in ['e','exec']:
                    cmd, instrct = cmd_exec(cmd=args)
                    if cmd:
                        ai.save(instrct, screen.text(), cmd)
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
                elif cmd in ['conf','config','configs']:
                    ai.printConfigs(end='\r\n')
                elif cmd in ['set']:
                    cmd_set(args)
                elif cmd in ['get']:
                    cmd_get(args)
                elif cmd in ['m','mode']:
                    cmd_mode(args)
                elif cmd in ['create']:
                    cmd_create()
                elif cmd in ['l','ls']:
                    cmd_ls()
                else:
                    read_line(f"{cmd}: command not found", max_chars=1)
            except Exception as e:
                print('error:', e, end='\r\n')
                err = traceback.format_exc()
    finally:
        mode = 'char'

def char_mode():
    global mode, err
    try:
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
    except Exception as e:
        print('error:', e, end='\r\n')
        err = traceback.format_exc()
        return ''

def read_command():
    if mode == 'char':
        cmd = char_mode()
    elif mode == 'line':
        cmd = line_mode()
    elif mode == 'prompt':
        cmd = prompt_mode()
    return cmd

def load_ai():
    global ai
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    ai = MixedAI.from_config(config_file_path)
    if len(ai.ais) == 0:
        cmd_create('text', 'text')
        cmd_create('chat', 'chat')

def save_ai():
    global ai
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    ai.save_config(config_file_path)

load_bufs()
load_ai()

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
    save_bufs()
    save_ai()
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    running = False
    print('exited, if not exit, please input ctrl-c again')

