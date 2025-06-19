#!/usr/bin/env python3
"""
generate.py
OpenAI接口、AI配置管理底层。
"""

import os
import traceback
import json

default_model = os.environ.get('LLS_OPENAI_MODEL', 'gpt-4o-mini')
base_url = os.environ.get('LLS_OPENAI_BASE_URL', 'https://api.openai.com')
api_key = os.environ.get('LLS_OPENAI_API_KEY', '')

client = None

def get_openai_client():
    """
    获取OpenAI客户端实例。
    """
    global client
    from openai import OpenAI
    client = OpenAI(
       base_url=base_url,
       api_key=api_key, 
    )
    return client

def convert_output(output):
    """
    解析AI输出，分离<think>标签内容。
    """
    think = ''
    if '<think>' in output:
        res = output.replace('<think>', '').split('</think>')
        think = res[0]
        output = res[1] if len(res) > 1 else ''
    output = output.strip()
    think = think.strip()
    return output, think

if __name__ == '__main__':
    pass

