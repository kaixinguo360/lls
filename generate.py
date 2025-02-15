#!/usr/bin/env python3

import os

model = os.environ.get('LLS_OPENAI_MODEL', 'gpt-4o-mini')
base_url = os.environ.get('LLS_OPENAI_BASE_URL', 'https://api.openai.com')
api_key = os.environ.get('LLS_OPENAI_API_KEY', '')

client = None

def get_openai_client():
    global client
    from openai import OpenAI
    client = OpenAI(
       base_url=base_url,
       api_key=api_key, 
    )
    return client

def convert_output(output):
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
{instrct}

以下是满足user指令的shell命令:
'''

class AI():

    def generate(s, instrct, console):
        pass

    def save(s, instrct, console, output):
        pass

    def print(s, **kwargs):
        pass

class TextCompletionAI(AI):

    def __init__(s, prompt_template=default_prompt_template):
        s.prompt_template = prompt_template

    def generate(s, instrct, console):
        yield '', ''
        try:
            client = get_openai_client()
            prompt = s.prompt_template.format(instrct=instrct, console=console)
            stream = client.completions.create(
                model=model,
                prompt=prompt,
                stream=True,
            )
            output = ''
            for chunk in stream:
                output += chunk.choices[0].text or ''
                yield convert_output(output)
        except Exception as e:
            yield f'error: {e}', ''

    def print(s, end='\r\n'):
        print(s.prompt_template.replace('\n', end), end=end)

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

