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
from commands import *

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
        print_context(state)
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

