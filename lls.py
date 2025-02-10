#!/usr/bin/env python3

import unicodedata
import subprocess
import threading
import termios
import string
import tty
import pty
import sys
import os

from generate import generate_cmd

old_tty = termios.tcgetattr(sys.stdin)
tty.setraw(sys.stdin.fileno())
master_fd, slave_fd = pty.openpty()

command = ['bash', '-i']
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

context = ''
exit = False

def read_stdout():
    global context
    while not exit:
        try:
            chars = os.read(master_fd, 10240)
            if chars:
                os.write(sys.stdout.fileno(), chars)
                if len(chars) == 1 and chars[0] == 127:
                    context = context[:-1]
                else:
                    context += chars.decode()
        except Exception as e:
            print('error: ' + e)

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

mode = 'char'

def print_context():
    global context
    last_line = context.split('\n')[-1]
    os.write(sys.stdout.fileno(), ('\033[1K\r' + last_line).encode())

def read_line(prompt=':', include_last=True, max_chars=-1, cancel=None, begin='\n', value=''):
    global mode
    if begin:
        os.write(sys.stdout.fileno(), begin.encode())
    line = value
    os.write(sys.stdout.fileno(), (f'\r{prompt}' + line).encode())
    while True:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for o in chars:
            i = o.encode()[0]
            if cancel is not None and i in [3, 4]:
                return cancel
            if i in [3, 4, 13]:
                os.write(sys.stdout.fileno(), f'\r{prompt}'.encode())
                if include_last:
                    line += o
                return line
            elif len(line) > 0 and i == 127:
                line = line[:-1]
                last_line = (f'{prompt}' + line).split('\n')[-1]
                os.write(sys.stdout.fileno(), (f'\033[1K\r{last_line}').encode())
            elif unicodedata.category(o)[0] != "C":
                line += o
                os.write(sys.stdout.fileno(), o.encode())
            if max_chars != -1 and len(line) >= max_chars:
                return line

def save_history(prompt, context, cmd):
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print(e)

def prompt_mode():
    global context, mode
    mode = 'char'
    prompt = read_line('(gen-prompt): ', cancel='', begin='\033[1K\r', include_last=False)
    if prompt == '':
        print_context()
        return ''
    cmd, think = generate_cmd(prompt, context)
    confirm_info = ', confirm?'
    flags = '[y/u/n/e/r/k/t]'
    default = 'u'
    save = False
    show_think = False
    while True:
        cmd_display = cmd.replace('\n', '\n\r')
        flags_display = flags.replace(default, default.upper())
        confirm = read_line(f'(gen-cmd): {cmd_display}{confirm_info} {flags_display} ', cancel='n', begin='\033[1K\r', include_last=False)
        if confirm == '':
            confirm = default 
        if confirm.lower() == 'y' or confirm.lower() == 'yes':
            save = True
            break
        elif confirm.lower() == 'u' or confirm.lower() == 'use':
            break
        elif confirm.lower() == 'k' or confirm.lower() == 'think':
            think_display = think.replace('\n', '\n\r')
            os.write(sys.stdout.fileno(), b'\r')
            os.write(sys.stdout.fileno(), think_display.encode())
        elif confirm.lower() == 'n' or confirm.lower() == 'no':
            cmd = ''
            break
        elif confirm.lower() == 'r' or confirm.lower() == 're':
            cmd, think = generate_cmd(prompt, context)
        elif confirm.lower() == 'e' or confirm.lower() == 'edit':
            prompt = read_line('(gen-prompt): ', begin='\033[1K\r', include_last=False, value=prompt)
            if prompt == '':
                break
            cmd, think = generate_cmd(prompt, context)
        elif confirm.lower() == 't' or confirm.lower() == 'teach':
            default = 'y'
            cmd = read_line(f'(gen-cmd): ', begin='\033[1K\r', include_last=False)
            if cmd == '':
                break
        else:
            confirm_info = ", please input 'y' or 'n':"
    print_context()
    if save:
        save_history(prompt, context, cmd)
    return cmd

def line_mode():
    global mode
    mode = 'char'
    cmd = read_line(begin='\033[1K\r', include_last=False)
    print_context()
    return cmd

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
            print('error: ' + e)
            mode = 'char'
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    exit = True
    print('exited, if not exit, please input ctrl-c again\n')

