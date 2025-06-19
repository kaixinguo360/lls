#!/usr/bin/env python3
"""
lls.py
主入口模块，负责流程控制、命令分发、AI管理、终端交互。
"""

# ====== 标准库与自定义模块导入 ======
import importlib.util
import subprocess
import traceback
import sys
import os

class LLSState:
    def __init__(self):
        self.err = None  # 错误信息缓存
        self.old_tty = None  # 原始终端设置
        self.master_fd = None
        self.slave_fd = None
        self.winsize = None
        self.ai = None
        self.screen_history_file_path = None
        self.screen = None
        self.running = True
        self.mode = 'char'
        self.slave_callback = None
        self.slave_tty = None
        self.bufs = None
        self.proc = None
        self.command = None
        self.total_chars = 0

# 初始化状态对象
state = LLSState()

# ====== 加载用户自定义配置（如有） ======
config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.llsrc.py')
if os.path.exists(config_file_path):
    try:
        # 动态加载配置模块
        spec = importlib.util.spec_from_file_location(name='lls_config', location=config_file_path)
        lls_config_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lls_config_module)
    except Exception as e:
        print('error:', e, file=sys.stderr)
        state.err = traceback.format_exc()

# ====== 解析命令行参数，确定主命令 ======
if len(sys.argv) > 2 and sys.argv[1] == '--':
    # 形如 python lls.py -- bash ...
    main_cmd = sys.argv[2]
    argv = sys.argv[3:]
else:
    # 若 SHELL 环境变量存在且不是本程序，则用 SHELL，否则用默认 shell
    if 'SHELL' in os.environ and os.path.realpath(os.environ['SHELL']) != os.path.realpath(sys.argv[0]):
        main_cmd = os.environ['SHELL']
    else:
        main_cmd = os.environ.get('LLS_FALLBACK_SHELL', 'bash')
    argv = sys.argv[1:]

state.command = [main_cmd, *argv]  # 构造命令行

# ====== 非交互模式直接执行命令并退出 ======
if not sys.stdin.isatty():
    result_code = subprocess.call(state.command)
    exit(result_code)

# ====== 终端与AI环境初始化 ======
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
from generate import MixedAI, TextCompletionAI, to_ai_type
from terminal import Screen, print_screen_perfect
from display import *

# 保存原始终端设置，便于恢复
state.old_tty = termios.tcgetattr(sys.stdin)
# 设置终端为原始模式，便于逐字符读取
tty.setraw(sys.stdin.fileno())
# 创建伪终端对，主从fd
state.master_fd, state.slave_fd = pty.openpty()
# 获取终端窗口大小
state.winsize = os.get_terminal_size()
# 初始化AI对象（混合AI，支持多种模式）
state.ai = MixedAI()
# 屏幕历史文件路径
state.screen_history_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_screen_history')
# 屏幕对象，负责内容显示与缓冲
state.screen = Screen(state.screen_history_file_path)
state.screen.keep_logs_when_clean_screen = True  # 清屏时保留日志

# 设置终端窗口大小
def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

# 同步窗口大小到伪终端
def sync_winsize(*args, **kwargs):
    state.winsize = os.get_terminal_size()
    state.screen.max_height = state.winsize.lines
    set_winsize(state.slave_fd, state.winsize.lines, state.winsize.columns)

sync_winsize()
signal.signal(signal.SIGWINCH, sync_winsize)  # 监听窗口大小变化

# ====== 启动主命令子进程 ======
try:
    state.proc = subprocess.Popen(
        state.command,
        preexec_fn=os.setsid,
        stdin=state.slave_fd,
        stdout=state.slave_fd,
        stderr=state.slave_fd,
        shell=False,
        text=False,
        bufsize=0,
    )
except Exception as e:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, state.old_tty)
    raise e

state.mode = 'char'  # 输入模式：char/line/prompt
state.running = True  # 主循环运行标志
state.slave_callback = None  # 从终端回调
state.slave_tty = termios.tcgetattr(state.slave_fd)  # 保存从终端设置

