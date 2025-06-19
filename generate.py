#!/usr/bin/env python3
"""
generate.py
AI基类、多模型管理、OpenAI接口、AI配置管理。
"""

import traceback
import json
import os

default_model = os.environ.get('LLS_OPENAI_MODEL', 'gpt-4o-mini')
base_url = os.environ.get('LLS_OPENAI_BASE_URL', 'https://api.openai.com')
api_key = os.environ.get('LLS_OPENAI_API_KEY', '')

client = None

ai_types = {}

def get_openai_client():
    """获取OpenAI客户端实例。"""
    global client
    from openai import OpenAI
    client = OpenAI(
       base_url=base_url,
       api_key=api_key, 
    )
    return client

def convert_output(output):
    """解析AI输出，分离<think>标签内容。"""
    think = ''
    if '<think>' in output:
        res = output.replace('<think>', '').split('</think>')
        think = res[0]
        output = res[1] if len(res) > 1 else ''
    output = output.strip()
    think = think.strip()
    return output, think

default_prompt_template = '''
你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足user指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.

例如, 如果user指令为"列出文件", 你需要输出下面一行内容:
ls

例如, 如果user指令为"列出所有文件详情", 你需要输出下面一行内容:
ls -la

例如, 如果user指令为"列出文件详情", 你需要输出下面一行内容:
ls -l

例如, 如果user指令为"列出所有文件", 你需要输出下面一行内容:
ls -a

以下是当前的控制台输出:
{console}

以下是当前user的指令:
{instruct}

以下是满足user指令的shell命令:
'''

class AI():
    """
    AI基类，定义通用接口。
    """
    def generate(self, instruct, console):
        pass

    def save(self, instruct, console, output):
        pass

    def print(self, **kwargs):
        pass

    def set(self, key, value):
        old = self.get(key)
        if old is not None:
            if isinstance(old, int):
                value = int(value)
            elif isinstance(old, float):
                value = float(value)
            elif isinstance(old, str):
                value = str(value)
            else:
                raise ValueError(f"unsupport argument type '{type(old).__name__}'")
        setattr(self, key, value)

    def get(self, key):
        return getattr(self, key)

    def configs(self):
        c = [i for i in self.__dict__.items() if i[0][:1] != '_']
        return sorted(c, key=lambda x:x[0])

    def printConfigs(self, end='\r\n'):
        for c in self.configs():
            key = c[0]
            value = str(c[1])
            _type = type(c[1]).__name__
            if isinstance(value, str):
                value = value.replace('\n', '\\n')
                if len(value) > 30:
                    value = value[:30] + '...'
            print(f"({_type}) {key} = {value}", end=end)

    @staticmethod
    def from_config(**kwargs):
        raise NotImplementedError

    def save_config(self, **kwargs):
        raise NotImplementedError

