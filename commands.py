"""
commands.py
"""

import os
import time
import termios
import traceback
import sys
import signal
from terminal import print_screen_perfect
from ai.registry import to_ai_type
from display import show_line, read_line, read_lines, clear_lines, print_lines, record_line
from common import *

# ====== 输入模式控制 ======

def read_command(state):
    if state.mode == 'char':
        cmd = char_mode(state)
    elif state.mode == 'line':
        cmd = line_mode(state)
    elif state.mode == 'prompt':
        cmd = prompt_mode(state)
    return cmd

def prompt_mode(state):
    """
    进入AI生成模式，返回生成命令
    """
    try:
        return cmd_generate(state, default='i')[0]
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
        return ''
    finally:
        print_context(state)
        state.mode = 'char'

def line_mode(state):
    """
    命令行模式，支持丰富命令分发与AI交互
    """
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
                    return cmd_quit(state)
                elif cmd in ['s','show','status']:
                    cmd_show_status(state)
                elif cmd in ['r','raw']:
                    cmd_raw(state)
                elif cmd in ['ch','chat']:
                    cmd_chat(state)
                elif cmd in ['reset']:
                    return cmd_reset(state)
                elif cmd in ['c','clear']:
                    cmd_clear(state)
                elif cmd in ['w','watch']:
                    cmd_watch(state)
                elif cmd in ['g','gen','generate']:
                    result = cmd_generate_wrap(state, args)
                    if result is None:
                        continue
                elif cmd in ['e','exec']:
                    cmd_exec_wrap(state, args)
                elif cmd in ['i','input']:
                    cmd_input(state, args)
                elif cmd in ['esc']:
                    cmd_esc(state, args)
                elif cmd in ['t','tty']:
                    return cmd_tty(state)
                elif cmd in ['a','auto']:
                    cmd_auto(state, args)
                elif cmd in ['err']:
                    cmd_err(state)
                elif cmd in ['conf','config','configs']:
                    cmd_conf(state)
                elif cmd in ['set']:
                    cmd_set(state, args)
                elif cmd in ['get']:
                    cmd_get(state, args)
                elif cmd in ['m','mode']:
                    cmd_mode(state, args)
                elif cmd in ['create']:
                    cmd_create(state)
                elif cmd in ['remove','del','delete']:
                    cmd_remove(state, args)
                elif cmd in ['rename']:
                    cmd_rename(state)
                elif cmd in ['l','ls']:
                    cmd_ls(state)
                else:
                    cmd_not_found(cmd)
            except Exception as e:
                print('error:', e, end='\r\n')
                state.err = traceback.format_exc()
    finally:
        state.mode = 'char'

def char_mode(state):
    """
    字符模式，逐字符读取，支持模式切换
    """
    try:
        output = ''
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in ['\005']:
                state.mode = 'line'
            elif c in ['\007']:
                state.mode = 'prompt'
            else:
                output += c
        return output
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
        return ''

# ====== 命令行模式（line_mode）下的所有命令，供line_mode函数调用。 ======

def read_instruct(prompt, value='', state=None):
    """
    读取AI指令，支持/命令切换模式
    prompt: 提示符字符串
    value: 默认输入内容
    state: 全局状态对象
    返回用户输入的指令字符串，支持以/开头的内置命令（如/show、/set等）
    """
    instruct = None
    while instruct is None:
        instruct = read_line(f'({prompt}-instruct): ', cancel='', include_last=False, value=value, id='instruct')
        value = instruct
        instruct = instruct.strip()
        if instruct[:1] == '/':
            cmd = instruct[1:]
            args = None
            instruct = None
            value = ''
            if ' ' in cmd and cmd[:1] != ' ':
                index = cmd.find(' ')
                args = cmd[index+1:].strip()
                cmd = cmd[:index].strip()
            if cmd in ['s','show','status']:
                cmd_show(state)
            elif cmd in ['set']:
                cmd_set(state, args)
            elif cmd in ['get']:
                cmd_get(state, args)
            elif cmd in ['m','mode']:
                cmd_mode(state, args)
            elif cmd in ['c','ch','chat']:
                instruct = args
                cmd_mode(state, 'chat')
            elif cmd in ['t', 'text']:
                instruct = args
                cmd_mode(state, 'text')
    return instruct