# ====== 子线程异步读取子进程输出，写入主终端与屏幕缓冲 ======
def read_stdout(state):
    while state.running:
        try:
            chars = os.read(state.master_fd, 10240)
            if chars:
                if state.mode != 'line':
                    os.write(sys.stdout.fileno(), chars)
                state.screen.write(chars)
            if state.slave_callback is not None:
                state.slave_callback()
        except Exception as e:
            print('error:', e, end='\r\n')
            state.err = traceback.format_exc()

stdout_thread = threading.Thread(target=read_stdout, args=(state,))
stdout_thread.daemon = True
stdout_thread.start()

# ====== 屏幕内容辅助显示 ======
def print_context():
    if state.screen.buffer == 'main' and state.screen.y == len(state.screen.lines) - 1:
        line = state.screen._raw.split('\n')[-1]
        os.write(sys.stdout.fileno(), ('\033[2K\r' + line).encode())
    else:
        os.write(sys.stdout.fileno(), ('\033[2K\r' + state.screen._raw).encode())

# ====== 历史缓冲区管理 ======
state.bufs = get_bufs()

def load_bufs(state):
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
            state.bufs[id] = buf
    except Exception as e:
        print('error: load history failed', end='\r\n')
        state.err = 'load history failed\n' + traceback.format_exc()

def save_bufs(state):
    try:
        history_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_history')
        history = {}
        for id in state.bufs.keys():
            buf = state.bufs.get(id)
            history[id] = buf.lines
        text = json.dumps(history)
        with open(history_file_path, 'w') as f:
            f.write(text)
    except Exception as e:
        print('error: save history failed', end='\r\n')
        state.err = 'save history failed\n' + traceback.format_exc()

# 记录一行内容到缓冲区
def record_line(value, id):
    read_line(value=value, id=id, skip_input=True)

# 保存AI生成历史到命令历史文件
def save_history(state, prompt, context, cmd):
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()

# 检查是否有取消输入（如Ctrl-C、Ctrl-D）
def check_cancel(cancel_chars=['\x03','\x04']):
    f, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
    if sys.stdin.fileno() in f:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in cancel_chars:
                return True
    return False

# 生成器包装，使其可被取消（如AI流式输出时可中断）
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
                raise KeyboardInterrupt
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

# 读取AI指令，支持/命令切换模式
def read_instruct(prompt, value=''):
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

# ====== AI管理与命令分发 ======
# cmd_ls: 列出所有AI实例及其状态
def cmd_ls(state):
    info = 'STATUS\tID\tTYPE\r\n'
    for id in state.ai.ais.keys():
        a = state.ai.ais[id]
        if state.ai.ai == a:
            info += f" [*]\t{id}\t{type(a).__name__}\r\n"
        else:
            info += f" [ ]\t{id}\t{type(a).__name__}\r\n"
    show_line(info)

# cmd_create: 创建新AI实例
def cmd_create(state, id=None, type=None):
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

# cmd_remove: 删除AI实例
def cmd_remove(state, id=None):
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

# cmd_rename: 重命名AI实例
def cmd_rename(state, id=None, new_id=None):
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

# cmd_mode: 切换当前AI实例
def cmd_mode(state, id, quiet=True, end='\r\n'):
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

# cmd_generate: AI生成命令主流程，支持多轮确认、编辑、重试等
def cmd_generate(state, instruct=None, prompt='gen', default='u'):
    if instruct is None:
        instruct = read_instruct(prompt)
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
            instruct = read_instruct(prompt, value=instruct)
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

# cmd_exec: 读取并执行命令，支持#分割指令与注释
def cmd_exec(state, prompt='cmd', cmd=None, id='cmd'):
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

# cmd_watch: 监控屏幕内容变化，定时刷新显示
def cmd_watch(state):
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

# cmd_show: 清屏并完美显示当前屏幕内容
def cmd_show(state, **kwargs):
    print('\033[2J\033[H\r', end='')
    print_screen_perfect(state.screen, end='\r\n', **kwargs)

# cmd_err: 显示最近捕获的错误信息
def cmd_err(state):
    print('\033[2J\033[H\r', end='')
    if state.err is None:
        print('no catched error', end='\r\n')
    else:
        print('catched error:', end='\r\n')
        os.write(sys.stdout.fileno(), state.err.replace('\n', '\r\n').encode())

