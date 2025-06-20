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
from common import LLSState

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
import tty
import pty
import signal

from ai.mixed import MixedAI
from ai.text import TextCompletionAI
from ai.chat import ChatAI
from terminal import Screen
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

sync_winsize(state)
signal.signal(signal.SIGWINCH, lambda x, y: sync_winsize(state))  # 监听窗口大小变化

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

# ====== 历史缓冲区管理 & AI实例管理 ======
state.bufs = get_bufs()
load_bufs(state)
load_ai(state)

# ====== 启动主循环 ======
try:
    os.write(sys.stdout.fileno(), b'\033c')  # 复位终端
    while state.proc.poll() is None:
        try:
            cmd = read_command(state)  # 读取用户输入
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