def cmd_quit(state):
    """
    退出当前模式，恢复终端显示。
    """
    print_context(state)
    return ''

def cmd_show_status(state):
    """
    显示当前屏幕内容。
    """
    cmd_show(state)

def cmd_raw(state):
    """
    清屏并以原始格式输出当前屏幕内容。
    """
    print('\033[2J\033[H\r', end='')
    print(state.screen.raw(), end='\r\n')

def cmd_chat(state):
    """
    清屏并显示AI对话内容。
    """
    print('\033[2J\033[H\r', end='')
    state.ai.print()

def cmd_reset(state):
    """
    重置终端和屏幕状态，恢复初始模式。
    """
    print_context(state)
    termios.tcsetattr(state.slave_fd, termios.TCSADRAIN, state.slave_tty)
    state.screen.mode = 'normal'
    state.screen.esc = ''
    return ''

def cmd_clear(state):
    """
    清屏。
    """
    print('\033[2J\033[H\r', end='')

def cmd_generate_wrap(state, args):
    """
    包装AI生成命令，自动写入子进程并刷新显示。
    """
    cmd = cmd_generate(state, args)[0]
    if cmd == '':
        return None
    os.write(state.master_fd, cmd.encode())
    time.sleep(0.1)
    cmd_show(state)
    return cmd

