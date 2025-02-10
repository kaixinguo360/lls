#!/usr/bin/env python3

import os

model = os.environ.get('LLS_OPENAI_MODEL', 'gpt-4o-mini')
base_url = os.environ.get('LLS_OPENAI_BASE_URL', 'https://api.openai.com')
api_key = os.environ.get('LLS_OPENAI_API_KEY', '')
system_template = '''
你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足用户指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.

例如, 如果user指令为"列出文件", 你需要输出下面一行内容:
ls

例如, 如果user指令为"列出所有文件详情", 你需要输出下面一行内容:
ls -la

例如, 如果user指令为"列出文件详情", 你需要输出下面一行内容:
ls -l

例如, 如果user指令为"列出所有文件", 你需要输出下面一行内容:
ls -a

以下是当前的控制台输出:
{context}
'''
user_template = '请输出一行shell命令, 完成如下任务:{prompt}, 不要对输出shell命令进行格式化, 也不要输出任何说明与解释'

client = None

def create_client():
    global client
    from openai import OpenAI
    client = OpenAI(
       base_url=base_url,
       api_key=api_key, 
    )

def convert_output(output):
    global client
    think = ''
    if '</think>' in output:
        res = output.replace('<think>', '').split('</think>')
        think = res[0]
        output = res[1]
    output = output.strip()
    think = think.strip()
    return output, think

def generate_cmd(prompt, context):
    try:
        if client == None:
            create_client()
        system_info = system_template.format(prompt=prompt, context=context)
        user_info = user_template.format(prompt=prompt, context=context)
        response = client.chat.completions.create(
            model=model,
            messages=[
                { 'role': 'system', 'content': system_info },
                { 'role': 'user', 'content': user_info },
            ],
        )
        output = response.choices[0].message.content
        return convert_output(output)
    except Exception as e:
        return f'error: {e}', ''

if __name__ == '__main__':
    cmd, think = generate_cmd('列出目录内所有文件', '')
    print(f"cmd: {cmd}")
    if think:
        print(f"think: {think}")

