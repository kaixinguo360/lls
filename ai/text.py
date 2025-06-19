# 文本补全AI
from .base import AI
from .registry import register_ai_type
import json
import os

default_model = os.environ.get('LLS_OPENAI_MODEL', 'gpt-4o-mini')
default_prompt_template = '''\n你是一个能干的助手, 需要根据user的指令和当前的shell控制台输出, 生成一条满足user指令的shell命令. 你的输出将直接发送给控制台并执行, 因此你不能输出shell命令以外的任何无关内容. 你不需要用任何引号包裹输出的shell命令, 也不需要格式化输出的shell命令.\n'''

class TextCompletionAI(AI):
    def __init__(self, model=default_model, prompt_template=default_prompt_template):
        self.model = model
        self.prompt_template = prompt_template
        self.post_processor = None

    def generate(self, instruct, console):
        yield '', ''
        try:
            from generate import get_openai_client, convert_output
            client = get_openai_client()
            prompt = self.prompt_template.format(instruct=instruct, console=console)
            stream = client.completions.create(
                model=self.model,
                prompt=prompt,
                stream=True,
            )
            output = ''
            for chunk in stream:
                output += chunk.choices[0].text or ''
                cmd, think = convert_output(output)
                yield cmd, think
            if self.post_processor:
                local_vars = { 'cmd': cmd, 'think': think }
                exec(self.post_processor, local_vars)
                cmd = local_vars['cmd']
                think = local_vars['think']
                yield cmd, think
        except Exception as e:
            yield f'error: {e}', ''

    def print(self, end='\r\n'):
        print(self.prompt_template.replace('\n', end), end=end)

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

    def save_config(self, path=None):
        config = {
            'model': self.model,
            'prompt_template': self.prompt_template,
            'post_processor': self.post_processor,
        }
        if path:
            with open(path, 'w') as f:
                json.dump(config, f)
        return config

register_ai_type('text', TextCompletionAI)
