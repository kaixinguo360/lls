#!/usr/bin/env python3
"""
chat.py
对话式AI实现，负责消息管理、对话生成、与OpenAI接口对接。
"""

import os
import json

from generate import AI, get_openai_client, convert_output, default_model, register_ai_type

default_system_instruct = '''你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足user指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.

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
{instruct}

以下是当前的控制台输出:
{console}
'''

class ChatAI(AI):
    """
    对话式AI，支持多轮消息管理与生成。
    """
    def __init__(self, model=default_model, system_instruct=default_system_instruct, user_template=default_user_template):
        self.model = model
        self.user = 'user'
        self.user_template = default_user_template
        self.assistant = 'assistant'
        self.system = 'system'
        self.system_instruct = None
        self.default_instruct = '继续'
        self.console_max_height = 30
        self.messages = []

        if system_instruct is not None:
            self.system_instruct = system_instruct
            self.add(self.system, self.system_instruct)

    def add_messages(self, *args):
        """批量添加消息。"""
        for m in args:
            self.messages.append(m)

    def add(self, role, content, **kwargs):
        """添加单条消息。"""
        m = dict(role=role, content=content, **kwargs)
        self.add_messages(m)
        return m

    def create_user_message(self, instruct, console):
        """根据指令和控制台内容生成user消息。"""
        lines = console.split('\n')
        if len(lines) > self.console_max_height:
            lines = lines[-self.console_max_height:]
        console = '\n'.join(lines)
        c = self.user_template.format(instruct=instruct, console=console)
        m = dict(role=self.user, content=c, instruct=instruct, console=console)
        return m

    def add_user(self, instruct, console):
        m = self.create_user_message(instruct, console)
        return self.add_messages(m)

    def pop(self):
        """弹出最后一条消息。"""
        if len(self.messages) > 0:
            m = self.messages[-1]
            self.messages = self.messages[:-1]
            return m

    def _generate(self, callback=None, append_messages=None):
        """底层生成器，支持流式输出。"""
        yield '', ''
        client = get_openai_client()
        messages = []
        for m in self.messages:
            messages.append(dict(role=m['role'], content=m['content']))
        if append_messages is not None:
            for m in append_messages:
                messages.append(dict(role=m['role'], content=m['content']))
        stream = client.chat.completions.create(
            model=self.model,
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

    def generate(self, instruct, console):
        """生成对话回复。"""
        m_user = self.create_user_message(instruct, console)
        return self._generate(append_messages=[m_user])

    def save(self, instruct, console, output):
        """保存一轮对话。"""
        if instruct is None:
            instruct = self.default_instruct
        m_user = self.create_user_message(instruct, console)
        m_ass = dict(role=self.assistant, content=output)
        self.add_messages(m_user)
        self.add_messages(m_ass)

    def print(self, end='\r\n', simple=False):
        """打印历史消息。"""
        winsize = os.get_terminal_size()
        for m in self.messages:
            role = m['role']
            if simple and role == 'user':
                content = m['instruct']
            else:
                content = m['content']
            print(f"{role.upper()}: {content}".replace('\n', end), end=end)
            print('+'+'-'*(winsize.columns-2)+'+', end=end)

    @staticmethod
    def from_config(path=None, config=None):
        s = ChatAI()
        if path:
            with open(path, 'r') as f:
                config = json.load(f)
        s.model = config.get('model', s.model)
        s.user = config.get('user', s.user)
        s.user_template = config.get('user_template', s.user_template)
        s.assistant = config.get('assistant', s.assistant)
        s.system = config.get('system', s.system)
        s.system_instruct = config.get('system_instruct', s.system_instruct)
        s.default_instruct = config.get('default_instruct', s.default_instruct)
        s.console_max_height = config.get('console_max_height', s.console_max_height)
        s.messages = []
        if s.system_instruct is not None:
            s.add(s.system, s.system_instruct)
        return s

    def save_config(self, path=None):
        config = {
            'model': self.model,
            'user': self.user,
            'user_template': self.user_template,
            'assistant': self.assistant,
            'system': self.system,
            'system_instruct': self.system_instruct,
            'default_instruct': self.default_instruct,
            'console_max_height': self.console_max_height,
        }
        if path:
            with open(path, 'w') as f:
                json.dump(config, f)
        return config

register_ai_type('chat', ChatAI)

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