def cmd_exec_wrap(state, args):
    """
    包装命令执行，自动保存历史并写入子进程。
    """
    cmd, instruct = cmd_exec(state, cmd=args)
    if cmd:
        state.ai.save(instruct, state.screen.text(), cmd)
        cmd += '\n'
        os.write(state.master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show(state)
    return cmd

def cmd_input(state, args):
    """
    执行input命令，将输入内容写入子进程并刷新显示。
    """
    cmd, instruct = cmd_exec(state, 'input', cmd=args, id='cmd_input')
    if cmd:
        os.write(state.master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show(state)
    return cmd

def cmd_esc(state, args):
    """
    处理esc相关子命令，显示或调试终端转义序列。
    """
    if args is None:
        args = 'show'
    if args in ['e','err','error']:
        print('\033[2J\033[H\r', end='')
        if len(state.screen.esc_err) == 0:
            print('no catched unknown escape sequences', end='\r\n')
        else:
            print('catched unknown escape sequences:', end='\r\n')
        for esc in state.screen.esc_err:
            print('esc:', esc.encode(), end='\r\n')
    elif args in ['s','save','saved']:
        print('\033[2J\033[H\r', end='')
        if len(state.screen.esc_record) == 0:
            print('no saved escape sequences', end='\r\n')
        else:
            print('saved escape sequences:', end='\r\n')
        for esc in state.screen.esc_record:
            print('esc:', esc, end='\r\n')
    elif args in ['d','debug']:
        state.screen.esc_debug = not state.screen.esc_debug
        print(f'debug mode: {state.screen.esc_debug}', end='\r\n')
    elif args in ['show','status']:
        print(f'debug mode: {state.screen.esc_debug}', end='\r\n')
    else:
        print('usage: esc [err|saved|status|debug]', end='\r\n')

def cmd_conf(state):
    """
    显示AI配置。
    """
    state.ai.printConfigs(end='\r\n')

# ========== 命令实现 =============
def cmd_ls(state):
    """
    列出所有AI实例及其状态。
    """
    info = 'STATUS\tID\tTYPE\r\n'
    for id in state.ai.ais.keys():
        a = state.ai.ais[id]
        if state.ai.ai == a:
            info += f" [*]\t{id}\t{type(a).__name__}\r\n"
        else:
            info += f" [ ]\t{id}\t{type(a).__name__}\r\n"
    show_line(info)

def cmd_create(state, id=None, type=None):
    """
    创建新AI实例。
    """
    if not id:
        id = read_line('(create-ai) id: ', cancel='', include_last=False)
        if not id:
            return
    if not type:
        type = read_line('(create-ai) type: ', cancel='', include_last=False)
        if not type:
            return
    try:
        t = to_ai_type(type)
        a = t()
    except:
        print(f"no such ai type '{type}'")
        return
    state.ai.add(id, a)
    state.ai.switch(id)
    show_line(f"created new ai '{id}'")

def cmd_remove(state, id=None):
    """
    删除指定AI实例。
    """
    if not id:
        ids = '[' + ','.join(state.ai.ais.keys()) + ']'
        for id in state.ai.ais.keys():
            if state.ai.ais[id] == state.ai.ai:
                break
        id = read_line(f"(remove-ai) current ai is '{id}' {ids} ", cancel='', include_last=False)
        if not id:
            return
    state.ai.remove(id)
    show_line(f"removed ai '{id}'")

def cmd_rename(state, id=None, new_id=None):
    """
    重命名AI实例。
    """
    if not id:
        ids = '[' + ','.join(state.ai.ais.keys()) + ']'
        for id in state.ai.ais.keys():
            if state.ai.ais[id] == state.ai.ai:
                break
        id = read_line(f"(rename-ai) current ai is '{id}' {ids} ", cancel='', include_last=False)
        if not id:
            return
    if not new_id:
        new_id = read_line(f"(rename-ai) selected ai '{id}', new id: ", cancel='', include_last=False)
        if not new_id:
            return
    state.ai.rename(id, new_id)
    show_line(f"renamed ai '{id}' to '{new_id}'")

def cmd_mode(state, id, quiet=True, end='\r\n'):
    """
    切换当前AI实例。
    """
    info = ''
    ids = '[' + ','.join(state.ai.ais.keys()) + ']'
    if not id:
        for id in state.ai.ais.keys():
            if state.ai.ais[id] == state.ai.ai:
                info = f"(select-ai) current ai is '{id}' {ids} "
                break
        id = read_line(info, cancel='', include_last=False)
    if not id:
        return
    if id in state.ai.ais:
        state.ai.switch(id)
        info = f"change ai to '{id}'"
    else:
        info = f"no such ai '{id}' {ids}"
    if info:
        if quiet:
            show_line(info)
        else:
            print(info, end=end)

def cmd_not_found(cmd):
    """
    未知命令处理，输出错误提示。
    """
    show_line(f"{cmd}: command not found")

def cmd_exec(state, prompt='cmd', cmd=None, id='cmd'):
    """
    读取并执行命令，支持#分割指令与注释。
    返回(cmd, instruct)。
    """
    if cmd is None:
        cmd = read_line(f'({prompt}): ', cancel='', include_last=False, id=id)
    else:
        record_line(cmd, id=id)
    instruct = None
    if '#' in cmd:
        args = cmd.split('#')
        cmd = args[0].strip()
        instruct = args[-1].strip()
    print('\033[2K\r', end='')
    return cmd, instruct

def cmd_generate(state, instruct=None, prompt='gen', default='u'):
    """
    AI生成命令主流程，支持多轮确认、编辑、重试等。
    返回(cmd, instruct)。
    """
    if instruct is None:
        instruct = read_instruct(prompt, state=state)
    else:
        record_line(instruct, id='instruct')
    if instruct == '':
        return '', ''
    context = state.screen.text()
    cmd, think = '', ''
    if '#' in instruct:
        args = instruct.split('#')
        instruct = args[0].strip()
        cmd = args[-1].strip()
        output = None
    else:
        output = state.ai.generate(instruct, context)
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
            try:
                prev_len = 0
                for chunk in cancelable(output):
                    gen_cmd, gen_think = chunk[0], chunk[1]
                    if gen_cmd:
                        text = f'({prompt}-cmd): ' + gen_cmd
                    elif gen_think:
                        text = f'({prompt}-think): ' + gen_think
                    else:
                        text = f'({prompt}-cmd): waiting...'
                    clear_lines(lines_all, lines_cur, clear=len(text)<prev_len)
                    lines_all, lines_cur = print_lines(text)
                    prev_len = len(text)
                del prev_len
            except KeyboardInterrupt as e:
                cancelled = True
            lines_all, lines_cur = clear_lines(lines_all, lines_cur)
            if not cancelled:
                cmd, think = gen_cmd, gen_think
            else:
                if gen_cmd == '' and gen_think != '':
                    cmd, think = gen_think, ''
                else:
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
            output = state.ai.generate(instruct, context)
        elif confirm in ['e','edit']:
            instruct = read_instruct(prompt, value=instruct, state=state)
            if instruct == '':
                cmd = ''
                break
            output = state.ai.generate(instruct, context)
        elif confirm in ['t','teach']:
            default = 'y'
            cmd = read_line(f'({prompt}-cmd): ', include_last=False, id='cmd')
            if cmd == '':
                break
        elif confirm in ['s','show','status']:
            cmd_show(state)
        elif confirm in ['cancel']:
            if time.time() - gen_time > 0.6:
                cmd = ''
                break
            gen_time = time.time()
        else:
            confirm_info = ", please input 'y' or 'n':"
    if save:
        save_history(state, instruct, context, cmd)
    if cmd:
        state.ai.save(instruct, context, cmd)
        if enter:
            cmd += '\n'
    return cmd, instruct

def cmd_watch(state):
    """
    监控屏幕内容变化，定时刷新显示，支持多种快捷键操作。
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
        if c in ['\x03','\x04','q']:
            break
        elif c in ['g']:
            cmd = cmd_generate(state)[0]
            if cmd:
                os.write(state.master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['e']:
            cmd, instruct = cmd_exec(state)
            if cmd:
                state.ai.save(instruct, state.screen.text(), cmd)
                cmd += '\n'
                os.write(state.master_fd, cmd.encode())
                time.sleep(0.1)
        elif c in ['i']:
            cmd, instruct = cmd_exec(state, 'input', id='cmd_input')
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

def cmd_show(state, **kwargs):
    """
    清屏并完美显示当前屏幕内容。
    """
    print('\033[2J\033[H\r', end='')
    print_screen_perfect(state.screen, end='\r\n', **kwargs)

def cmd_err(state):
    """
    显示最近捕获的错误信息。
    """
    print('\033[2J\033[H\r', end='')
    if state.err is None:
        print('no catched error', end='\r\n')
    else:
        print('catched error:', end='\r\n')
        os.write(sys.stdout.fileno(), state.err.replace('\n', '\r\n').encode())

def cmd_tty(state):
    """
    进入原始终端显示模式，支持回调实时刷新。
    """
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

def cmd_auto(state, instruct):
    """
    自动AI生成并执行命令，适合批量/自动化场景。
    """
    if instruct is None:
        instruct = read_instruct('auto', state=state)
    else:
        record_line(instruct, id='auto-instruct')
    if instruct == '':
        return ''
    while True:
        cmd, instruct = cmd_generate(state, instruct, prompt='auto')
        if cmd == '':
            break
        cmd += '\n'
        os.write(state.master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show(state)

def cmd_get(state, args):
    """
    获取AI配置项。
    """
    try:
        if not args:
            state.ai.printConfigs()
        else:
            key = args
            value = str(state.ai.get(key)).replace('\n', '\r\n')
            print(f'{key} = {value}')
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()

def cmd_set(state, args):
    """
    设置AI配置项。
    """
    try:
        i = args.index(' ')
        key = args[:i].strip()
        value = args[i:].strip()
    except:
        key = args
        value = None
    if not key:
        print('usage: set [key] [value]', end='\r\n')
        return
    if not value:
        value = str(state.ai.get(key))
        value = read_lines(f"{key}> ", cancel='__cancel__', value=value)
        if value == '__cancel__':
            return
    try:
        display = value.replace('\n', '\\n')
        if len(display) > 30:
            display = display[:30] + '...'
        print(f'set {key} = {display}', end='\r\n')
        state.ai.set(key, value)
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
