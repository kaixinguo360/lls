"""
commands/__init__.py
命令模块的导入和注册入口
"""

import os
import sys
import time
import traceback
from display import show_line, read_line
from common import print_context
from commands.registry import register

# Step 1: 导入所有命令
from commands.core import (
    cmd_quit, cmd_show_status, cmd_raw, cmd_chat, cmd_reset, 
    cmd_clear, cmd_err, cmd_conf
)
from commands.terminal import cmd_watch, cmd_tty, cmd_esc
from commands.generate import (
    cmd_generate_wrap, cmd_exec, cmd_exec_wrap, cmd_input, cmd_auto
)
from commands.ai import (
    cmd_mode, cmd_create, cmd_remove, cmd_rename, cmd_ls, cmd_set, cmd_get
)

# Step 2: 依次注册 - 就这一件事
register(['q', 'quit', 'exit'], cmd_quit)
register(['s', 'show', 'status'], cmd_show_status)
register(['r', 'raw'], cmd_raw)
register(['ch', 'chat'], cmd_chat)
register(['reset'], cmd_reset)
register(['c', 'clear'], cmd_clear)
register(['w', 'watch'], cmd_watch)
register(['t', 'tty'], cmd_tty)
register(['esc'], cmd_esc)
register(['g', 'gen', 'generate'], cmd_generate_wrap)
register(['e', 'exec'], cmd_exec_wrap)
register(['i', 'input'], cmd_input)
register(['a', 'auto'], cmd_auto)
register(['err'], cmd_err)
register(['conf', 'config', 'configs'], cmd_conf)
register(['set'], cmd_set)
register(['get'], cmd_get)
register(['m', 'mode'], cmd_mode)
register(['create'], cmd_create)
register(['remove', 'del', 'delete'], cmd_remove)
register(['rename'], cmd_rename)
register(['l', 'ls'], cmd_ls)

# 导出注册接口
from commands.registry import execute_command, get_command


# ====== 输入模式控制 ======

def read_command(state):
    """根据当前模式分发到对应的处理函数"""
    if state.mode == 'char':
        cmd = char_mode(state)
    elif state.mode == 'line':
        cmd = line_mode(state)
    elif state.mode == 'prompt':
        cmd = prompt_mode(state)
    return cmd


def prompt_mode(state):
    """
    进入 AI 生成模式，返回生成命令
    """
    try:
        return cmd_generate_wrap(state, None)
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
        return ''
    finally:
        print_context(state)
        state.mode = 'char'


def line_mode(state):
    """
    命令模式，根据输入命令分发给对应函数
    统一使用注册表进行分发
    """
    try:
        while True:
            try:
                # 读取行模式下的用户输入
                cmd = read_line(cancel='q', include_last=False, id='line_mode', no_save=['q'])
                cmd = cmd.strip()
                args = None
                
                if ' ' in cmd and cmd[:1] != ' ':
                    index = cmd.find(' ')
                    args = cmd[index+1:].strip()
                    cmd = cmd[:index].strip()
                
                if cmd == '':
                    continue
                
                # 通过统一入口执行命令
                result = execute_command(cmd, state, args)
                
                # 处理返回值
                if result == 'exit':
                    return ''
                elif result is None:
                    continue
                else:
                    return result
            except Exception as e:
                show_line(f"error: {str(e)}")
                state.err = traceback.format_exc()
    finally:
        state.mode = 'char'


def char_mode(state):
    """
    字符模式，逐字符读取，支持模式切换
    Ctrl-E 切换到 line_mode
    Ctrl-G 切换到 prompt_mode
    """
    try:
        output = ''
        chars = os.read(sys.stdin.fileno(), 10240).decode()
        for c in chars:
            if c in ['\005']:  # Ctrl-E
                state.mode = 'line'
            elif c in ['\007']:  # Ctrl-G
                state.mode = 'prompt'
            else:
                output += c
        return output
    except Exception as e:
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
        return ''


__all__ = ['execute_command', 'get_command', 'read_command', 'char_mode', 'line_mode', 'prompt_mode']
