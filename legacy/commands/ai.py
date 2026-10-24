"""
commands/ai.py
AI 实例管理相关命令：mode, create, remove, rename, ls, set, get 等
"""

from display import show_line, read_line, read_lines
from ai.registry import to_ai_type


def cmd_mode(state, args):
    """
    切换当前 AI 实例
    
    如果提供 args，直接切换到对应 ID；否则交互式选择
    """
    id = args
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


def cmd_create(state, args):
    """
    创建新的 AI 实例
    
    交互式输入实例 ID 和类型
    """
    id = read_line('(create-ai) id: ', cancel='', include_last=False)
    if not id:
        return
    
    type_name = read_line('(create-ai) type: ', cancel='', include_last=False)
    if not type_name:
        return
    
    try:
        t = to_ai_type(type_name)
        a = t()
    except:
        print(f"no such ai type '{type_name}'")
        return
    
    state.ai.add(id, a)
    state.ai.switch(id)
    show_line(f"created new ai '{id}'")


def cmd_remove(state, args):
    """
    删除指定 AI 实例
    
    可通过 args 指定 ID，否则交互式选择
    """
    id = args
    if not id:
        ids = '[' + ','.join(state.ai.ais.keys()) + ']'
        for id in state.ai.ais.keys():
            if state.ai.ais[id] == state.ai.ai:
                break
        id = read_line(f"(remove-ai) current ai is '{id}' {ids} ", cancel='', include_last=False)
        if not id:
            return
    
    state.ai.remove(id)
    show_line(f"removed ai '{id}'")


def cmd_rename(state, args):
    """
    重命名 AI 实例
    
    交互式输入原 ID 和新 ID
    """
    ids = '[' + ','.join(state.ai.ais.keys()) + ']'
    id = None
    
    for id in state.ai.ais.keys():
        if state.ai.ais[id] == state.ai.ai:
            break
    
    id = read_line(f"(rename-ai) current ai is '{id}' {ids} ", cancel='', include_last=False)
    if not id:
        return
    
    new_id = read_line(f"(rename-ai) selected ai '{id}', new id: ", cancel='', include_last=False)
    if not new_id:
        return
    
    state.ai.rename(id, new_id)
    show_line(f"renamed ai '{id}' to '{new_id}'")


def cmd_ls(state, args):
    """
    列出所有 AI 实例及其状态
    
    当前使用的实例用 [*] 标记
    """
    info = 'STATUS\tID\tTYPE\r\n'
    for id in state.ai.ais.keys():
        a = state.ai.ais[id]
        if state.ai.ai == a:
            info += f" [*]\t{id}\t{type(a).__name__}\r\n"
        else:
            info += f" [ ]\t{id}\t{type(a).__name__}\r\n"
    show_line(info)


def cmd_set(state, args):
    """
    设置当前 AI 实例的配置项
    
    格式：set <key> <value>
    """
    if not args:
        print('usage: set [key] [value]', end='\r\n')
        return
    
    try:
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
        import traceback
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()


def cmd_get(state, args):
    """
    获取当前 AI 实例的配置项
    
    如果不提供 args，则打印所有配置；否则打印指定配置的值
    """
    try:
        if not args:
            state.ai.printConfigs()
        else:
            key = args
            value = str(state.ai.get(key)).replace('\n', '\r\n')
            print(f'{key} = {value}')
    except Exception as e:
        import traceback
        print('error:', e, end='\r\n')
        state.err = traceback.format_exc()