class MixedAI(AI):
    """
    多AI模型管理器，支持动态切换和配置。
    """
    ais = {}
    ai = None
    current_ai_id = None

    def add(self, id, ai):
        self.ais[id] = ai

    def remove(self, id):
        if id in self.ais.keys():
            a = self.ais[id]
            del self.ais[id]
            if a == self.ai:
                if len(self.ais) == 0:
                    self.ai = None
                else:
                    self.ai = self.ais[list(self.ais.keys())[0]]

    def switch(self, id):
        if id in self.ais.keys():
            self.ai = self.ais[id]
            self.current_ai_id = id
        else:
            raise ValueError(f"No such ai '{id}'")

    def rename(self, id, new_id):
        if id in self.ais.keys():
            self.ais[new_id] = self.ais[id]
            del self.ais[id]

    def generate(self, instruct, console):
        if self.ai:
            return self.ai.generate(instruct, console)
        else:
            def fun():
                yield '', 'no selected ai'
            return fun()

    def save(self, instruct, console, output):
        if self.ai:
            self.ai.save(instruct, console, output)
        else:
            pass

    def print(self, end='\r\n', **kwargs):
        if self.ai:
            self.ai.print(end=end, **kwargs)
        else:
            print("no selected ai", end=end)

    def set(self, key, value):
        if self.ai:
            self.ai.set(key, value)
        else:
            pass

    def get(self, key):
        if self.ai:
            return self.ai.get(key)
        else:
            return None

    def configs(self):
        if self.ai:
            return self.ai.configs()

    @staticmethod
    def from_config(path=None, config=None):
        s = MixedAI()
        try:
            if path:
                with open(path, 'r') as f:
                    config = json.load(f)
            ais = config.get('ai')
            if ais:
                for id in ais.keys():
                    try:
                        c = ais[id]
                        id = c.get('id')
                        t = to_ai_type(c.get('type'))
                        conf = c.get('config')
                        ai = t.from_config(config=conf)
                        s.add(id, ai)
                    except Exception as e:
                        err = traceback.format_exc().replace('\n', '\r\n')
                        print(f"prase ai config '{id}' failed:", err, end='\r\n')
            id = config.get('current_ai_id')
            if id:
                s.switch(id)
        except Exception as e:
            err = traceback.format_exc().replace('\n', '\r\n')
            print(f"prase ai config failed:", err, end='\r\n')
        return s

    def save_config(self, path=None):
        config = {
            'current_ai_id': self.current_ai_id,
            'ai': {},
        }
        for id in self.ais.keys():
            ai = self.ais[id]
            config['ai'][id] = {
                'id': id,
                'type': get_ai_type(ai),
                'config': ai.save_config(),
            }
        if path:
            with open(path, 'w') as f:
                json.dump(config, f)
        return config

class TextCompletionAI(AI):

    def __init__(s, model=default_model, prompt_template=default_prompt_template):
        s.model = model
        s.prompt_template = prompt_template
        s.post_processor = None

    def generate(s, instruct, console):
        yield '', ''
        try:
            client = get_openai_client()
            prompt = s.prompt_template.format(instruct=instruct, console=console)
            stream = client.completions.create(
                model=s.model,
                prompt=prompt,
                stream=True,
            )
            output = ''
            for chunk in stream:
                output += chunk.choices[0].text or ''
                cmd, think = convert_output(output)
                yield cmd, think
            if s.post_processor:
                local_vars = { 'cmd': cmd, 'think': think }
                exec(s.post_processor, local_vars)
                cmd = local_vars['cmd']
                think = local_vars['think']
                yield cmd, think
        except Exception as e:
            yield f'error: {e}', ''

    def print(s, end='\r\n'):
        print(s.prompt_template.replace('\n', end), end=end)

    @staticmethod
    def from_config(path=None, config=None):
        s = TextCompletionAI()
        if path:
            with open(path, 'r') as f:
                config = json.load(f)
        s.model = config.get('model', s.model)
        s.prompt_template = config.get('prompt_template', s.prompt_template)
        s.post_processor = config.get('post_processor', s.post_processor)
        return s

    def save_config(s, path=None):
        config = {
            'model': s.model,
            'prompt_template': s.prompt_template,
            'post_processor': s.post_processor,
        }
        if path:
            with open(path, 'w') as f:
                json.dump(config, f)
        return config

def register_ai_type(id, t):
    global ai_types
    ai_types[id] = t

def to_ai_type(id):
    global ai_types
    return ai_types[id]

def get_ai_type(ai):
    global ai_types
    for id in ai_types:
        t = ai_types[id]
        if type(ai) == t:
            return id
    return 'unknown'

register_ai_type('base', AI)
register_ai_type('mixed', MixedAI)
register_ai_type('text', TextCompletionAI)

if __name__ == '__main__':
    a = TextCompletionAI()
    output = a.generate('列出目录内所有文件', '')
    for chunk in output:
        cmd = chunk[0]
        think = chunk[1]
        print(f"\033[H\033[2J", end='')
        if think:
            print(f"think: {think}")
        if cmd:
            print(f"cmd: {cmd}")

