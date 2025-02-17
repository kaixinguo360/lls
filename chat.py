#!/usr/bin/env python3

import os
import json

from generate import AI, get_openai_client, convert_output, default_model

default_system_instrct = '''你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足user指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.

例如, 如果user指令为"列出文件", 你需要输出下面一行内容:
ls

例如, 如果user指令为"列出所有文件详情", 你需要输出下面一行内容:
ls -la

例如, 如果user指令为"列出文件详情", 你需要输出下面一行内容:
ls -l

例如, 如果user指令为"列出所有文件", 你需要输出下面一行内容:
ls -a
'''
default_user_template = '''生成一条满足user指令的shell命令

以下是当前user的指令:
{instrct}

以下是当前的控制台输出:
{console}
'''

class ChatAI(AI):

    def __init__(s, model=default_model, system_instrct=default_system_instrct, user_template=default_user_template):
        s.model = model
        s.user = 'user'
        s.user_template = default_user_template
        s.assistant = 'assistant'
        s.system = 'system'
        s.system_instrct = None
        s.default_instrct = '继续'
        s.console_max_height = 30
        s.messages = []

        if system_instrct is not None:
            s.system_instrct = system_instrct
            s.add(s.system, s.system_instrct)

    def add_messages(s, *args):
        for m in args:
            s.messages.append(m)

    def add(s, role, content, **kwargs):
        m = dict(role=role, content=content, **kwargs)
        s.add_messages(m)
        return m

    def create_user_message(s, instrct, console):
        lines = console.split('\n')
        if len(lines) > s.console_max_height:
            lines = lines[-s.console_max_height:]
        console = '\n'.join(lines)
        c = s.user_template.format(instrct=instrct, console=console)
        m = dict(role=s.user, content=c, instrct=instrct, console=console)
        return m

    def add_user(s, instrct, console):
        m = s.create_user_message(instrct, console)
        return s.add_messages(m)

    def pop(s):
        if len(s.messages) > 0:
            m = s.messages[-1]
            s.messages = s.messages[:-1]
            return m

    def _generate(s, callback=None, append_messages=None):
        yield '', ''
        client = get_openai_client()
        messages = []
        for m in s.messages:
            messages.append(dict(role=m['role'], content=m['content']))
        if append_messages is not None:
            for m in append_messages:
                messages.append(dict(role=m['role'], content=m['content']))
        stream = client.chat.completions.create(
            model=s.model,
            messages=messages,
            stream=True,
        )
        output = ''
        cmd, think = '', ''
        try:
            for chunk in stream:
                output += chunk.choices[0].delta.content or ''
                cmd, think = convert_output(output)
                yield cmd, think
        finally:
            if callback:
                callback(cmd, think)

    def generate(s, instrct, console):
        m_user = s.create_user_message(instrct, console)
        return s._generate(append_messages=[m_user])

    def save(s, instrct, console, output):
        if instrct is None:
            instrct = s.default_instrct
        m_user = s.create_user_message(instrct, console)
        m_ass = dict(role=s.assistant, content=output)
        s.add_messages(m_user)
        s.add_messages(m_ass)

    def print(s, end='\r\n', simple=False):
        winsize = os.get_terminal_size()
        for m in s.messages:
            role = m['role']
            if simple and role == 'user':
                content = m['instrct']
            else:
                content = m['content']
            print(f"{role.upper()}: {content}".replace('\n', end), end=end)
            print('+'+'-'*(winsize.columns-2)+'+', end=end)

if __name__ == '__main__':
    c = ChatAI()
    print('\033[2J\033[H\r', end='')
    c.print()
    m = ('查看当前日期', 'root@localhost# ')
    output = c.generate(*m)
    for chunk in output:
        cmd = chunk[0]
        think = chunk[1]
        print(f"\033[H\033[2J", end='')
        if think:
            print(f"think: {think}")
        if cmd:
            print(f"cmd: {cmd}")
    c.save(*m, cmd)
    print('\033[2J\033[H\r', end='')
    c.print()

