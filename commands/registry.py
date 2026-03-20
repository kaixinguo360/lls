"""
commands/registry.py
命令注册与分发机制
"""

import traceback

# 全局命令表
_commands = {}


def register(names, func):
    """
    注册命令
    
    names: str 或 [str, ...] - 命令名称与别名
    func: 函数对象 - 前两个参数应为 (state, args)
    不返回任何东西
    """
    # 规范化 names 为列表
    if isinstance(names, str):
        names = [names]
    
    # 自动提取 docstring
    doc = (func.__doc__ or '').strip()
    lines = [line.strip() for line in doc.split('\n') if line.strip()]
    summary = lines[0] if lines else '(无说明)'
    details = '\n'.join(lines[1:]) if len(lines) > 1 else ''
    
    # 保存命令信息
    info = {
        'func': func,
        'names': names,
        'summary': summary,
        'details': details,
        'module': func.__module__
    }
    
    # 为每个别名都注册
    for name in names:
        _commands[name] = info


def get_command(name):
    """
    根据命令名获取命令信息
    """
    return _commands.get(name)


def execute_command(name, state, args):
    """
    执行命令
    
    name: 命令名称
    state: LLSState 对象
    args: 命令参数字符串或 None
    返回命令的返回值
    """
    cmd_info = get_command(name)
    if not cmd_info:
        raise KeyError(f"{name}: command not found")
    
    func = cmd_info['func']
    try:
        return func(state, args)
    except Exception as e:
        state.err = traceback.format_exc()
        return None
