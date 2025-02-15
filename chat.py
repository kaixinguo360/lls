#!/usr/bin/env python3

import json

from generate import get_openai_client, convert_output, model

default_system_instruct = '''
你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足user指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.

例如, 如果user指令为"列出文件", 你需要输出下面一行内容:
ls

例如, 如果user指令为"列出所有文件详情", 你需要输出下面一行内容:
ls -la

例如, 如果user指令为"列出文件详情", 你需要输出下面一行内容:
ls -l

例如, 如果user指令为"列出所有文件", 你需要输出下面一行内容:
ls -a
'''
default_user_template = '''
生成一条满足user指令的shell命令

以下是当前user的指令:
{instruct}

以下是当前的控制台输出:
{console}
'''

class Chat():

    def __init__(s, system_instruct=default_system_instruct, user_template=default_user_template):
        s.user = 'user'
        s.user_template = default_user_template
        s.assistant = 'assistant'
        s.system = 'system'
        s.system_instruct = None
        s.default_instrct = '继续'
        s.console_max_height = 30
        s.last_try = None
        s.messages = []

        if system_instruct is not None:
            s.system_instruct = system_instruct
            s.add(s.system, s.system_instruct)

    def add_messages(s, *args):
        for m in args:
            s.messages.append(m)

    def add(s, role, content, **kwargs):
        m = dict(role=role, content=content, **kwargs)
        s.add_messages(m)
        return m

    def create_user_message(s, instruct, console):
        lines = console.split('\n')
        if len(lines) > s.console_max_height:
            lines = lines[-s.console_max_height:]
        console = '\n'.join(lines)
        c = s.user_template.format(instruct=instruct, console=console)
        m = dict(role=s.user, content=c)
        return m

    def add_user(s, instruct, console):
        m = s.create_user_message(instruct, console)
        return s.add_messages(m)

    def pop(s):
        if len(s.messages) > 0:
            m = s.messages[-1]
            s.messages = s.messages[:-1]
            return m

    def generate(s, callback=None, append_messages=None):
        yield '', ''
        client = get_openai_client()
        messages = s.messages[:]
        if append_messages:
            messages += append_messages
        stream = client.chat.completions.create(
            model=model,
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

    def try_generate(s, instruct, console):
        m_user = s.create_user_message(instruct, console)
        def callback(cmd, think):
            m_ass = dict(role=s.assistant, content=cmd)
            s.last_try = (m_user, m_ass)
        return s.generate(append_messages=[m_user])

    def commit(s, cmd=None):
        if s.last_try:
            args = s.last_try
            m_user = args[0]
            m_ass = args[1]
            s.last_try = None
            if cmd is not None:
                m_ass['content'] = cmd
            s.add_messages(m_user)
            s.add_messages(m_ass)

    def add_chat(s, instrct=None, console=None, cmd=None):
        if instrct is None:
            instrct = s.default_instrct
        m_user = s.create_user_message(instrct, console)
        m_ass = dict(role=s.assistant, content=cmd)
        s.add_messages(m_user)
        s.add_messages(m_ass)

def print_chat_perfect(c, end='\n'):
    print(json.dumps(c.messages, indent=2, ensure_ascii=False).replace('\n', end))

if __name__ == '__main__':
    c = Chat()
    print_chat_perfect(c)
    m = ('查看当前日期', 'root@localhost# ')
    output = c.try_generate(*m)
    for chunk in output:
        cmd = chunk[0]
        think = chunk[1]
        print(f"\033[H\033[2J", end='')
        if think:
            print(f"think: {think}")
        if cmd:
            print(f"cmd: {cmd}")
    print_chat_perfect(c)
    c.commit('aaaaa')
    print_chat_perfect(c)

