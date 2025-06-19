"""
common.py
通用工具与辅助函数模块。
"""

import os
import traceback
import sys
import select
import queue
import threading

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
