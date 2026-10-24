import os
import traceback
import sys
import json
import select
import queue
import threading
from terminal import Screen
from ai.mixed import MixedAI

class TerminalState:
    def __init__(self):
        self.command = None
        self.old_tty = None  # 原始终端设置
        self.master_fd = None
        self.slave_fd = None
        self.slave_callback = None
        self.slave_tty = None
        self.winsize = None
        self.proc = None

class LLSState(TerminalState):
    def __init__(self):
        self.err = None  # 错误信息缓存
        self.ai = None
        self.screen_history_file_path = None
        self.screen = None
        self.running = True
        self.mode = 'char'
        self.bufs = None
        self.total_chars = 0

def print_context(state):
    """
    恢复终端显示，刷新当前屏幕内容。
    """
    if state.screen.buffer == 'main' and state.screen.y == len(state.screen.lines) - 1:
        line = state.screen._raw.split('\n')[-1]
        os.write(sys.stdout.fileno(), ('\033[2K\r' + line).encode())
    else:
        os.write(sys.stdout.fileno(), ('\033[2K\r' + state.screen._raw).encode())

def save_history(state, prompt, context, cmd):
    """
    保存AI生成历史到命令历史文件
    """
    try:
        with open(os.path.join(os.environ['HOME'], '.cmd_history'), 'a') as f:
            line = f"prompt: {prompt}\t{cmd}\n"
            f.write(line)
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()

def check_cancel(cancel_chars=['\x03','\x04']):
    """
    检查是否有取消输入，如Ctrl-C、Ctrl-D（专供cancelable函数调用）
    """
    f, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
    if sys.stdin.fileno() in f:
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in cancel_chars:
                return True
    return False

def cancelable(generator):
    """
    生成器包装，使其可被取消（如AI流式输出时可中断）
    """
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

# ====== AI配置与历史加载/保存 ======

# 加载AI配置与实例
def load_ai(state):
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    state.ai = MixedAI.from_config(config_file_path)
    #if len(state.ai.ais) == 0:
    #    cmd_create(state, 'text', 'text')
    #    cmd_create(state, 'chat', 'chat')

# 保存AI配置与实例
def save_ai(state):
    config_file_path = os.path.join(os.environ.get('HOME', os.getcwd()), '.lls_ai_config')
    state.ai.save_config(config_file_path)

# ====== 历史缓冲区管理 ======

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

# ====== 终端窗口大小管理 ======

import struct
import fcntl
import termios

# 同步窗口大小到伪终端
def sync_winsize(state):
    state.winsize = os.get_terminal_size()
    state.screen.max_height = state.winsize.lines
    set_winsize(state.slave_fd, state.winsize.lines, state.winsize.columns)

# 设置终端窗口大小
def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)