# cmd_tty: 进入原始终端显示模式，支持回调实时刷新
def cmd_tty(state):
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
    print_context()

# cmd_auto: 自动AI生成并执行命令，适合批量/自动化场景
def cmd_auto(state, instruct):
    if instruct is None:
        instruct = read_instruct('auto')
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

# ====== 主输入模式与主循环 ======
# prompt_mode: 进入AI生成模式，返回生成命令
def prompt_mode(state):
    try:
        return cmd_generate(state, default='i')[0]
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
        return ''
    finally:
        print_context()
        state.mode = 'char'

# line_mode: 行模式，支持丰富命令分发与AI交互
def line_mode(state):
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
                    cmd_show(state)
                elif cmd in ['r','raw']:
                    print('\033[2J\033[H\r', end='')
                    print(state.screen.raw(), end='\r\n')
                elif cmd in ['ch','chat']:
                    print('\033[2J\033[H\r', end='')
                    state.ai.print()
                elif cmd in ['reset']:
                    print_context()
                    termios.tcsetattr(state.slave_fd, termios.TCSADRAIN, state.slave_tty)
                    state.screen.mode = 'normal'
                    state.screen.esc = ''
                    return ''
                elif cmd in ['c','clear']:
                    print('\033[2J\033[H\r', end='')
                elif cmd in ['w','watch']:
                    cmd_watch(state)
                elif cmd in ['g','gen','generate']:
                    cmd = cmd_generate(state, args)[0]
                    if cmd == '':
                        continue
                    os.write(state.master_fd, cmd.encode())
                    time.sleep(0.1)
                    cmd_show(state)
                elif cmd in ['e','exec']:
                    cmd, instruct = cmd_exec(state, cmd=args)
                    if cmd:
                        state.ai.save(instruct, state.screen.text(), cmd)
                        cmd += '\n'
                        os.write(state.master_fd, cmd.encode())
                        time.sleep(0.1)
                        cmd_show(state)
                elif cmd in ['i','input']:
                    cmd, instruct = cmd_exec(state, 'input', cmd=args, id='cmd_input')
                    if cmd:
                        os.write(state.master_fd, cmd.encode())
                        time.sleep(0.1)
                        cmd_show(state)
                elif cmd in ['esc']:
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
                elif cmd in ['t','tty']:
                    cmd_tty(state)
                    return ''
                elif cmd in ['a','auto']:
                    cmd_auto(state, args)
                elif cmd in ['err']:
                    cmd_err(state)
                elif cmd in ['conf','config','configs']:
                    state.ai.printConfigs(end='\r\n')
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
                    show_line(f"{cmd}: command not found")
            except Exception as e:
                print('error:', e, end='\r\n')
                state.err = traceback.format_exc()
    finally:
        state.mode = 'char'

# char_mode: 字符模式，逐字符读取，支持模式切换
def char_mode(state):
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

def read_command():
    if state.mode == 'char':
        cmd = char_mode(state)
    elif state.mode == 'line':
        cmd = line_mode(state)
    elif state.mode == 'prompt':
        cmd = prompt_mode(state)
    return cmd

# ====== AI配置与历史加载/保存 ======
# 加载AI配置与实例
def load_ai(state):
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    state.ai = MixedAI.from_config(config_file_path)
    if len(state.ai.ais) == 0:
        cmd_create(state, 'text', 'text')
        cmd_create(state, 'chat', 'chat')

def save_ai(state):
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    state.ai.save_config(config_file_path)

# ====== 启动主循环 ======
load_bufs(state)
load_ai(state)

try:
    os.write(sys.stdout.fileno(), b'\033c')  # 复位终端
    while state.proc.poll() is None:
        try:
            cmd = read_command()  # 读取用户输入
            os.write(state.master_fd, cmd.encode())  # 发送到子进程
        except Exception as e:
            print('error:', e, end='\r\n')
            state.err = traceback.format_exc()
            state.mode = 'char'
finally:
    # 退出时清理资源，保存历史，恢复终端
    state.screen.close()
    save_bufs(state)
    save_ai(state)
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, state.old_tty)
    state.running = False
    print('exited, if not exit, please input ctrl-c again')

