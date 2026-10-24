"""
commands/generate.py
生成相关命令：generate, exec, input, auto 等
"""

import os
import sys
import time
import traceback
from display import show_line, read_line, read_lines, clear_lines, print_lines, record_line
from common import print_context, check_cancel, cancelable, save_history
from commands.core import cmd_show


def read_instruct(prompt, value='', state=None):
    """
    读取 AI 指令，支持 / 命令切换模式
    
    prompt: 提示符字符串
    value: 默认输入内容
    state: 全局状态对象
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
            if cmd in ['s', 'show', 'status']:
                cmd_show(state)
            elif cmd in ['set']:
                cmd_set_inner(state, args)
            elif cmd in ['get']:
                cmd_get_inner(state, args)
            elif cmd in ['m', 'mode']:
                cmd_mode_inner(state, args)
            elif cmd in ['c', 'ch', 'chat']:
                instruct = args
                cmd_mode_inner(state, 'chat')
            elif cmd in ['t', 'text']:
                instruct = args
                cmd_mode_inner(state, 'text')
    return instruct


def cmd_exec_handler(state, prompt='cmd', cmd=None, id='cmd'):
    """
    读取并执行命令，支持 # 分割指令与注释
    
    返回 (cmd, instruct)
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


def cmd_generate(state, args):
    """
    AI 生成命令主流程
    
    支持多轮确认、编辑、重试等操作。本项目最主要的功能。
    用户可通过快捷键与生成结果交互：y/u/i/n/e/s/r/k/t
    """
    instruct = args
    prompt = 'gen'
    default = 'u'
    
    if instruct is None:
        instruct = read_instruct(prompt, state=state)
    else:
        record_line(instruct, id='instruct')
    
    if instruct == '':
        return '', ''
    
    context = state.screen.text()
    cmd, think = '', ''
    
    if '#' in instruct:
        args_split = instruct.split('#')
        instruct = args_split[0].strip()
        cmd = args_split[-1].strip()
        output = None
    else:
        output = state.ai.generate(instruct, context)
    
    confirm_info = ', confirm?'
    flags = '[y/u/i/n/e/s/r/k/t]'
    save = False
    show_think = False
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
                    clear_lines(lines_all, lines_cur, clear=len(text) < prev_len)
                    lines_all, lines_cur = print_lines(text)
                    prev_len = len(text)
                del prev_len
            except KeyboardInterrupt:
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
        confirm_info = ', confirm?'
        
        confirm = read_line(text, cancel='cancel', exit='n', include_last=False)
        confirm = confirm.lower()
        if confirm == '':
            confirm = default
        
        if confirm in ['y', 'yes']:
            save = True
            break
        elif confirm in ['u', 'use']:
            break
        elif confirm in ['i', 'input']:
            enter = False
            break
        elif confirm in ['n', 'no', 'q', 'quit', 'exit']:
            cmd = ''
            break
        elif confirm in ['k', 'think']:
            show_think = True
        elif confirm in ['r', 're', 'retry']:
            output = state.ai.generate(instruct, context)
        elif confirm in ['e', 'edit']:
            instruct = read_instruct(prompt, value=instruct, state=state)
            if instruct == '':
                cmd = ''
                break
            output = state.ai.generate(instruct, context)
        elif confirm in ['t', 'teach']:
            default = 'y'
            cmd = read_line(f'({prompt}-cmd): ', include_last=False, id='cmd')
            if cmd == '':
                break
        elif confirm in ['s', 'show', 'status']:
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


def cmd_generate_wrap(state, args):
    """
    包装 AI 生成命令，自动写入子进程并刷新显示
    """
    cmd = cmd_generate(state, args)[0]
    if cmd == '':
        return None
    os.write(state.master_fd, cmd.encode())
    time.sleep(0.1)
    cmd_show(state)
    return cmd


def cmd_exec(state, args):
    """
    执行单条命令，将输入字符串传递给实际终端，末尾自动附加回车
    """
    cmd, instruct = cmd_exec_handler(state, cmd=args)
    if cmd:
        state.ai.save(instruct, state.screen.text(), cmd)
        cmd += '\n'
        os.write(state.master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show(state)
    return cmd


def cmd_exec_wrap(state, args):
    """
    包装命令执行，自动保存历史并写入子进程
    """
    return cmd_exec(state, args)


def cmd_input(state, args):
    """
    执行单条命令，将输入内容写入子进程，末尾不附加回车
    """
    cmd, instruct = cmd_exec_handler(state, 'input', cmd=args, id='cmd_input')
    if cmd:
        os.write(state.master_fd, cmd.encode())
        time.sleep(0.1)
        cmd_show(state)
    return cmd


def cmd_auto(state, args):
    """
    自动 AI 生成并执行命令
    
    适合批量/自动化场景。根据给定提示词和终端输出，持续生成命令
    """
    instruct = args
    if instruct is None:
        instruct = read_instruct('auto', state=state)
    else:
        record_line(instruct, id='auto-instruct')
    
    if instruct == '':
        return ''
    
    while True:
        cmd, instruct = cmd_generate(state, instruct)
        cmd_obj = cmd.rstrip('\n') if cmd else ''
        if cmd_obj == '':
            break
        cmd_with_newline = cmd_obj + '\n'
        os.write(state.master_fd, cmd_with_newline.encode())
        time.sleep(0.1)
        cmd_show(state)


# 内部辅助函数（供 read_instruct 中的 / 命令调用）
def cmd_set_inner(state, args):
    """read_instruct 中的 set 子命令"""
    try:
        if not args:
            state.ai.printConfigs()
        else:
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


def cmd_get_inner(state, args):
    """read_instruct 中的 get 子命令"""
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


def cmd_mode_inner(state, id_or_args):
    """read_instruct 中的 mode 子命令"""
    id = id_or_args
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
        show_line(info)
